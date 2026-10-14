"""Target-free component histories for the experimental MF stock comparison.

These histories are sensitivity priors, not identified renovation histories. They
start from original construction references. Effective parents fitted to surviving
stock must be evaluated separately, otherwise renovation is counted twice.
"""

# JSON validation reports malformed field types and values through one interface.
# ruff: noqa: TRY004

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = "mf-stock-history/1"
COMPONENT_MEANS = {"window": 35.0, "roof": 40.0, "facade": 50.0}
DEFAULT_CONTROLS = {
    "lifetime_multiplier": 1.0,
    "roof_upgrade_probability": 0.5,
    "facade_upgrade_probability": 0.5,
}
COMPONENT_ALIASES = {"windows": "window", "roof_covering": "roof", "wall": "facade"}
_SE02_ORIGINAL_RECORDS = {
    "assembly": (
        "layered/assemblies.json",
        "assemblies",
        "v1_mf_se02_existing_window",
        "43a38a2d82b67042654a2241be89a9fa493d3fdc59dc0da61449fe23410843bf",
    ),
    "material": (
        "layered/materials.json",
        "materials",
        "window_lod2_mf_se02_existing",
        "5a3814ef18aa9fb703b825a21b4a55a90d8c4e78450415590df7a4759576d934",
    ),
    "thermal": (
        "layered/honeybee.json",
        "materials",
        "EPSM_window_lod2_mf_se02_existing",
        "f34701bbe8c6065f8c58085f91439b264c1d3094d876f2aaa1a6556a44a0648d",
    ),
}
_SE02_CLAIM_FIELDS = (
    "condition",
    "basis",
    "assembly_id",
    "source_recipe",
    "previous_original_construction_reference_u_w_m2k",
    "selected_value",
    "decision",
    "source",
)
_SE02_CLAIM_SHA256 = "4c28395e3b2a02735215c25afdcbc578d36e9424da8567070c3166bcfd3dd499"


def _content_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _data_document(relative):
    return json.loads(files("epsm_data.datasets").joinpath(relative).read_text())


def _data_source_hashes(relatives):
    result = {}
    for relative in sorted(set(relatives)):
        resource = files("epsm_data.datasets").joinpath(relative)
        result[str(Path(str(resource)).resolve())] = hashlib.sha256(
            resource.read_bytes()
        ).hexdigest()
    return result


def _historical_se02_window():
    """Restore the preserved construction reference, never the fitted stock U.

    Record hashes deliberately require review when the source chain changes.
    They exclude unrelated archive evaluation metrics from the evidence claim.
    """
    records, hashes = {}, {}
    for role, (path, group, key, expected) in _SE02_ORIGINAL_RECORDS.items():
        record = _data_document(path)[group][key]
        checksum = _content_sha256(record)
        if checksum != expected:
            raise ValueError(
                f"Reviewed historical-window source content changed: {path}#/{group}/{key}"
            )
        records[role] = record
        hashes[f"{path}#/{group}/{key}"] = checksum
    path = "layered/evidence/mf_se02_window_v1_4_diagnostic.json"
    claim = _data_document(path)["claim"]
    reviewed_claim = {field: claim.get(field) for field in _SE02_CLAIM_FIELDS}
    checksum = _content_sha256(reviewed_claim)
    if checksum != _SE02_CLAIM_SHA256:
        raise ValueError(
            "Reviewed historical-window source content changed: preserved-original evidence claim"
        )
    hashes[f"{path}#/claim:construction_reference_fields"] = checksum
    assembly, material, thermal = (
        records[name] for name in ("assembly", "material", "thermal")
    )
    if (
        assembly["role"] != "WINDOW"
        or assembly["material_id"] != _SE02_ORIGINAL_RECORDS["material"][2]
        or material["honeybee_id"] != _SE02_ORIGINAL_RECORDS["thermal"][2]
        or thermal["type"] != "EnergyWindowMaterialSimpleGlazSys"
        or thermal["u_factor"]
        != claim["previous_original_construction_reference_u_w_m2k"]
    ):
        raise ValueError("Preserved historical-window source chain is inconsistent")
    return {
        "u_value_w_m2k": thermal["u_factor"],
        "solar_heat_gain_coefficient": thermal["shgc"],
        "visible_transmittance": thermal["vt"],
        "source": "Preserved EPSM Data v1_mf_se02_existing_window original assembly and mf_se02_window_v1_4_diagnostic historical-reference evidence",
        "source_content_sha256": hashes,
        "source_hashes": _data_source_hashes(
            [record[0] for record in _SE02_ORIGINAL_RECORDS.values()]
            + [path, "parametric/catalogue.json"]
        ),
        "excluded_stock_profile": {
            "assembly_id": claim["assembly_id"],
            "u_value_w_m2k": claim["selected_value"],
            "condition": claim["condition"],
            "reason": "Retrospective surviving-stock hypothesis is not original construction or an installation-period profile",
        },
    }


