"""Manifest-locked campaign execution with OAT and 200-row pilot gates."""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from multiprocessing import get_context
from pathlib import Path

from epsm_core import EnergyPlusRunner
from epsm_core.compiler import CompilationReport

from .analysis import baseline, qa
from .campaign import generate, manifest
from .oat import run_oat
from .store import CampaignStore


def simulate_one(executable, spec, path):
    return EnergyPlusRunner(executable).run(spec, path)


def execute(store, runner, limit, workers):
    pending = iter(store.pending(limit))

    def submit(pool, item):
        if shutil.disk_usage(store.path).free < 20 * 1024**3:
            raise RuntimeError("Campaign stopped with less than 20 GiB free disk space")
        rid, spec = item
        store.write_weather(spec.climate)
        store.db.execute("UPDATE runs SET status='running' WHERE run_id=?", [rid])
        path = store.path / "artifacts" / rid
        if path.exists():
            path = store.path / "artifacts" / (rid + "_retry_" + uuid.uuid4().hex[:8])
        return pool.submit(simulate_one, str(runner.compiler.executable), spec, path), rid, path

    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
        active = {}
        for _ in range(workers):
            item = next(pending, None)
            if item:
                future, rid, path = submit(pool, item)
                active[future] = (rid, path)
        count = 0
        while active:
            finished, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in finished:
                rid, path = active.pop(future)
                try:
                    result = future.result()
                    report = CompilationReport.model_validate_json(
                        (path / "compilation.json").read_text()
                    )
                    store.complete(result, report)
                    # Retain native SQL losslessly without doubling the hourly data footprint.
                    sql = path / "eplusout.sql"
                    compressed = path / "eplusout.sql.gz"
                    staging = path / "eplusout.sql.gz.pending"
                    with (
                        sql.open("rb") as source,
                        gzip.open(staging, "wb", compresslevel=1) as target,
                    ):
                        shutil.copyfileobj(source, target)

                    def digest(stream):
                        h = hashlib.sha256()
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            h.update(block)
                        return h.hexdigest()

                    with sql.open("rb") as source, gzip.open(staging, "rb") as restored:
                        if digest(source) != digest(restored):
                            raise ValueError("SQL compression validation failed")
                    os.replace(staging, compressed)
                    sql.unlink()
                except Exception as e:
                    store.db.execute(
                        "UPDATE runs SET status='failed',error=? WHERE run_id=?", [str(e), rid]
                    )
                    raise
                count += 1
                if count % 10 == 0:
                    print(f"Completed {count} runs in this stage", flush=True)
                item = next(pending, None)
                if item:
                    future, rid, path = submit(pool, item)
                    active[future] = (rid, path)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["plan", "pilot", "run", "qa", "ml"])
    p.add_argument("config", type=Path)
    p.add_argument("store", type=Path)
    p.add_argument("--energyplus", required=True)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    config = json.loads(args.config.read_text())
    runner = EnergyPlusRunner(args.energyplus)
    campaign_manifest = manifest(config, runner.compiler)
    store = CampaignStore(args.store, campaign_manifest)
    if args.command in ("qa", "ml"):
        report = baseline(store) if args.command == "ml" else qa(store, config.get("legacy_csv"))
        (store.path / (args.command + ".json")).write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return
    if store.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0:
        cases = generate(config)
        store.plan(cases, runner.compiler)
        print(f"Planned {len(cases)} simulations", flush=True)
    if args.command == "plan":
        return
    store.recover()
    if not store.db.execute("SELECT 1 FROM gates WHERE name='oat'").fetchone():
        from epsm_core import BuildingSpec

        base = BuildingSpec.model_validate_json(
            store.db.execute("SELECT spec_json FROM runs ORDER BY ordinal LIMIT 1").fetchone()[0]
        )
        report = run_oat(base, runner, store.path / "oat")
        (store.path / "oat.json").write_text(json.dumps(report, indent=2))
        if not all(r["passed"] for r in report.values()):
            raise ValueError("OAT directional gate failed")
        store.db.execute("INSERT INTO gates VALUES ('oat',?)", [json.dumps(report)])
    execute(store, runner, 200, args.workers)
    validation = store.validate()
    if validation["errors"] or validation["orphan_partitions"]:
        raise ValueError(validation)
    if (
        store.db.execute(
            "SELECT count(*) FROM runs WHERE ordinal<200 AND status='complete'"
        ).fetchone()[0]
        != 200
    ):
        raise ValueError("Pilot incomplete")
    store.db.execute("INSERT OR REPLACE INTO gates VALUES ('pilot',?)", [json.dumps(validation)])
    (store.path / "pilot_qa.json").write_text(json.dumps(qa(store), indent=2))
    if args.command == "run":
        execute(store, runner, None, args.workers)
        report = qa(store, config.get("legacy_csv"))
        (store.path / "qa.json").write_text(json.dumps(report, indent=2))
        (store.path / "ml.json").write_text(json.dumps(baseline(store), indent=2))


if __name__ == "__main__":
    main()
