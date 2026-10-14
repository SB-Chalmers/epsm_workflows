"""Reuse native cases only after exact engine, compiler and physical verification.

A changed experiment creates a new campaign. Old observations never influence
cache eligibility. Receipts reference immutable archived artifacts; source/spec
checksums are pinned in the new manifest. This supports orchestration changes
without pretending that altered physical models were already simulated.
"""

from pathlib import Path

from epsm_core import BuildingSpec
from epsm_data.cohorts import apply_cohort_profile

from ..campaign import atomic_json, file_sha256
from .common import digest, read


def import_native_receipts(root, previous_roots):
    """Import accepted cases with identical Core sources, engine and native spec."""
    root = Path(root)
    manifest = read(root / "manifest.json")
    config = read(root / "config.json")
    registers = read(root / "parameter-register.json")
    rows = {r["building_id"]: r for r in read(root / "features.json")}
    identity = digest(manifest)
    imported, skipped, provenance = [], [], {}
    core_sources = {k: v for k, v in manifest["sources"].items() if "/epsm_core/" in k}
    # Pin references in a separate cache manifest. The campaign identity must
    # stay fixed, otherwise imported keys would invalidate themselves.
    for previous in previous_roots:
        previous = Path(previous).resolve()
        old_manifest = read(previous / "manifest.json")
        old_core = {
            k: v for k, v in old_manifest["sources"].items() if "/epsm_core/" in k
        }
        engine = config["engine"]
        if (
            old_core != core_sources
            or old_manifest["inputs"].get(engine) != manifest["inputs"][engine]
        ):
            raise ValueError("Native cache requires identical Core sources and engine")
        provenance[str(previous / "manifest.json")] = file_sha256(
            previous / "manifest.json"
        )
        for path in sorted(previous.glob("simulations/*/receipt.json")):
            receipt = read(path)
            bid = receipt["building_id"]
            if (
                receipt["status"] != "accepted"
                or bid not in rows
                or rows[bid]["status"] != "prepared"
            ):
                continue
            row = rows[bid]
            parameters = {
                name: value
                for name, value in receipt["parameters"].items()
                if name not in row.get("locked_parameters", ())
                and registers[bid]
                .get(name, {})
                .get("status", "screen_and_cohort_calibration")
                == "screen_and_cohort_calibration"
            }
            baseline = BuildingSpec.model_validate(read(root / row["spec_path"]))
            profile = {
                "infiltration_basis": "atemp_area_rate"
                if baseline.airflow.envelope_infiltration_l_s_m2 is not None
                else "core_volume_ach",
                "source": "Frozen effective cohort diagnostic; parameters are inferred assumptions",
                "parameters": parameters,
                "bounds": {k: v["bounds"] for k, v in registers[bid].items()},
            }
            spec = apply_cohort_profile(
                baseline, profile, locked_parameters=row.get("locked_parameters", ())
            )
            old_spec_path = path.parent / "spec.json"
            old_spec = BuildingSpec.model_validate(read(old_spec_path))
            if (
                spec.physical_hash() != receipt["spec_sha256"]
                or old_spec.physical_hash() != spec.physical_hash()
                or spec.climate.weather_sha256 != receipt["weather_sha256"]
            ):
                skipped.append(
                    {
                        "building_id": bid,
                        "reason": "physical_or_weather_identity_changed",
                    }
                )
                continue
            if file_sha256(spec.climate.epw_path) != receipt["weather_sha256"]:
                raise ValueError("Archived weather payload changed")
            result_path = (
                Path(receipt["diagnostics"]["err_path"]).parent / "result.json"
            )
            if not result_path.exists():
                skipped.append(
                    {"building_id": bid, "reason": "native_completion_artifact_missing"}
                )
                continue
            if read(result_path)["scalars"] != receipt["scalars"]:
                raise ValueError(
                    "Receipt scalars differ from native completion artifact"
                )
            key = digest((identity, bid, parameters))
            new_path = root / "simulations" / key / "receipt.json"
            if new_path.exists():
                continue
            provenance[str(path)] = file_sha256(path)
            provenance[str(old_spec_path)] = file_sha256(old_spec_path)
            provenance[str(result_path)] = file_sha256(result_path)
            atomic_json(new_path.parent / "spec.json", spec.model_dump(mode="json"))
            atomic_json(
                new_path,
                receipt
                | {
                    "identity": key,
                    "parameters": parameters,
                    "reused_verified_native_case": {
                        "previous_receipt": str(path),
                        "previous_identity": receipt["identity"],
                        "checks": "identical Core sources, engine, physical spec and weather payload",
                    },
                },
            )
            imported.append(key)
    atomic_json(
        root / "native-cache-import.json",
        {"imported": imported, "skipped": skipped, "inputs": provenance},
    )
    return {"imported": len(imported), "skipped": len(skipped)}