def _finite_number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"Invalid {name}")
    return float(value)


def _year(value, name):
    number = _finite_number(value, name, positive=True)
    if not number.is_integer():
        raise ValueError(f"{name} must be an integer year")
    return int(number)


def validate_controls(controls=None):
    """Validate the three shared history controls; do not fit per-building values."""
    controls = dict(controls or {})
    if set(controls) - set(DEFAULT_CONTROLS):
        raise ValueError(
            f"Unknown history controls: {set(controls) - set(DEFAULT_CONTROLS)}"
        )
    values = DEFAULT_CONTROLS | controls
    for key, value in values.items():
        values[key] = _finite_number(value, key)
    if not 0.75 <= values["lifetime_multiplier"] <= 1.25:
        raise ValueError("lifetime_multiplier must be between 0.75 and 1.25")
    for key in ("roof_upgrade_probability", "facade_upgrade_probability"):
        if not 0 <= values[key] <= 1:
            raise ValueError(f"{key} must be between 0 and 1")
    return values


def engineering_prior(controls=None):
    """Versioned, explicitly experimental defaults for the comparison pilot."""
    return {
        "schema_version": SCHEMA_VERSION,
        "classification": "engineering_sensitivity_prior",
        "status": "experimental_not_catalogue_defaults",
        "controls": validate_controls(controls),
        "components": {
            component: {
                "mean_years": mean,
                "weibull_shape": 3.0,
                "weibull_scale_years": mean / math.gamma(1 + 1 / 3),
                "source": "Accepted MF comparison engineering sensitivity prior",
                "classification": "engineering_assumption",
            }
            for component, mean in COMPONENT_MEANS.items()
        },
        "bounds": {
            "lifetime_multiplier": [0.75, 1.25],
            "roof_upgrade_probability": [0, 1],
            "facade_upgrade_probability": [0, 1],
        },
        "thermal_material_degradation": 0.0,
        "lighting_changes": "inactive",
        "energy_conditioning": False,
    }


def weibull_scale(mean_years, shape=3.0):
    """Convert the mean lifetime to Weibull scale, rather than using it as scale."""
    mean = _finite_number(mean_years, "mean_years", positive=True)
    shape = _finite_number(shape, "shape", positive=True)
    return mean / math.gamma(1 + 1 / shape)


