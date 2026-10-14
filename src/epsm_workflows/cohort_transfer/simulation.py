"""Content-addressed native Core simulations and scalar emulator diagnostics.

References: Core HeatingDeliverySpec; EPSM Data apply_cohort_profile. No observed
heating enters a simulation receipt. Failed models retain specs and diagnostics.
"""

from __future__ import annotations

import math
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from threading import Lock

import numpy as np
from epsm_core import BuildingSpec, EnergyPlusRunner, MaterialSpec

from ..campaign import atomic_json
from ..mf_physics import add_insulation_overlay, apply_physical_profile
from .common import digest, read

_WORKER = None


def _initialize_worker(root):
    global _WORKER
    _WORKER = Evaluator(root, workers=1)


def _evaluate_worker(item):
    row, settings = item
    return _WORKER.evaluate(row, settings)


class Evaluator:
    """Evaluate explicit parameters using strict, restartable EnergyPlus receipts."""

    def __init__(self, root, *, workers=None):
        self.root = Path(root)
        self.config = read(self.root / "config.json")
        self.registers = read(self.root / "parameter-register.json")
        self.workers = workers or self.config["workers"]
        self.specs = {}
        self.results = {}
        self.locks = {}
        self.mutex = Lock()
        self.identity = digest(read(self.root / "manifest.json"))
        self.pool = None

    def spec(self, row):
        with self.mutex:
            if row["building_id"] not in self.specs:
                self.specs[row["building_id"]] = BuildingSpec.model_validate(
                    read(self.root / row["spec_path"])
                )
            return self.specs[row["building_id"]]

    def effective_parameters(self, row, parameters):
        """Deactivate absent roles and independently locked inputs before simulation."""
        if row.get("status") != "prepared":
            return {}
        register = self.registers[row["building_id"]]
        locked = set(row.get("locked_parameters", ()))
        if "wall_resistance" in locked:
            locked.add("wall_insulation_overlay_m")
        return {
            name: value
            for name, value in parameters.items()
            if name not in locked
            and (
                name not in register
                or register[name].get("status", "screen_and_cohort_calibration")
                == "screen_and_cohort_calibration"
            )
        }

    def case_key(self, row, parameters):
        """Canonical case identity includes only physically applicable settings."""
        return digest(
            (
                self.identity,
                row["building_id"],
                self.effective_parameters(row, parameters),
            )
        )

    def evaluate(self, row, parameters):
        """Return carrier EUI, with source/engine identity part of the receipt key."""
        if row["status"] != "prepared":
            return None
        effective = self.effective_parameters(row, parameters)
        key = self.case_key(row, parameters)
        with self.mutex:
            lock = self.locks.setdefault(key, Lock())
        with lock:
            if key in self.results:
                return self.results[key].get("prediction")
            folder = self.root / "simulations" / key
            path = folder / "receipt.json"
            if path.exists():
                record = read(path)
                if record["identity"] != key:
                    raise ValueError("Simulation cache identity changed")
            else:
                record = {
                    "identity": key,
                    "building_id": row["building_id"],
                    "parameters": effective,
                    "requested_parameters": parameters,
                    "status": "failed",
                    "prediction": None,
                }
                try:
                    source = self.spec(row)
                    register = self.registers[row["building_id"]]
                    physical_parameters = dict(effective)
                    overlay_depth = physical_parameters.pop("wall_insulation_overlay_m", None)
                    if overlay_depth is not None:
                        definition = self.config.get("wall_overlay")
                        bound = register["wall_insulation_overlay_m"]["bounds"]
                        if (
                            definition is None
                            or not math.isfinite(overlay_depth)
                            or not bound[0] <= overlay_depth <= bound[1]
                        ):
                            raise ValueError("Wall overlay requires sourced material and frozen bounds")
                        if overlay_depth > 0:
                            wall, overlay_audit = add_insulation_overlay(
                                source.envelope.wall,
                                material=MaterialSpec.model_validate(definition["material"]),
                                thickness_m=overlay_depth,
                                source=definition["source"],
                            )
                            source = source.updated(envelope=source.envelope.updated(wall=wall))
                            record["wall_overlay_audit"] = overlay_audit
                    profile = {
                        # A new prepared study fits against its own frozen
                        # baseline; this declaration does not migrate old fits.
                        "infiltration_basis": "atemp_area_rate"
                        if source.airflow.envelope_infiltration_l_s_m2 is not None
                        else "core_volume_ach",
                        "parameters": physical_parameters,
                        "bounds": {k: v["bounds"] for k, v in register.items()},
                        "source": "Frozen effective cohort diagnostic; parameters are inferred assumptions",
                    }
                    spec = apply_physical_profile(
                        source,
                        profile,
                        locked_parameters=row.get("locked_parameters", ()),
                        resistance_basis=self.config.get("resistance_basis"),
                    )
                    atomic_json(folder / "spec.json", spec.model_dump(mode="json"))
                    output = folder / "energyplus"
                    if output.exists():
                        index = 0
                        while (folder / f"interrupted-run-{index}").exists():
                            index += 1
                        retained = folder / f"interrupted-run-{index}"
                        output.rename(retained)
                        record["retained_interrupted_artifacts"] = str(retained)
                    result = EnergyPlusRunner(energyplus=self.config["engine"]).run(
                        spec, output,
                        # The frozen campaign config explicitly authorizes the Core
                        # approximation; its receipt marks recovered shading for review.
                        recover_context_shadows=self.config.get("recover_context_shadows", False),
                    )
                    carrier = result.scalars.get("space_heating_carrier_input_kwh")
                    if carrier is None or not math.isfinite(carrier) or carrier < 0:
                        raise ValueError(
                            "Delivered-space-heat result unavailable or invalid"
                        )
                    prediction = carrier / row["atemp_m2"]
                    record.update(
                        status="accepted",
                        prediction=prediction,
                        spec_sha256=spec.physical_hash(),
                        weather_sha256=spec.climate.weather_sha256,
                        thermal_demand_kwh=result.scalars["space_heating_demand_kwh"],
                        scalars=result.scalars,
                        diagnostics=result.diagnostics,
                    )
                except Exception as exc:  # noqa: BLE001 - preserve every failed scientific case
                    record["reason"] = f"{type(exc).__name__}: {exc}"
                atomic_json(path, record)
            self.results[key] = record
            return record.get("prediction")

    def batch(self, rows, designs):
        """Execute the full declared panel before outcomes can select a candidate."""
        items = [
            (row, name, settings) for row in rows for name, settings in designs.items()
        ]
        # SQLite releases the GIL for every row; many Python reader threads
        # contend heavily. Spawned processes retain native Core behavior and use
        # independent connections. Deduplicate before submission so receipt paths
        # never have two writers. Existing completed receipts remain authoritative.
        unique = {}
        for row, _, settings in items:
            key = self.case_key(row, settings)
            unique.setdefault(key, (row, settings))
        values, pending = {}, []
        for key, (row, settings) in unique.items():
            if row["status"] != "prepared":
                values[key] = None
            elif (self.root / "simulations" / key / "receipt.json").exists():
                values[key] = self.evaluate(row, settings)
            else:
                pending.append((key, row, settings))
        if (
            pending
            and self.config.get("parallel_backend", "threads") == "processes"
            and self.workers > 1
        ):
            if self.pool is None:
                self.pool = ProcessPoolExecutor(
                    max_workers=self.workers,
                    mp_context=get_context("spawn"),
                    initializer=_initialize_worker,
                    initargs=(str(self.root),),
                )
            predictions = self.pool.map(
                _evaluate_worker, [(row, p) for _, row, p in pending]
            )
            for (key, _, _), prediction in zip(pending, predictions, strict=True):
                values[key] = prediction
        elif pending:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                predictions = pool.map(lambda x: self.evaluate(x[1], x[2]), pending)
                for (key, _, _), prediction in zip(pending, predictions, strict=True):
                    values[key] = prediction
        return {
            (row["building_id"], name): values[self.case_key(row, settings)]
            for row, name, settings in items
        }

    def close(self):
        """Join owned workers after all requested native cases complete."""
        if self.pool is not None:
            self.pool.shutdown(wait=True)
            self.pool = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def quadratic_emulator(designs, predictions):
    """Check a quadratic emulator on held-out simulation settings only.

    It is a search diagnostic, never a deployed predictor. Accept approximation
    only if holdout MAE <=2% of the mean physical prediction and >=8 complete
    settings exist. Direct Core evaluations remain authoritative in all cases.
    """
    names = sorted({name for settings in designs.values() for name in settings})
    rows = [(settings, predictions.get(key)) for key, settings in designs.items()]
    rows = [(p, y) for p, y in rows if y is not None and math.isfinite(y)]
    if len(rows) < max(8, 2 * len(names) + 2):
        return {"status": "insufficient_simulation_settings", "deployed": False}
    x = np.array([[p.get(name, 0) for name in names] for p, _ in rows])
    center, scale = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-9)
    z = (x - center) / scale
    x = np.column_stack((np.ones(len(z)), z, z * z))
    target = np.array([y for _, y in rows])
    held = np.arange(len(rows)) % 5 == 0
    coef = np.linalg.lstsq(x[~held], target[~held], rcond=None)[0]
    mae = float(np.abs(x[held] @ coef - target[held]).mean())
    fraction = mae / max(float(target[held].mean()), 1e-9)
    return {
        "status": "validated" if fraction <= 0.02 else "rejected",
        "holdout_simulation_mae_kwh_m2": mae,
        "relative_mae": fraction,
        "deployed": False,
        "selection_uses_direct_core": True,
    }
