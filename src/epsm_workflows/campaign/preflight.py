# SPDX-License-Identifier: MIT
"""Compile every planned case without running EnergyPlus; gate submission on it."""
from __future__ import annotations

import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb

from ..records import atomic_json, canonical
from .store import plan_info

_COMPILER = None
_WEATHERS = {}
_ROOT = None


def _schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS identity(key VARCHAR PRIMARY KEY, value JSON)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS results(
        ordinal INTEGER PRIMARY KEY, case_id VARCHAR NOT NULL, status VARCHAR NOT NULL,
        failure_code VARCHAR, message VARCHAR, seconds DOUBLE NOT NULL)"""
    )


def identity(root, energyplus):
    from .worker import runtime_identity

    info = plan_info(root)
    return {"campaign_id": info["campaign_id"], "plan_sha256": info["plan_sha256"], **runtime_identity(energyplus)}


def _initialize(root, energyplus):
    global _COMPILER, _ROOT
    from epsm_core import EnergyPlusCompiler

    _ROOT = Path(root)
    _COMPILER = EnergyPlusCompiler(energyplus)


def _weather(weather_id):
    if weather_id not in _WEATHERS:
        from epsm_core import Weather

        item = json.loads((_ROOT / "weather.json").read_text())[weather_id]
        _WEATHERS[weather_id] = Weather.from_epw(_ROOT / item["path"], calendar_year=item["calendar_year"])
    return _WEATHERS[weather_id]


def _compile(row):
    from epsm_core import Building, preflight_energyplus

    ordinal, case_id, sample_id, weather_id, building_json = row
    started = time.monotonic()
    try:
        building = Building.from_dict(json.loads(building_json))
        building.identifier = sample_id
        building.readiness(engine="energyplus").require()
        spec = building.to_spec(_weather(weather_id))
        preflight_energyplus(spec).require()
        _COMPILER.compile(spec)
        return ordinal, case_id, "passed", None, None, time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 - every compile failure is evidence
        code = getattr(exc, "code", None) or type(exc).__name__
        return ordinal, case_id, "failed", str(code), str(exc)[:2000], time.monotonic() - started


def preflight(root, energyplus, workers=1):
    """Compile all cases exhaustively; a resumed run keeps committed rows."""
    root = Path(root).resolve()
    expected = identity(root, energyplus)
    path = root / "preflight.duckdb"
    db = duckdb.connect(str(path))
    _schema(db)
    stored = db.execute("SELECT value FROM identity WHERE key='runtime'").fetchone()
    if stored and json.loads(stored[0]) != expected:
        db.close()
        raise ValueError("preflight.duckdb was produced by a different plan or runtime; remove it to restart")
    if not stored:
        db.execute("INSERT INTO identity VALUES ('runtime', ?)", [canonical(expected)])
    done = {r[0] for r in db.execute("SELECT ordinal FROM results").fetchall()}
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    rows = [
        r
        for r in plan.execute(
            "SELECT r.ordinal,r.case_id,r.sample_id,r.weather_id,b.building_json FROM runs r "
            "JOIN buildings b USING(building_key) ORDER BY r.ordinal"
        ).fetchall()
        if r[0] not in done
    ]
    plan.close()
    if rows:
        if workers == 1:
            _initialize(root, energyplus)
            outcomes = map(_compile, rows)
        else:
            pool = ProcessPoolExecutor(max_workers=workers, initializer=_initialize, initargs=(root, energyplus))
            outcomes = pool.map(_compile, rows, chunksize=8)
        batch = []
        for outcome in outcomes:
            batch.append(outcome)
            if len(batch) >= 64:
                db.executemany("INSERT INTO results VALUES (?,?,?,?,?,?)", batch)
                batch = []
        if batch:
            db.executemany("INSERT INTO results VALUES (?,?,?,?,?,?)", batch)
        if workers != 1:
            pool.shutdown()
    total, failed = db.execute("SELECT count(*), count(*) FILTER (status='failed') FROM results").fetchone()
    sample = db.execute(
        "SELECT ordinal,case_id,failure_code,message FROM results WHERE status='failed' ORDER BY ordinal LIMIT 20"
    ).fetchall()
    db.execute("CHECKPOINT")
    db.close()
    receipt = {
        "compiled": total,
        "expected": plan_info(root)["runs"],
        "failed": failed,
        "sample_failures": [dict(zip(("ordinal", "case_id", "failure_code", "message"), s)) for s in sample],
        "runtime": expected,
        "finished_at": time.time(),
    }
    atomic_json(root / "preflight.json", receipt)
    if failed or total != receipt["expected"]:
        raise ValueError(f"Preflight failed: {failed} failures, {total}/{receipt['expected']} compiled")
    return receipt


def require_preflight(root, energyplus=None):
    """Refuse submission unless every case compiled under the current runtime."""
    root = Path(root)
    receipt_path = root / "preflight.json"
    if not receipt_path.exists():
        raise ValueError("Run preflight before submitting")
    receipt = json.loads(receipt_path.read_text())
    if receipt["failed"] or receipt["compiled"] != receipt["expected"]:
        raise ValueError("Preflight did not pass for every case")
    if energyplus is not None:
        current = identity(root, energyplus)
        keys = ("campaign_id", "plan_sha256", "energyplus")
        if any(current[k] != receipt["runtime"].get(k) for k in keys):
            raise ValueError("Preflight was performed with a different plan or EnergyPlus")
    return receipt
