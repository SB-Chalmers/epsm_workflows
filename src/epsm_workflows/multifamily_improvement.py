"""Existing-data-only shared SE.02/F multifamily diagnostic experiment.

References: EPSM Data LoD2 v1.3 calibration/extraction review; Core Sveby
residential v2.0 Tables 8.1/9.1 and generated-edge contract; existing Gothenburg
AMY/EPC cohort under archetype-diagnosis-mf-se02-2026-10-03. The 40-point screen
is a hypothesis search, never an annual validation or an individual best-fit.
Previously examined properties remain development observations. Source geometry,
material identity and infiltration are fixed across all common-family candidates.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import qmc

from .archetype_application import _hash, _implementations
from .archetype_diagnosis.physics import build_spec as legacy_spec
from .archetype_pilot import _insulation_scaled
from .campaign import atomic_json, file_sha256

ENGINE = "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus"
SEED = 71353


def designs():
    """Freeze 40 joint designs plus baseline and window-only evidence anchors.

    Insulation thickness bounds follow the prior diagnostic ±20% hypothesis;
    window 2.2 is the archived surviving-stock alternative to released 2.8,
    not an established original-construction correction. Flows .30/.35/.40 are
    operating sensitivities around Sveby's .35, not inferred measurements.
    """
    result = {
        "baseline": {"wall": 1.0, "roof": 1.0, "window": 2.8, "flow": None},
        "window_22": {"wall": 1.0, "roof": 1.0, "window": 2.2, "flow": None},
    }
    for i, point in enumerate(qmc.LatinHypercube(d=4, seed=SEED).random(40)):
        result[f"joint_{i:02d}"] = {
            "wall": float(0.8 + 0.4 * point[0]),
            "roof": float(0.8 + 0.4 * point[1]),
            "window": 2.2 if point[2] < 0.5 else 2.8,
            "flow": (0.30, 0.35, 0.40)[min(2, int(point[3] * 3))],
        }
    return result


def native_spec(row, weather, parameters):
    """Apply native Atemp/calendar/delivery APIs once, without density rescaling.

    Apartment count alone does not identify Sveby's room-size categories. Keep
    Core's labelled occupancy fallback unless room mix is observed. Net delivery
    losses are explicitly zero in this diagnostic baseline and reported as unknown;
    no DHW is added to the separately reported EPC delivered-space-heating target.
    """
    from epsm_core import DeliverySpec, HeatingDeliverySpec, load_sveby_spec

    original = legacy_spec(row, "existing", weather, "frozen")
    ratio = row["atemp_m2"] / original.geometry.conditioned_floor_area_m2
    flow = original.airflow.mechanical_vent_l_s_m2 / ratio
    if parameters["flow"] is not None:
        flow = parameters["flow"]
    spec = original.updated(
        load_reference_area_m2=row["atemp_m2"],
        use=load_sveby_spec().use_spec(calendar_year=int(row["measurement_year"])),
        airflow=original.airflow.updated(mechanical_vent_l_s_m2=flow),
        envelope=original.envelope.updated(
            wall=_insulation_scaled(original.envelope.wall, parameters["wall"]),
            top=_insulation_scaled(original.envelope.top, parameters["roof"]),
        ),
        facade=original.facade.updated(
            glazing=original.facade.glazing.updated(u_value_w_m2k=parameters["window"])
        ),
        delivery=DeliverySpec(
            heating=HeatingDeliverySpec(
                carrier="district_heat",
                source="Diagnostic ideal delivery; net losses unknown, held at zero",
            )
        ),
    )
    if "soil" in parameters:
        spec = spec.updated(
            envelope=spec.envelope.updated(
                foundation=spec.envelope.foundation.updated(
                    soil_conductivity_w_mk=parameters["soil"]
                )
            )
        )
    if "attic" in parameters:
        from epsm_core import UnconditionedExchangeSpec

        spec = spec.updated(
            unconditioned_exchange=(
                UnconditionedExchangeSpec(
                    zone_kind="attic",
                    outdoor_air_ach=parameters["attic"],
                    source="Prior parameter_sensitivity.py 1/2 ACH diagnostic; actual exchange unknown",
                ),
            )
        )
    if "loss" in parameters:
        spec = spec.updated(
            delivery=spec.delivery.updated(
                heating=spec.delivery.heating.updated(
                    net_distribution_loss_kwh_m2_year=parameters["loss"],
                    source="Prior 0/5/10 kWh/m2 net-loss diagnostic; actual loss unknown",
                )
            )
        )
    return spec


def evidence(row):
    """Keep reported renovation and unknown inputs separate from assumed defaults."""
    fields = (
        "completed_renovation_year",
        "reported_completed_measures",
        "reno_window",
        "reno_wall",
        "reno_roof",
        "reno_ventilation",
        "apartments",
        "outline",
        "roof_form",
        "heated_basement_floors",
        "designed_ventilation_l_s_m2",
        "measurement_year",
        "property_key",
        "source_file",
        "source_line",
        "source_row_sha256",
    )
    return {
        **{k: row.get(k) for k in fields},
        "missing_renovation_means_unrenovated": False,
        "occupancy": "Sveby labelled fallback; apartment room mix unavailable",
        "flow_basis": "reported EPC design flow"
        if row.get("designed_ventilation_l_s_m2") is not None
        else "unknown; Sveby .35 assumed",
        "attic_exchange": "unknown; sealed default",
        "ground": "Kiva default soil; observed soil unavailable",
        "exposure": "context unavailable; exposed facade assumption",
        "delivery": "net loss unknown; zero diagnostic baseline",
        "construction_state": "existing catalogue hypothesis; renovation missingness retained",
    }


def summary(frame, attempted):
    """Count failed simulations in the original attempted-building denominator."""
    accepted = frame[frame.status == "accepted"]
    return {
        "attempted": attempted,
        "accepted": len(accepted),
        "coverage20": float((accepted.ape < 20).sum() / attempted)
        if attempted
        else None,
        "coverage10": float((accepted.ape < 10).sum() / attempted)
        if attempted
        else None,
        "mae_kwh_m2": float(accepted.absolute_error.mean()) if len(accepted) else None,
        "bias_kwh_m2": float(accepted.signed_error.mean()) if len(accepted) else None,
    }


def run(source, output, *, workers=4):
    """Freeze and execute a retrospective shared-family experiment, retaining failures.

    Original training membership selects one family setting; previously opened
    validation/test properties are labelled development contrasts. Incomplete
    training panels retain baseline. No reshuffling creates an untouched holdout.
    """
    from epsm_core import EnergyPlusRunner

    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cohort = source / "physics/cohort.parquet"
    rows = pd.read_parquet(cohort).to_dict("records")
    rows = [
        {k: None if isinstance(v, float) and math.isnan(v) else v for k, v in r.items()}
        for r in rows
    ]
    weather = source / "physics/weather"
    panel = designs()
    candidates = tuple(panel)
    for name, change in {
        "sensitivity_soil_10": {"soil": 1.0},
        "sensitivity_soil_25": {"soil": 2.5},
        "sensitivity_attic_1": {"attic": 1.0},
        "sensitivity_attic_2": {"attic": 2.0},
        "sensitivity_loss_5": {"loss": 5.0},
        "sensitivity_loss_10": {"loss": 10.0},
    }.items():
        panel[name] = {**panel["baseline"], **change}
    target_path = source / "targets-development.parquet"
    from .archetype_diagnosis.physics import catalogue_spec

    helper_sources = [
        Path(inspect.getmodule(function).__file__)
        for function in (legacy_spec, catalogue_spec, _insulation_scaled)
    ]
    identities = {
        str(p): file_sha256(p)
        for p in [
            cohort,
            target_path,
            Path(__file__),
            *helper_sources,
            *sorted(weather.glob("*")),
        ]
        if p.is_file()
    }
    runner = EnergyPlusRunner(energyplus=ENGINE)
    engine_identity = {
        "engine_hash": runner.compiler.engine_hash,
        "schema_hash": runner.compiler.schema_hash,
    }
    manifest = {
        "engine": engine_identity,
        "schema_version": 1,
        "inputs": identities,
        "implementations": _implementations(),
        "designs": panel,
        "seed": SEED,
        "study_kind": "retrospective_diagnostic",
        "target_boundary": "normal-use adjusted delivered space heat before weather correction",
        "independent_properties": len({r["property_key"] for r in rows}),
        "building_evidence": {r["building_id"]: evidence(r) for r in rows},
        "confirmation_gates": {
            "minimum_buildings": 120,
            "minimum_properties": {"train": 30, "validation": 10, "test": 20},
            "fresh_holdout": False,
            "passed": False,
        },
        "catalogue_changed": False,
    }
    path = output / "manifest.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError("Frozen diagnostic inputs changed")
    atomic_json(path, manifest)
    if len({r["property_key"] for r in rows}) != len(rows):
        raise ValueError("This diagnostic bootstrap requires one building per property")
    targets = pd.read_parquet(target_path).set_index("building_id").target
    if any(
        not math.isfinite(float(targets.loc[r["building_id"]]))
        or targets.loc[r["building_id"]] <= 0
        for r in rows
    ):
        raise ValueError("Positive finite space-heat targets required")

    def case(job):
        row, scenario = job
        folder = output / "cases" / row["building_id"] / scenario
        receipt = folder / "receipt.json"
        if receipt.exists():
            return json.loads(receipt.read_text())
        folder.mkdir(parents=True, exist_ok=True)
        result = {
            "building_id": row["building_id"],
            "property_key": row["property_key"],
            "original_split": row["split"],
            "scenario": scenario,
            "status": "failed",
            "target": float(targets.loc[row["building_id"]]),
            "atemp_m2": row["atemp_m2"],
        }
        try:
            spec = native_spec(row, weather, panel[scenario])
            atomic_json(folder / "spec.json", spec.model_dump(mode="json"))
            run = EnergyPlusRunner(energyplus=ENGINE).run(spec, folder / "energyplus")
            prediction = (
                float(run.scalars["space_heating_carrier_input_kwh"]) / row["atemp_m2"]
            )
            if not math.isfinite(prediction) or prediction < 0:
                raise ValueError("Invalid delivered heating prediction")
            delta = prediction - result["target"]
            result.update(
                status="accepted",
                prediction=prediction,
                signed_error=delta,
                absolute_error=abs(delta),
                ape=100 * abs(delta) / result["target"],
                scalars=run.scalars,
                diagnostics=run.diagnostics,
                spec_sha256=spec.physical_hash(),
            )
        except Exception as exc:  # noqa: BLE001 -- retain every engine/compiler failure receipt
            result["reason"] = str(exc)
        atomic_json(receipt, result)
        print(row["building_id"][:8], scenario, result["status"], flush=True)
        return result

    # Smoke-test one original failure and two other properties before expanding.
    smoke = [case((r, "baseline")) for r in rows[:3]]
    atomic_json(output / "smoke.json", smoke)
    if any(r["status"] != "accepted" for r in smoke):
        atomic_json(
            output / "complete.json",
            {
                "status": "stopped_after_smoke_failure",
                "original_attempted_buildings": len(rows),
            },
        )
        return
    jobs = [
        (r, s)
        for r in rows
        for s in panel
        if not ("attic" in panel[s] and r["roof_form"] == "flat")
    ]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(case, jobs))
    frame = pd.DataFrame(results)
    frame.drop(columns=["scalars", "diagnostics"], errors="ignore").to_parquet(
        output / "per-building.parquet", index=False
    )
    training = frame[frame.original_split == "train"]
    scores = {
        s: summary(
            training[training.scenario == s], len(training[training.scenario == s])
        )
        for s in candidates
    }
    complete = all(
        v["accepted"] == v["attempted"] and v["attempted"] > 0 for v in scores.values()
    )
    chosen = (
        min(
            scores,
            key=lambda s: (
                -scores[s]["coverage20"],
                scores[s]["mae_kwh_m2"],
                s != "baseline",
                s,
            ),
        )
        if complete
        else "baseline"
    )
    atomic_json(
        output / "selection.json",
        {
            "scenario": chosen,
            "scores": scores,
            "objective": "coverage20_then_mae",
            "source": "original training only; all properties previously examined",
            "complete_training_panel": complete,
            "individual_fitting": False,
        },
    )
    report = {
        "study_kind": "retrospective_diagnostic",
        "selected_scenario": chosen,
        "baseline": summary(frame[frame.scenario == "baseline"], len(rows)),
        "selected": summary(frame[frame.scenario == chosen], len(rows)),
        "original_training": scores[chosen],
        "development_contrast": summary(
            frame[(frame.scenario == chosen) & (frame.original_split != "train")],
            sum(r["split"] != "train" for r in rows),
        ),
        "confirmed_population_accuracy": False,
        "catalogue_changed": False,
        "unresolved": [
            "renovation state",
            "exposed facade/context",
            "attic exchange",
            "soil/foundation",
            "delivery losses",
            "occupancy room mix",
        ],
        "hypothesis_parameters": panel[chosen],
    }
    base = frame[frame.scenario == "baseline"].set_index("building_id")
    selected = frame[frame.scenario == chosen].set_index("building_id")
    paired = base[["absolute_error", "prediction", "status"]].join(
        selected[["absolute_error", "prediction", "status"]],
        lsuffix="_baseline",
        rsuffix="_selected",
    )
    paired.to_csv(output / "paired.csv")
    accepted = paired[
        (paired.status_baseline == "accepted") & (paired.status_selected == "accepted")
    ]
    if len(accepted):
        rng = np.random.default_rng(SEED)
        changes = (
            accepted.absolute_error_selected - accepted.absolute_error_baseline
        ).to_numpy()
        boot = changes[rng.integers(0, len(changes), size=(2000, len(changes)))].mean(
            axis=1
        )
        report["paired_mae_change_kwh_m2"] = {
            "mean": float(changes.mean()),
            "property_bootstrap_95_percent": np.quantile(boot, [0.025, 0.975]).tolist(),
            "improved": int((changes < 0).sum()),
            "worsened": int((changes > 0).sum()),
        }
    report["sensitivity_only"] = {
        name: summary(frame[frame.scenario == name], sum(s == name for _, s in jobs))
        for name in panel
        if name not in candidates
    }
    report["evidence_subgroups"] = {}
    for field in (
        "designed_ventilation_l_s_m2",
        "completed_renovation_year",
        "roof_form",
    ):
        groups = {}
        for row in rows:
            label = (
                str(row[field])
                if field == "roof_form"
                else ("unknown" if row[field] is None else "reported")
            )
            groups.setdefault(label, []).append(row["building_id"])
        report["evidence_subgroups"][field] = {
            label: summary(
                frame[(frame.scenario == chosen) & frame.building_id.isin(ids)],
                len(ids),
            )
            for label, ids in groups.items()
        }
    atomic_json(output / "report.json", report)
    text = (
        f"# Multifamily shared-candidate diagnostic\n\nPreviously examined {len(rows)} buildings; no untouched validation claim.\n\n"
        f"Baseline coverage below 20%: {report['baseline']['coverage20']:.1%}. Selected shared setting: {chosen}; coverage {report['selected']['coverage20']:.1%}.\n"
        f"Below 10%: {report['selected']['coverage10']:.1%}. Accepted {report['selected']['accepted']}/{len(rows)}.\n\n"
        "Selection uses original training properties only; remaining properties are development contrasts. "
        "The 120-building and 30/10/20 independent-property confirmation gates remain unmet. "
        "No released catalogue changes are justified by this experiment. See manifest.json for known/unknown evidence and selection.json for all training candidates.\n"
    )
    (output / "report.md").write_text(text)
    atomic_json(
        output / "complete.json",
        {
            "status": "complete",
            "planned_cases": len(jobs),
            "accepted": int((frame.status == "accepted").sum()),
            "manifest_sha256": _hash(manifest),
        },
    )


def main(argv=None):
    """Execute the pinned existing cohort without obtaining new observations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    args = parser.parse_args(argv)
    run(args.source, args.output, workers=args.workers)


if __name__ == "__main__":
    main()
