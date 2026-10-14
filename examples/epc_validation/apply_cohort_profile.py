"""Apply a versioned effective profile to an audited native building specification.

References: docs/cohort-transfer.md; EPSM Data cohorts.assign_cohort and
apply_cohort_profile; GIS-led EPC-validated membership policy v2. This application
step uses no heating target. Provisional, out-of-parent, out-of-domain or unaudited
assignments retain the input parent. Quantitative independently evidenced locks
are preserved. It supplies no new building-accuracy claim.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from epsm_core import BuildingSpec
from epsm_data.cohorts import apply_cohort_profile, assign_cohort

from epsm_workflows.campaign import atomic_json, file_sha256


def apply(
    spec_path, descriptors_path, bundle_path, destination, *, allow_provisional=False
):
    """Write the effective spec and an auditable assignment/fallback receipt."""
    spec_path, descriptors_path, bundle_path = map(
        Path, (spec_path, descriptors_path, bundle_path)
    )
    source = BuildingSpec.model_validate_json(spec_path.read_text())
    descriptors = json.loads(descriptors_path.read_text())
    bundles = json.loads(bundle_path.read_text())
    bundles = bundles if isinstance(bundles, list) else [bundles]
    atemp = descriptors.get("atemp_m2")
    if (
        atemp is None
        or source.load_reference_area_m2 is None
        or not math.isclose(float(atemp), source.load_reference_area_m2, rel_tol=1e-9)
    ):
        raise ValueError(
            "Native load reference area must equal the audited reported Atemp"
        )
    # Native identifier and systems must refer to the same audited building.
    # References: Core BuildingSpec; cohort_transfer.preparation native boundary.
    if source.identifier != descriptors.get("building_id"):
        raise ValueError("Native identifier must equal the audited building ID")
    if source.airflow.topology != descriptors.get("ventilation"):
        raise ValueError("Native ventilation topology must equal the audited parent")
    if (
        source.delivery.heating is None
        or source.delivery.heating.carrier != descriptors.get("heating_family")
    ):
        raise ValueError("Native heating carrier must equal the audited parent")
    membership = descriptors.get("membership", {})
    if (
        not membership.get("eligible")
        or membership.get("policy") != "gis-led-epc-validated-pure-programme-v2"
    ):
        assignment = {
            "status": "parent_fallback",
            "reason": "unaudited_source_membership",
            "profile": None,
        }
    else:
        matching = [
            b
            for b in bundles
            if all(descriptors.get(k) == v for k, v in b["parent"].items())
        ]
        if len(matching) > 1:
            raise ValueError("More than one profile bundle matches the parent")
        assignment = (
            assign_cohort(descriptors, matching[0], allow_provisional=allow_provisional)
            if matching
            else {
                "status": "parent_fallback",
                "reason": "outside_parent",
                "profile": None,
            }
        )
    locked = descriptors.get("locked_parameters", [])
    profile = assignment.get("profile")
    effective = (
        apply_cohort_profile(source, profile, locked_parameters=locked)
        if profile is not None
        else source
    )
    destination = Path(destination)
    atomic_json(destination / "spec.json", effective.model_dump(mode="json"))
    receipt = {
        "building_id": descriptors.get("building_id"),
        "assignment": assignment,
        "allow_provisional": allow_provisional,
        "locked_parameters": locked,
        "baseline_spec_sha256": source.physical_hash(),
        "effective_spec_sha256": effective.physical_hash(),
        "inputs": {
            str(p.resolve()): file_sha256(p)
            for p in (spec_path, descriptors_path, bundle_path)
        },
        "heating_target_used": False,
        "accuracy_independently_validated_here": False,
    }
    atomic_json(destination / "assignment.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--descriptors", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-provisional", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            apply(
                args.spec,
                args.descriptors,
                args.bundle,
                args.output,
                allow_provisional=args.allow_provisional,
            ),
            indent=2,
        )
    )