def _uniform(identity, seed, draw, component, cycle, purpose):
    # Counter-based draws preserve common random numbers across parameter choices,
    # row order, processes and component-specific changes. No Python hash is used.
    payload = json.dumps(
        [identity, seed, draw, component, cycle, purpose],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    integer = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    # Midpoints on a 52-bit grid remain representable strictly between 0 and 1.
    return ((integer >> 12) + 0.5) / (2**52)


def _validate_target_free(row):
    forbidden = []
    for key in row:
        text = str(key).lower()
        if (
            text.startswith(("target", "residual", "prediction_error", "measured_heat"))
            or "energy" in text
            or "demand" in text
            or ("kwh" in text and ("heat" in text or "observed" in text))
            or text in {"heating_kwh", "heating_kwh_m2", "heat_kwh_m2", "performance"}
        ):
            forbidden.append(key)
    if forbidden:
        raise ValueError(
            f"History assignment cannot receive energy or residual fields: {forbidden}"
        )


def _profiles(values, role):
    if values is None:
        return []
    if isinstance(values, Mapping):
        values = [values] if "profile_id" in values else list(values.values())
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError(f"{role} profiles must be a sequence of reviewed records")
    result = []
    for item in values:
        if not isinstance(item, Mapping):
            raise ValueError(f"Invalid {role} profile")
        item = dict(item)
        if (
            not str(item.get("profile_id", "")).strip()
            or not str(item.get("source", "")).strip()
        ):
            raise ValueError(f"{role} profile requires an identity and source")
        if item.get("classification") not in {
            "measured",
            "source_derived",
            "engineering_assumption",
            "engineering_proxy",
        }:
            raise ValueError(
                f"{role} profile requires a declared evidence classification"
            )
        item["valid_from_year"] = _year(item.get("valid_from_year"), "valid_from_year")
        if item.get("valid_through_year") is not None:
            item["valid_through_year"] = _year(
                item["valid_through_year"], "valid_through_year"
            )
            if item["valid_through_year"] < item["valid_from_year"]:
                raise ValueError("Profile validity years are reversed")
        if role == "window":
            if item.get("condition") in {
                "representative_surviving_stock_window",
                "effective_surviving_stock",
                "calibrated_surviving_stock",
            } or item.get("construction_reference_basis") in {
                "effective_surviving_stock",
                "calibrated_surviving_stock",
            }:
                raise ValueError(
                    "Window history profiles require original construction, not surviving-stock assumptions"
                )
            item["u_value_w_m2k"] = _finite_number(
                item.get("u_value_w_m2k"), "whole-window U", positive=True
            )
            if item.get("definition") != "whole_window_including_frame":
                raise ValueError(
                    "Window profiles require whole-window U including frame"
                )
            if not str(item.get("installation_period_basis", "")).strip():
                raise ValueError(
                    "Window profiles require a declared installation-period basis"
                )
            for name in ("solar_heat_gain_coefficient", "visible_transmittance"):
                if name in item:
                    item[name] = _finite_number(item[name], name)
                    if not 0 <= item[name] <= 1:
                        raise ValueError(f"Invalid {name}")
        result.append(item)
    if len({item["profile_id"] for item in result}) != len(result):
        raise ValueError(f"Duplicate {role} profile identities")
    return sorted(
        result, key=lambda item: (item["valid_from_year"], item["profile_id"])
    )


@lru_cache(maxsize=1)
def _library_window_profiles():
    from epsm_data import __version__, resolve_building_archetype
    from epsm_data.parametric import ERAS

    profiles = []
    claims = _data_document("parametric/catalogue.json")["claim_metadata"]
    for era, (start, end) in ERAS.items():
        native = resolve_building_archetype(
            category="multi_family",
            construction_year=1940 if era == "SE.01" else start,
            climate_zone="SE.Zone3",
            ventilation="F",
            default_state="existing",
        )
        glazing = native["glazing"]
        claim_key = f"multi_family:{era}:existing:window"
        claim = claims[claim_key]
        historical = None
        if era == "SE.02":
            historical = _historical_se02_window()
            if (
                claim.get("condition") != "representative_surviving_stock_window"
                or claim.get("basis") != "user_adopted_retrospective_window_hypothesis"
                or claim.get("previous_original_construction_reference_u_w_m2k")
                != historical["u_value_w_m2k"]
                or claim.get("selected_value")
                != historical["excluded_stock_profile"]["u_value_w_m2k"]
                or glazing.u_value_w_m2k
                != historical["excluded_stock_profile"]["u_value_w_m2k"]
                or claim.get("assembly_id")
                != historical["excluded_stock_profile"]["assembly_id"]
            ):
                raise ValueError(
                    "Current SE.02 stock window changed; historical separation requires review"
                )
        elif claim.get("condition") != "original_construction":
            raise ValueError(
                f"Window history library requires original construction: {era}"
            )
        profile = {
            "profile_id": f"mf_window_period_proxy_{era}",
            "era": era,
            "valid_from_year": start,
            "valid_through_year": end,
            "u_value_w_m2k": glazing.u_value_w_m2k,
            "solar_heat_gain_coefficient": glazing.solar_heat_gain_coefficient,
            "visible_transmittance": glazing.visible_transmittance,
            "definition": "whole_window_including_frame",
            "source": f"EPSM Data {__version__} original MF construction-era window library",
            "classification": "engineering_proxy",
            "construction_reference_basis": "historical_original_construction",
            "source_content_sha256": {
                f"parametric/catalogue.json#/claim_metadata/{claim_key}": _content_sha256(
                    claim
                ),
            },
            "source_hashes": _data_source_hashes(
                [
                    "parametric/catalogue.json",
                    "layered/archetypes.json",
                    "layered/assemblies.json",
                    "layered/materials.json",
                    "layered/honeybee.json",
                ]
            ),
            "installation_period_basis": (
                "Original construction-era windows used as an installation-period proxy; "
                "not measured retrofit performance or a retrofit requirement"
            ),
        }
        if historical:
            profile.update(historical)
        profiles.append(profile)
    return tuple(profiles)


def library_window_profiles():
    """Get independent copies of the explicitly labelled Data period proxies."""
    return deepcopy(list(_library_window_profiles()))


def original_window_profile(construction_year):
    """Historical whole-window reference for preparing original-assembly specs.

    This does not change EPSM Data's current surviving-stock parent assumptions.
    Use separately from calibrated/effective parents and explicit observations.
    """
    construction_year = _year(construction_year, "construction_year")
    profile = _eligible_profile(
        _library_window_profiles(), construction_year, construction_year
    )
    if profile is None:
        raise ValueError("No declared historical window covers original construction")
    return deepcopy(profile)


@lru_cache(maxsize=6)
def _original_component_claims(era):
    from epsm_data.parametric import ERAS

    if era not in ERAS:
        raise ValueError(f"Unknown MF construction era: {era}")
    claims = _data_document("parametric/catalogue.json")["claim_metadata"]
    assemblies = _data_document("layered/assemblies.json")["assemblies"]
    source_hashes = _data_source_hashes(
        [
            "parametric/catalogue.json",
            "layered/archetypes.json",
            "layered/assemblies.json",
            "layered/materials.json",
            "layered/honeybee.json",
        ]
    )
    result = {}
    for role in (
        "roof",
        "external_wall",
        "intermediate_floor",
        "internal_wall",
        "ground_floor",
        "basement_wall",
        "window",
    ):
        key = f"multi_family:{era}:existing:{role}"
        claim = claims[key]
        record_hashes = {
            f"parametric/catalogue.json#/claim_metadata/{key}": _content_sha256(claim)
        }
        if role == "window":
            year = 1940 if era == "SE.01" else ERAS[era][0]
            window = original_window_profile(year)
            result[role] = {
                "condition": "original_construction",
                "source_declared_condition": claim.get("condition"),
                "construction_reference_basis": window["construction_reference_basis"],
                "basis": "preserved_historical_original_reference"
                if era == "SE.02"
                else claim["basis"],
                "classification": "engineering_proxy",
                "nominal_u_value_w_m2k": window["u_value_w_m2k"],
                "selected_value_w_m2k": window["u_value_w_m2k"],
                "boundary": "whole_window",
                "value_kind": "historical_whole_window_reference",
                "source": window["source"],
                "assembly_id": (
                    _SE02_ORIGINAL_RECORDS["assembly"][2]
                    if era == "SE.02"
                    else claim["assembly_id"]
                ),
                "source_content_sha256": record_hashes
                | window["source_content_sha256"],
                "source_hashes": source_hashes | window["source_hashes"],
                "excluded_stock_profile": window.get("excluded_stock_profile"),
            }
            continue
        semantic_fields = " ".join(
            str(claim.get(field, ""))
            for field in ("condition", "basis", "construction_basis")
        ).lower()
        if claim.get("condition") != "original_construction" or any(
            text in semantic_fields
            for text in ("surviving", "retrospective", "calibrated", "effective_stock")
        ):
            raise ValueError(
                f"Original component reference cannot use surviving-stock/calibrated modification: {era}/{role}"
            )
        effective = (
            str(claim.get("boundary", "")).startswith("effective_")
            or claim.get("value_kind") == "legacy_effective_ground_reference"
        )
        if effective and role not in {"ground_floor", "basement_wall"}:
            raise ValueError(
                f"Effective boundary is only permitted for ground references: {era}/{role}"
            )
        if effective and (
            claim.get("boundary")
            not in {"effective_slab_on_ground", "effective_below_ground_wall"}
            or claim.get("value_kind") != "legacy_effective_ground_reference"
        ):
            raise ValueError(f"Unreviewed effective ground boundary: {era}/{role}")
        assembly_id = claim["assembly_id"]
        record_hashes[f"layered/assemblies.json#/assemblies/{assembly_id}"] = (
            _content_sha256(assemblies[assembly_id])
        )
        result[role] = {
            "condition": claim["condition"],
            "construction_reference_basis": "historical_original_construction",
            "basis": claim["basis"],
            "classification": claim["classification"],
            "nominal_u_value_w_m2k": claim["nominal_u_value"],
            "selected_value_w_m2k": claim["selected_value"],
            "boundary": claim["boundary"],
            "value_kind": claim["value_kind"],
            "effective_ground_boundary_reference": effective,
            "source": claim["source"],
            "source_recipe": claim.get("source_recipe"),
            "assembly_id": assembly_id,
            "confidence": claim.get("confidence"),
            "source_content_sha256": record_hashes,
            "source_hashes": source_hashes,
        }
    return result


def original_component_claims(era):
    """Audit original MF fabric claims, preserving ground-boundary distinctions.

    Effective slab/basement boundary targets are permitted with their declared
    original assemblies; they are never treated as renovations or nominal U.
    Explicit surviving-stock or calibrated opaque substitutions require review.
    """
    return deepcopy(_original_component_claims(era))


def _eligible_profile(profiles, year, construction_year):
    year = math.floor(year)
    choices = [
        item
        for item in profiles
        if item["valid_from_year"] <= year
        and (
            item.get("valid_through_year") is None or year <= item["valid_through_year"]
        )
        and (
            item.get("construction_year_min") is None
            or construction_year >= item["construction_year_min"]
        )
        and (
            item.get("construction_year_max") is None
            or construction_year <= item["construction_year_max"]
        )
    ]
    return choices[-1] if choices else None


def _component_priors(component_priors):
    priors = engineering_prior()["components"]
    for component, supplied in (component_priors or {}).items():
        component = COMPONENT_ALIASES.get(component, component)
        if component not in priors or not isinstance(supplied, Mapping):
            raise ValueError("Unknown component lifetime prior")
        if not str(supplied.get("source", "")).strip():
            raise ValueError("Published lifetime priors require a source")
        if supplied.get("classification") not in {
            "source_derived",
            "engineering_assumption",
            "measured",
        }:
            raise ValueError("Lifetime priors require an evidence classification")
        mean = _finite_number(supplied.get("mean_years"), "mean_years", positive=True)
        shape = _finite_number(
            supplied.get("weibull_shape", 3), "weibull_shape", positive=True
        )
        priors[component] = dict(supplied) | {
            "mean_years": mean,
            "weibull_shape": shape,
            "weibull_scale_years": weibull_scale(mean, shape),
        }
    return priors


def _evidence(row, construction_year, measurement_year):
    accepted, ignored = {}, []
    for role, records in (row.get("component_evidence") or {}).items():
        role = COMPONENT_ALIASES.get(role, role)
        if role not in COMPONENT_MEANS:
            raise ValueError(f"Unsupported component evidence: {role}")
        records = [records] if isinstance(records, Mapping) else records
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise ValueError("Component evidence must contain dated records")
        for item in records:
            if not isinstance(item, Mapping):
                raise ValueError("Invalid component evidence")
            item = dict(item)
            year = _year(
                item.get("installation_year", item.get("valid_from_year")),
                "installation_year",
            )
            if year > measurement_year:
                ignored.append(
                    {
                        "component": role,
                        "installation_year": year,
                        "reason": "future_evidence",
                    }
                )
                continue
            if year < construction_year:
                raise ValueError("Component evidence predates construction")
            if not str(item.get("source", "")).strip() or item.get(
                "classification"
            ) not in {"measured", "source_derived"}:
                ignored.append(
                    {
                        "component": role,
                        "installation_year": year,
                        "reason": "insufficient_independent_evidence",
                    }
                )
                continue
            item["installation_year"] = year
            accepted.setdefault(role, []).append(item)
    for records in accepted.values():
        records.sort(key=lambda item: item["installation_year"])
    return accepted, ignored


def _state_key(state):
    payload = json.dumps(state, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return "state_" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def generate_histories(
    row,
    controls=None,
    *,
    draws=100,
    seed=20261008,
    upgrade_profiles=None,
    window_profiles=None,
    component_priors=None,
):
    """Generate reusable, target-free component histories and compressed states.

    ``upgrade_profiles`` contains reviewed ``roof``/``facade`` period mappings.
    Each needs profile_id, valid_from_year, source and classification. No supplied
    mapping means no thermal upgrade, even when a refurbishment is sampled.
    Independently sourced ``component_evidence`` may anchor installation dates.
    Raw optional renovation flags have no effect on assignments.
    """
    _validate_target_free(row)
    if isinstance(draws, bool) or not isinstance(draws, int) or not 1 <= draws <= 10000:
        raise ValueError("draws must be an integer between 1 and 10000")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    identity = next(
        (
            str(row[k]).strip()
            for k in ("building_id", "identity", "property_key")
            if row.get(k)
        ),
        "",
    )
    if not identity:
        raise ValueError("A stable building identity is required")
    construction_year = _year(row.get("construction_year"), "construction_year")
    measurement_year = _year(row.get("measurement_year"), "measurement_year")
    if measurement_year < construction_year:
        raise ValueError("Measurement year predates construction")
    if row.get("parent_basis", "original_construction") != "original_construction":
        raise ValueError(
            "Dynamic histories require original construction references, not calibrated surviving-stock parents"
        )
    controls = validate_controls(controls)
    priors = _component_priors(component_priors)
    window_profiles = _profiles(
        library_window_profiles() if window_profiles is None else window_profiles,
        "window",
    )
    original_window = _eligible_profile(
        window_profiles, construction_year, construction_year
    )
    if original_window is None:
        raise ValueError("No declared window profile covers original construction")
    upgrade_profiles = dict(upgrade_profiles or {})
    if set(upgrade_profiles) - {"roof", "facade", "wall"}:
        raise ValueError("Unknown thermal upgrade role")
    if "facade" in upgrade_profiles and "wall" in upgrade_profiles:
        raise ValueError("Supply facade or wall profiles, not both")
    upgrades = {
        "roof": _profiles(upgrade_profiles.get("roof"), "roof"),
        "facade": _profiles(
            upgrade_profiles.get("facade", upgrade_profiles.get("wall")), "facade"
        ),
    }
    evidence, ignored_evidence = _evidence(row, construction_year, measurement_year)
    histories, grouped = [], {}
    for draw in range(draws):
        state = {
            "window_era": original_window.get("era"),
            "window_u_value_w_m2k": original_window["u_value_w_m2k"],
            "window_profile_id": original_window["profile_id"],
            "window_solar_heat_gain_coefficient": original_window.get(
                "solar_heat_gain_coefficient"
            ),
            "window_visible_transmittance": original_window.get(
                "visible_transmittance"
            ),
            "roof_profile_id": "original",
            "wall_profile_id": "original",
        }
        installation_years = dict.fromkeys(COMPONENT_MEANS, float(construction_year))
        events = {}
        for component, prior in priors.items():
            component_events, elapsed, cycle = [], float(construction_year), 0
            anchors = list(evidence.get(component, []))
            scale = prior["weibull_scale_years"] * controls["lifetime_multiplier"]
            while True:
                if cycle > 2000:
                    raise ValueError("Lifetime prior produces too many service cycles")
                u = _uniform(identity, seed, draw, component, cycle, "lifetime")
                proposed = elapsed + scale * (-math.log1p(-u)) ** (
                    1 / prior["weibull_shape"]
                )
                anchor = (
                    anchors[0]
                    if anchors and anchors[0]["installation_year"] <= proposed
                    else None
                )
                event_year = float(anchor["installation_year"]) if anchor else proposed
                if event_year > measurement_year:
                    break
                if anchor:
                    anchors.pop(0)
                event = {
                    "year": event_year,
                    "cycle": cycle,
                    "basis": "independent_component_evidence"
                    if anchor
                    else "sampled_service_life",
                    "source": anchor["source"] if anchor else prior["source"],
                }
                if component == "window":
                    profile = _eligible_profile(
                        window_profiles, event_year, construction_year
                    )
                    if anchor and "u_value_w_m2k" in anchor:
                        if anchor.get("definition") != "whole_window_including_frame":
                            raise ValueError(
                                "Observed window U requires a whole-window definition"
                            )
                        profile = dict(profile or original_window) | {
                            "profile_id": "observed_window_"
                            + hashlib.sha256(
                                json.dumps(anchor, sort_keys=True).encode()
                            ).hexdigest()[:12],
                            "u_value_w_m2k": _finite_number(
                                anchor["u_value_w_m2k"],
                                "observed window U",
                                positive=True,
                            ),
                        }
                    event["action"] = "replacement"
                    if profile and (
                        anchor
                        and "u_value_w_m2k" in anchor
                        or profile["u_value_w_m2k"] < state["window_u_value_w_m2k"]
                    ):
                        for target, source in (
                            ("window_era", "era"),
                            ("window_u_value_w_m2k", "u_value_w_m2k"),
                            ("window_profile_id", "profile_id"),
                            (
                                "window_solar_heat_gain_coefficient",
                                "solar_heat_gain_coefficient",
                            ),
                            ("window_visible_transmittance", "visible_transmittance"),
                        ):
                            state[target] = profile.get(source)
                        event["thermal_effect"] = (
                            "installation_period_proxy"
                            if not anchor
                            else "evidence_selected_window"
                        )
                        event["profile_id"] = profile["profile_id"]
                    else:
                        event["thermal_effect"] = "replacement_like_for_like"
                        event["profile_id"] = state["window_profile_id"]
                else:
                    probability = controls[f"{component}_upgrade_probability"]
                    requested = (
                        _uniform(
                            identity, seed, draw, component, cycle, "energy_upgrade"
                        )
                        < probability
                    )
                    profile = _eligible_profile(
                        upgrades[component], event_year, construction_year
                    )
                    if anchor:
                        requested = bool(anchor.get("profile_id"))
                        profile = next(
                            (
                                p
                                for p in upgrades[component]
                                if p["profile_id"] == anchor.get("profile_id")
                                and _eligible_profile(
                                    [p], event_year, construction_year
                                )
                            ),
                            None,
                        )
                    event["action"] = (
                        "covering_replacement"
                        if component == "roof"
                        else "facade_refurbishment"
                    )
                    if requested and profile:
                        state[
                            "roof_profile_id"
                            if component == "roof"
                            else "wall_profile_id"
                        ] = profile["profile_id"]
                        event["thermal_effect"] = "energy_upgrade"
                        event["profile_id"] = profile["profile_id"]
                    else:
                        event["thermal_effect"] = (
                            "replacement_like_for_like"
                            if component == "roof"
                            else "maintenance"
                        )
                        event["upgrade_mapping_available"] = profile is not None
                        event["upgrade_requested"] = requested
                installation_years[component] = event_year
                component_events.append(event)
                elapsed, cycle = event_year, cycle + 1
            events[component] = component_events
        key = _state_key(state)
        histories.append(
            {
                "draw": draw,
                "weight": 1 / draws,
                "state_key": key,
                "state": dict(state),
                "installation_years": installation_years,
                "events": events,
            }
        )
        group = grouped.setdefault(
            key,
            {
                "state_key": key,
                "state": dict(state),
                "draw_indices": [],
                "installation_years": {role: [] for role in COMPONENT_MEANS},
            },
        )
        group["draw_indices"].append(draw)
        for role, year in installation_years.items():
            group["installation_years"][role].append(year)
    states = []
    for key in sorted(grouped):
        item = grouped[key]
        item["weight"] = len(item["draw_indices"]) / draws
        states.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "assignment": {
            "building_id": identity,
            "construction_year": construction_year,
            "measurement_year": measurement_year,
            "parent_basis": "original_construction",
            "source_start_year": _year(
                row.get("source_start_year", construction_year), "source_start_year"
            ),
            "source_start_year_basis": row.get(
                "source_start_year_basis",
                "Reported construction year; original assembly is a catalogue representative",
            ),
            "target_free": True,
            "observed_renovation_history": False,
        },
        "provenance": {
            "prior": engineering_prior(controls) | {"components": priors},
            "seed": seed,
            "draws": draws,
            "random_numbers": "SHA256 counter-based common random numbers",
            "window_profiles": window_profiles,
            "upgrade_profiles": upgrades,
            "supported_component_evidence": evidence,
            "ignored_evidence": ignored_evidence,
            "optional_renovation_flags_used": False,
            "unidentifiable_components": [
                role for role in ("roof", "facade") if not upgrades[role]
            ],
            "history_timing_boundary": "Component state at the nominal measurement-year endpoint; intra-year annual load effects are unresolved",
        },
        "histories": histories,
        "states": states,
    }


def weighted_predictions(states, predictions):
    """Integrate native state predictions without renormalising failures away.

    ``predictions`` is a state-key mapping or a sequence aligned to ``states``.
    Complete ensembles return expected heating and the weighted state spread.
    A missing/invalid native result retains its probability mass and makes the
    ensemble unavailable for comparative scoring.
    """
    if isinstance(states, Mapping):
        states = states["states"]
    if not states:
        raise ValueError("At least one weighted state is required")
    weights = [
        _finite_number(item.get("weight"), "state weight", positive=True)
        for item in states
    ]
    if not math.isclose(sum(weights), 1.0, abs_tol=1e-10):
        raise ValueError("State weights must sum to one")
    if len({item["state_key"] for item in states}) != len(states):
        raise ValueError("Duplicate weighted state identities")
    if isinstance(predictions, Mapping):
        values = [predictions.get(item["state_key"]) for item in states]
    else:
        values = list(predictions)
        if len(values) != len(states):
            raise ValueError("Prediction sequence must align with all states")
    valid, failed = [], []
    for item, weight, value in zip(states, weights, values, strict=True):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            failed.append(item["state_key"])
        else:
            valid.append((float(value), weight))
    successful_weight = sum(weight for _, weight in valid)
    result = {
        "status": "incomplete" if failed else "complete",
        "prediction": None
        if failed
        else sum(value * weight for value, weight in valid),
        "successful_weight": successful_weight,
        "missing_weight": max(0.0, 1.0 - successful_weight),
        "failed_state_keys": failed,
        "weighted_p05": None,
        "weighted_p50": None,
        "weighted_p95": None,
    }
    if not failed:
        ordered = sorted(valid)
        for q, name in (
            (0.05, "weighted_p05"),
            (0.5, "weighted_p50"),
            (0.95, "weighted_p95"),
        ):
            cumulative = 0.0
            for value, weight in ordered:
                cumulative += weight
                if cumulative + 1e-12 >= q:
                    result[name] = value
                    break
    return result
