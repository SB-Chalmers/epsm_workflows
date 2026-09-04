"""Single-writer DuckDB catalogue with atomic, validated Parquet partitions."""

import hashlib
import json
import os
import uuid
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from epsm_core import BuildingSpec
from epsm_core.weather import file_sha256

HOURLY_SCHEMA = pa.schema(
    [("hour", pa.int16()), ("signal", pa.string()), ("unit", pa.string()), ("value", pa.float64())]
)
SCALAR_SCHEMA = pa.schema([("target", pa.string()), ("value", pa.float64())])


class CampaignStore:
    def __init__(self, path, manifest):
        self.path = Path(path).resolve()
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.manifest_hash = hashlib.sha256(canonical.encode()).hexdigest()
        existed = self.path.exists() and any(self.path.iterdir())
        if existed and not (self.path / "store.duckdb").exists():
            raise ValueError("Nonempty campaign directory without a catalogue")
        self.path.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self.path / "store.duckdb"))
        if existed:
            try:
                found = self.db.execute("SELECT hash FROM manifest").fetchone()
            except duckdb.Error:
                self.db.close()
                raise ValueError("Nonempty catalogue has no campaign manifest")
            if not found or found[0] != self.manifest_hash:
                self.db.close()
                raise ValueError("Campaign manifest mismatch; refusing to mix runs")
        else:
            self.db.execute("CREATE TABLE manifest(hash VARCHAR PRIMARY KEY, json VARCHAR)")
            self.db.execute("INSERT INTO manifest VALUES (?,?)", [self.manifest_hash, canonical])
            self.db.execute("""CREATE TABLE runs(run_id VARCHAR PRIMARY KEY, ordinal INTEGER UNIQUE,
                status VARCHAR, spec_json VARCHAR, metadata_json VARCHAR, report_json VARCHAR,
                scalar_json VARCHAR, diagnostics_json VARCHAR, hourly_hash VARCHAR, scalar_hash VARCHAR,
                error VARCHAR)""")
            self.db.execute("CREATE TABLE gates(name VARCHAR PRIMARY KEY, report_json VARCHAR)")
            self.db.execute(
                "CREATE TABLE weather(checksum VARCHAR PRIMARY KEY, parquet_hash VARCHAR)"
            )
        (self.path / "manifest.json").write_text(json.dumps(manifest, indent=2))

    def plan(self, cases, compiler):
        rows = [
            (
                compiler.run_id(c.spec),
                i,
                "pending",
                c.spec.model_dump_json(),
                json.dumps(c.metadata()),
            )
            for i, c in enumerate(cases)
        ]
        if len(set(r[0] for r in rows)) != len(rows):
            raise ValueError("Duplicate resolved physical run IDs in plan")
        self.db.executemany(
            "INSERT INTO runs(run_id,ordinal,status,spec_json,metadata_json) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            rows,
        )
        if self.db.execute("SELECT count(*) FROM runs").fetchone()[0] != len(rows):
            raise ValueError("Catalogue plan differs from requested campaign")

    def pending(self, limit=None):
        query = "SELECT run_id,spec_json FROM runs WHERE status!='complete'"
        if limit is not None:
            query += f" AND ordinal < {int(limit)}"
        for rid, spec in self.db.execute(query + " ORDER BY ordinal").fetchall():
            yield rid, BuildingSpec.model_validate_json(spec)

    def _atomic(self, table, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        stage = path.with_suffix(".pending-" + uuid.uuid4().hex)
        pq.write_table(table, stage, compression="zstd")
        with stage.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(stage, path)

    def write_weather(self, climate):
        import csv

        if self.db.execute(
            "SELECT 1 FROM weather WHERE checksum=?", [climate.weather_sha256]
        ).fetchone():
            return
        with open(climate.epw_path) as f:
            for _ in range(8):
                next(f)
            rows = list(csv.reader(f))
        if len(rows) != 8760:
            raise ValueError("Weather must have 8760 rows")
        cols = {"hour": np.arange(8760, dtype=np.int16)}
        for name, index in (
            ("month", 1),
            ("day", 2),
            ("dry_bulb_c", 6),
            ("relative_humidity_pct", 8),
            ("global_horizontal_wh_m2", 13),
            ("direct_normal_wh_m2", 14),
            ("diffuse_horizontal_wh_m2", 15),
            ("wind_speed_m_s", 21),
        ):
            cols[name] = [float(r[index]) for r in rows]
        path = self.path / "weather" / (climate.weather_sha256 + ".parquet")
        self._atomic(pa.table(cols), path)
        self.db.execute(
            "INSERT INTO weather VALUES (?,?)", [climate.weather_sha256, file_sha256(path)]
        )

    def complete(self, result, report):
        if not result.hourly or any(
            len(v) != 8760 or not np.isfinite(v).all() for v in result.hourly.values()
        ):
            raise ValueError("Invalid hourly output")
        if any(not np.isfinite(v) or v < 0 for v in result.scalars.values()):
            raise ValueError("Invalid scalar output")
        for target in ("space_heating_demand_kwh", "dhw_load_kwh", "ideal_thermal_load_kwh"):
            if not np.isclose(
                result.hourly[target].sum(), result.scalars[target], rtol=0.001, atol=1e-6
            ):
                raise ValueError("Annual target disagrees with hourly output")
        arrays = []
        for name, values in sorted(result.hourly.items()):
            arrays.append(
                pa.Table.from_pydict(
                    dict(
                        hour=np.arange(8760, dtype=np.int16),
                        signal=[name] * 8760,
                        unit=[result.units[name]] * 8760,
                        value=values,
                    ),
                    schema=HOURLY_SCHEMA,
                )
            )
        directory = self.path / "partitions" / result.run_id
        hp, sp = directory / "hourly.parquet", directory / "scalars.parquet"
        self._atomic(pa.concat_tables(arrays), hp)
        self._atomic(
            pa.Table.from_pydict(
                dict(target=list(result.scalars), value=list(result.scalars.values())),
                schema=SCALAR_SCHEMA,
            ),
            sp,
        )
        self._validate_partition(result.run_id)
        self.db.execute("BEGIN TRANSACTION")
        try:
            self.db.execute(
                """UPDATE runs SET status='complete',report_json=?,scalar_json=?,
                diagnostics_json=?,hourly_hash=?,scalar_hash=?,error=NULL WHERE run_id=?""",
                [
                    report.model_dump_json(),
                    json.dumps(result.scalars),
                    json.dumps(result.diagnostics),
                    file_sha256(hp),
                    file_sha256(sp),
                    result.run_id,
                ],
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _validate_partition(self, rid):
        p = self.path / "partitions" / rid
        for name, schema in (("hourly", HOURLY_SCHEMA), ("scalars", SCALAR_SCHEMA)):
            table = pq.read_table(p / (name + ".parquet"))
            if table.schema != schema or not table.num_rows:
                raise ValueError(f"Schema drift or empty {name} partition: {rid}")
            if not np.isfinite(table["value"].to_numpy()).all():
                raise ValueError("Nonfinite Parquet values")
        table = pq.read_table(p / "hourly.parquet")
        grouped = table.group_by("signal").aggregate(
            [("hour", "count"), ("hour", "count_distinct"), ("hour", "min"), ("hour", "max")]
        )
        if any(v != 8760 for v in grouped["hour_count"].to_pylist()):
            raise ValueError("Incomplete hourly partition")
        if any(v != 8760 for v in grouped["hour_count_distinct"].to_pylist()):
            raise ValueError("Duplicate hourly indices")
        if any(v != 0 for v in grouped["hour_min"].to_pylist()) or any(
            v != 8759 for v in grouped["hour_max"].to_pylist()
        ):
            raise ValueError("Incorrect hourly indices")

    def validate(self):
        errors = []
        rows = self.db.execute("SELECT run_id,status,hourly_hash,scalar_hash FROM runs").fetchall()
        complete = {r[0] for r in rows if r[1] == "complete"}
        for rid, status, hh, sh in rows:
            if status != "complete":
                continue
            try:
                self._validate_partition(rid)
                for name, expected in (("hourly", hh), ("scalars", sh)):
                    if (
                        file_sha256(self.path / "partitions" / rid / (name + ".parquet"))
                        != expected
                    ):
                        raise ValueError("Partition checksum mismatch")
            except Exception as e:
                errors.append(f"{rid}: {e}")
        partitions = self.path / "partitions"
        orphans = (
            [p.name for p in partitions.iterdir() if p.is_dir() and p.name not in complete]
            if partitions.exists()
            else []
        )
        for checksum, expected in self.db.execute("SELECT * FROM weather").fetchall():
            p = self.path / "weather" / (checksum + ".parquet")
            if not p.exists() or file_sha256(p) != expected:
                errors.append(f"Invalid weather {checksum}")
        return dict(completed=len(complete), errors=errors, orphan_partitions=orphans)

    def recover(self):
        # A killed process leaves pending files, never a completed catalogue entry.
        report = self.validate()
        if report["errors"]:
            raise ValueError(report)
        for rid in report["orphan_partitions"]:
            quarantine = self.path / "quarantine" / (rid + "_" + uuid.uuid4().hex)
            quarantine.parent.mkdir(exist_ok=True)
            os.replace(self.path / "partitions" / rid, quarantine)
        self.db.execute("UPDATE runs SET status='pending' WHERE status='running'")

    def hourly(self, run_id, join_weather=True):
        record = self.db.execute(
            "SELECT spec_json,status FROM runs WHERE run_id=?", [run_id]
        ).fetchone()
        if not record or record[1] != "complete":
            raise KeyError(run_id)
        table = pq.read_table(self.path / "partitions" / run_id / "hourly.parquet")
        if not join_weather:
            return table
        checksum = json.loads(record[0])["climate"]["weather_sha256"]
        return table.join(
            pq.read_table(self.path / "weather" / (checksum + ".parquet")), keys="hour"
        )
