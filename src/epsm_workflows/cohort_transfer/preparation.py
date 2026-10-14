"""Freeze existing observations, native baseline specs and measurement-year AMYs.

References: reviewed EPSM extraction claims, EPC energy-boundary trace,
Core Sveby resources and smhi2epw provenance contract. Missing physical inputs
are declared assumptions; numerical or ambiguous-target failures remain visible.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..campaign import atomic_json, file_sha256
from .common import DEFAULTS, NUMERIC, PARENT, cleaned, digest, read, source_identity
from .parameters import applicable


def mechanical_flow_completion(
    category, design, normal_use_flow, *, zero_policy="retain_reported"
):
    """Explicit MF zero-report policy; neither completion is measured operation."""
    if zero_policy not in {"retain_reported", "normal_use"}:
        raise ValueError("Unknown MF reported-zero flow policy")
    if design is None:
        return normal_use_flow, "normal_use_completion_assumption"
    if category == "multi_family" and design == 0 and zero_policy == "normal_use":
        return normal_use_flow, "reported_zero_normal_use_completion_assumption"
    return design, "reported_design_completion_assumption"


def normal_use_dates(year, count, *, summer_closure=False):
    """Synthetic normal-use date placement, never an observed school calendar.

    Select evenly spaced weekdays to conserve Sveby's annual day count. The
    six-week school closure is an explicitly labelled alternative scenario.
    """
    days = []
    current = date(year, 1, 1)
    while current.year == year:
        closed = summer_closure and date(year, 6, 24) <= current <= date(year, 8, 4)
        if current.weekday() < 5 and not closed:
            days.append(current)
        current += timedelta(days=1)
    if count > len(days):
        raise ValueError("Requested normal-use active days exceed available weekdays")
    return [
        days[i].isoformat() for i in np.linspace(0, len(days) - 1, count).astype(int)
    ]


def prepare_footprint(row):
    """Return bounded Core preparation plus source/cleaned geometry evidence."""
    from epsm_core.preparation import prepare_geometry
    from shapely import from_wkb

    original = row.get("source_geometry_wkb_hex", row["geometry_wkb_hex"])
    result = prepare_geometry(from_wkb(bytes.fromhex(original)))
    receipt = result.artifact(
        source_crs="EPSG:3006",
        target_crs="EPSG:3006",
        transformation="identity; supplied footprint already in metric EPSG:3006",
    )
    return result, receipt


def roof_model_inputs(row):
    """Keep roof evidence distinct from the declared unclear-to-flat fallback."""
    source = row.get("roof_form")
    if source in ("flat", "pitched_ridge"):
        return {
            "modelled_roof": "flat" if source == "flat" else "gable",
            "roof_fallback_applied": False,
            "roof_assumption": None,
        }
    if source != "unclear":
        raise ValueError("Unknown or unsupported roof form")
    if row.get("height_status") != "determined":
        raise ValueError("height_not_determined")
    measured = row.get("representative_height_m")
    if measured is None or not np.isfinite(measured) or measured <= 0:
        raise ValueError(
            "missing_or_invalid_representative_height_for_flat_roof_fallback"
        )
    return {
        "modelled_roof": "flat",
        "roof_fallback_applied": True,
        "roof_assumption": {
            "policy": "unclear_to_flat_v1",
            "source_roof_form": source,
            "reason": "Unresolved measured roof classification; declared flat completion scenario",
            "height_source": "representative_height_m",
            "height_m": measured,
            "topology": "compact",
            "limitations": "Actual roof area, attic buffer and conditioned height may differ",
        },
    }


def build_geometry(row, config):
    """Share bounded footprint preparation between preflight and annual baseline."""
    from epsm_core import GeometrySpec, RoofSpec

    basement = row.get("heated_basement_floors")
    if basement not in (0, 1):
        raise ValueError("Unknown or multiple heated basement floors")
    storeys = row.get("storeys")
    if (
        isinstance(storeys, bool)
        or storeys is None
        or not np.isfinite(storeys)
        or int(storeys) != storeys
        or not 1 <= storeys <= 100
    ):
        raise ValueError("Unknown or invalid storey count")
    roof_inputs = roof_model_inputs(row)
    height = (
        row["representative_height_m"] / storeys
        if roof_inputs["roof_fallback_applied"]
        else row.get("floor_height_m")
    )
    if height is None:
        total = row.get("height_m", row.get("eave_height_m"))
        if total is None:
            raise ValueError("Missing measured height")
        height = total / storeys
    if not np.isfinite(height) or height <= 0:
        raise ValueError("Invalid measured floor height")
    shape = roof_inputs["modelled_roof"]
    roof = (
        RoofSpec(shape="flat", topology="compact")
        if shape == "flat"
        else RoofSpec(shape="gable", topology="cold_attic", pitch_deg=30)
    )
    prepared, _ = prepare_footprint(row)
    if prepared.status != "accepted":
        raise ValueError(
            f"footprint_preparation_{prepared.status}: {'; '.join(prepared.reasons)}"
        )
    geometry = GeometrySpec.from_footprint(
        prepared.cleaned_geometry,
        storey_elevations_m=tuple(height * i for i in range(int(storeys) + 1)),
        source_crs="EPSG:3006",
        roof=roof,
    )
    geometry = geometry.updated(
        parts=tuple(
            part.updated(
                outline_kind="facade"
                if row.get("outline") == "Fasad"
                else "roof_edge_proxy",
                outline_reviewed=False,
            )
            for part in geometry.parts
        )
    )
    if basement:
        geometry = geometry.updated(
            basement_depth_m=config.get("basement_depth_m", 2.5),
            basement_conditioned=True,
        )
    return geometry


def build_baseline(row, config):
    """Compose any supported matrix use/era using existing metric footprint/height.

    Basement depth, recovery, unclear-to-flat roofs and unknown operation are
    explicit completion assumptions. Atemp scales loads once; measured heights
    remain fixed and footprint cleanup is bounded and audited.
    Mixed carriers need an explicit native spec rather than guessed allocation.
    """
    from epsm_core import (
        AirflowSpec,
        BuildingSpec,
        DeliverySpec,
        FacadeSpec,
        FoundationSpec,
        HeatingDeliverySpec,
        ScheduleSpec,
        UseSpec,
        load_sveby_office_v1,
        load_sveby_school_v1,
        load_sveby_spec,
    )
    from epsm_core.weather import climate_from_epw
    from epsm_data.application import resolve_building_archetype

    category = row["use_group"]
    if category not in ("single_family", "multi_family", "office", "school"):
        raise ValueError("Unsupported mixed programme; native programme spec required")
    geometry = build_geometry(row, config)
    ventilation = row["ventilation"]
    classification = resolve_building_archetype(
        category=category,
        construction_year=int(row["construction_year"]),
        climate_zone=row.get(
            "catalogue_climate_zone", config["catalogue_climate_zone"]
        ),
        ventilation=ventilation,
        component_states=row.get("component_states"),
        component_sources=row.get("component_sources"),
    )
    year = int(row["measurement_year"])
    weather_entry = config.get("weather_manifest", {}).get(
        f"{row['municipality']}:{year}"
    )
    weather = (
        Path(weather_entry["path"])
        if weather_entry
        else Path(config["weather_directory"]) / f"weather-{year}.epw"
    )
    climate = climate_from_epw(weather, calendar_year=year)
    if weather_entry and climate.weather_sha256 != weather_entry["sha256"]:
        raise ValueError("Pinned AMY checksum changed")
    facade_settings = {}
    if category in ("single_family", "multi_family"):
        profile = load_sveby_spec()
        use = profile.use_spec(calendar_year=year)
        if config.get("apply_residential_shading", False):
            facade_settings = dict(profile.building_assumptions(calendar_year=year)["facade"])
        flow = 0.35
        fraction = ScheduleSpec.constant(1)
    else:
        profile = (
            load_sveby_office_v1() if category == "office" else load_sveby_school_v1()
        )
        explicit_dates = row.get("active_dates")
        days = explicit_dates or normal_use_dates(
            year,
            profile.data["reference_active_days"],
            summer_closure=category == "school"
            and config.get("school_summer_closure", True),
        )
        operation = profile.building_assumptions(
            calendar_year=year,
            active_dates=days,
            calendar_source=row.get("calendar_source")
            or "Effective normal-use scenario: evenly distributed weekdays; optional June24-Aug4 closure; actual dates unknown",
            zone_role="classrooms",
        )
        use = UseSpec.model_validate(operation["use"])
        flow = operation["airflow"]["mechanical_vent_l_s_m2"]
        fraction = ScheduleSpec.model_validate(
            operation["airflow"]["mechanical_vent_fraction"]
        )
        facade_settings = operation["facade"]
    design = row.get("designed_flow_l_s_m2", row.get("designed_ventilation_l_s_m2"))
    flow, _ = mechanical_flow_completion(
        category,
        design,
        flow,
        zero_policy=config.get("mf_reported_zero_flow_policy", "retain_reported"),
    )
    natural = (
        0.35 * row["atemp_m2"] / 1000 * 3600 / geometry.conditioned_volume_m3
        if ventilation == "S"
        else 0.0
    )
    airflow = AirflowSpec(
        topology=ventilation,
        # DB 1.5.0 MF review: q_inf uses Atemp, matching the explicit load
        # reference area below. Do not treat compatibility ACH as model ACH.
        **(
            classification.get("core_airflow_inputs")
            or {"envelope_infiltration_ach": classification["infiltration_ach"]}
        ),
        natural_exchange_ach=natural,
        mechanical_vent_l_s_m2=0 if ventilation == "S" else flow,
        mechanical_vent_fraction=fraction,
        sensible_recovery_effectiveness=config.get("ftx_recovery", 0.7)
        if ventilation == "FTX"
        else 0,
    )
    family = row["heating_family"]
    if family == "district_heat":
        heating = HeatingDeliverySpec(
            carrier="district_heat",
            source="Declared interface efficiency1, net loss0; delivered-space-heat target",
        )
    elif family in ("electric_direct", "electric_water"):
        heating = HeatingDeliverySpec(
            carrier="electric_direct", source="Declared direct electric heat, net loss0"
        )
    elif family in ("oil", "wood", "wood_chips", "gas", "pellets"):
        heating = HeatingDeliverySpec(
            carrier="fuel",
            efficiency=config.get("fuel_efficiency", 0.9),
            source="Effective combustion efficiency scenario; observed efficiency unavailable",
        )
    elif family.startswith("heat_pump"):
        cop = config.get("heat_pump_cop", {}).get(family)
        if cop is None:
            raise ValueError(
                "Heat-pump carrier input requires an explicitly declared COP scenario"
            )
        heating = HeatingDeliverySpec(
            carrier="heat_pump",
            heat_pump_cop=ScheduleSpec.constant(cop),
            source="Declared effective COP scenario; actual part-load performance unknown",
        )
    else:
        raise ValueError(
            "Mixed/unresolved heating carrier; native accounting spec required"
        )
    facade_settings.pop("glazing", None)
    spec = BuildingSpec(
        identifier=row["building_id"],
        geometry=geometry,
        envelope=classification["envelope"].updated(
            foundation=FoundationSpec(), ground_temperature=None
        ),
        facade=FacadeSpec(
            default_wwr=config.get("wwr", 0.2),
            glazing=classification["glazing"],
            **facade_settings,
        ),
        airflow=airflow,
        use=use,
        climate=climate,
        load_reference_area_m2=row["atemp_m2"],
        delivery=DeliverySpec(heating=heating),
    )
    if category == "multi_family" and row.get("mf_evidence"):
        from ..mf_evidence import apply_quantitative_evidence

        spec, _ = apply_quantitative_evidence(spec, row)
    return spec


def physical_descriptors(row, spec):
    """Resolve actual envelope areas and steady-state thermal-response proxies.

    These are explanatory baseline inputs, not fitted energy signatures or
    annual energy predictions. Roof/ground coupling is retained as an assumption.
    """
    from epsm_core.pipeline import resolve_geometry

    resolved = resolve_geometry(spec)
    conditioned = {z for z, d in resolved.graph.zones.items() if d["conditioned"]}
    wall = roof = ground = window = transmission = 0.0
    by_face = {f.name: f for f in resolved.graph.surfaces}
    for face in resolved.graph.surfaces:
        adjacent = by_face.get(face.adjacent)
        if face.zone not in conditioned or (
            face.boundary == "Surface"
            and adjacent is not None
            and adjacent.zone in conditioned
        ):
            continue
        construction = None
        if face.kind == "Wall":
            wall += face.area
            construction = (
                spec.envelope.basement_wall
                if face.boundary == "Ground"
                else spec.envelope.wall
            )
        elif face.kind in ("Roof", "Ceiling"):
            roof += face.area
            construction = spec.envelope.top
        elif face.kind == "Floor" and face.boundary == "Ground":
            ground += face.area
            construction = spec.envelope.ground
        if construction is not None:
            transmission += face.area * construction.u_value_w_m2k
    for opening in resolved.openings:
        if by_face[opening.parent].zone in conditioned:
            window += opening.area
    transmission += window * (
        spec.facade.glazing.u_value_w_m2k - spec.envelope.wall.u_value_w_m2k
    )
    area = row["atemp_m2"]
    volume = spec.geometry.conditioned_volume_m3
    flow = area * spec.airflow.mechanical_vent_l_s_m2 / 1000
    # Core's native area/volume contract; keep separate natural exchange.
    outdoor = (
        spec.airflow.infiltration_design_flow_m3_s(
            reference_area_m2=area, volume_m3=volume
        )
        + volume * spec.airflow.natural_exchange_ach / 3600
    )
    mean_fraction = np.mean(
        spec.airflow.mechanical_vent_fraction.hourly(spec.climate.calendar_year)
    )
    heat_flow = (
        flow * mean_fraction * (1 - spec.airflow.sensible_recovery_effectiveness)
    )
    return {
        **{k: row.get(k) for k in NUMERIC},
        "wall_area_per_atemp": wall / area,
        "roof_area_per_atemp": roof / area,
        "ground_area_per_atemp": ground / area,
        "volume_per_atemp": volume / area,
        # Dimensionless surface/volume compactness and observed dwelling density;
        # neither descriptor uses the annual heating observation (ISO 52016 basis).
        "compactness": (wall + roof + ground) / volume ** (2 / 3),
        "atemp_per_apartment": area / row["apartments"]
        if row.get("apartments") and row["apartments"] > 0
        else None,
        "gross_to_atemp_ratio": spec.geometry.conditioned_floor_area_m2 / area,
        "transmission_w_k_per_atemp": transmission / area,
        "air_exchange_w_k_per_atemp": 1206 * (outdoor + heat_flow) / area,
        "heated_basement": "yes" if spec.geometry.basement_conditioned else "no",
    }


def programme_context(row):
    """Retain existing GIS/EPC programme disagreement without relabelling from heat."""
    purpose = str(row.get("purpose") or row.get("andamal1") or "").casefold()
    category = row.get("use_group", "unknown")
    if (
        category in ("single_family", "multi_family")
        and purpose
        and "bostad" not in purpose
    ):
        return "non_residential_purpose_in_residential_epc"
    return (
        "housing"
        if category in ("single_family", "multi_family") and purpose
        else category
        if purpose
        else "unknown"
    )


def prepare(config_path, output):
    """Freeze one new retrospective campaign; originals are never modified."""
    from epsm_core import BuildingSpec

    path = Path(config_path).resolve()
    config = DEFAULTS | read(path)
    if (
        config["schema_version"] != 1
        or config["outer_folds"] != 5
        or config["inner_folds"] != 3
    ):
        raise ValueError("Protocol requires schema1 and five/three property folds")
    root = Path(output).resolve()
    if root.exists():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    source = Path(config["cohort_file"])
    source = source if source.is_absolute() else (path.parent / source).resolve()
    rows = (
        pd.read_parquet(source).to_dict("records")
        if source.suffix == ".parquet"
        else read(source)
    )
    targets = {}
    inputs = {str(path): file_sha256(path), str(source): file_sha256(source)}
    boundary_audit = None
    if config.get("boundary_audit"):
        audit_path = Path(config["boundary_audit"])
        audit_path = (
            audit_path
            if audit_path.is_absolute()
            else (path.parent / audit_path).resolve()
        )
        boundary_audit = read(audit_path)
        inputs[str(audit_path)] = file_sha256(audit_path)
    from ..archetype_membership import (
        assess_membership,
        hydrate_link_membership,
        hydrate_source_membership,
    )

    rows, membership_sources = hydrate_source_membership(cleaned(rows))
    inputs.update(membership_sources)
    if config.get("membership_publication"):
        publication = Path(config["membership_publication"])
        publication = (
            publication
            if publication.is_absolute()
            else (path.parent / publication).resolve()
        )
        rows, membership_sources = hydrate_link_membership(rows, publication)
        inputs.update(membership_sources)
    for name in ("spec_manifest", "weather_directory"):
        if config.get(name):
            value = Path(config[name])
            config[name] = str(
                value if value.is_absolute() else (path.parent / value).resolve()
            )
    if isinstance(config.get("weather_manifest"), str):
        value = Path(config["weather_manifest"])
        value = value if value.is_absolute() else (path.parent / value).resolve()
        config["weather_manifest"] = read(value)
        inputs[str(value)] = file_sha256(value)
    if config.get("targets_file"):
        target_path = Path(config["targets_file"])
        target_path = (
            target_path
            if target_path.is_absolute()
            else (path.parent / target_path).resolve()
        )
        target_rows = pd.read_parquet(target_path).to_dict("records")
        targets = {r["building_id"]: r["target"] for r in target_rows}
        if len(targets) != len(target_rows):
            raise ValueError("Conflicting or duplicate building targets")
        inputs[str(target_path)] = file_sha256(target_path)
    specs = read(config["spec_manifest"]) if config.get("spec_manifest") else {}
    if config.get("spec_manifest"):
        inputs[config["spec_manifest"]] = file_sha256(config["spec_manifest"])
    observations, features, failures, registers = {}, [], [], {}
    membership_audit = []
    contextual = {}
    if config.get("context_sources"):
        from .context import derive_context

        contextual, context_sources = derive_context(
            cleaned(rows), config["context_sources"]
        )
        inputs.update(context_sources)
        atomic_json(root / "context-source-records.json", cleaned(contextual))
    seen = set()
    for raw in rows:
        row = cleaned(raw)
        bid = row["building_id"]
        if bid in seen:
            raise ValueError("Duplicate building observation")
        seen.add(bid)
        row.setdefault(
            "use_group",
            "multi_family"
            if row.get("epc_category") == "FLERBOSTADSHUS"
            else "unknown",
        )
        row.setdefault("ventilation", config.get("ventilation", "unknown"))
        row.setdefault("heating_family", config.get("heating_family", "unknown"))
        row.setdefault(
            "correction_status",
            "normal_use_adjusted_pre_weather_correction"
            if row.get("normal_use_adjusted") and not row.get("normal_year_corrected")
            else "unknown",
        )
        membership = assess_membership(row)
        membership_audit.append(
            {"building_id": bid, "property_key": row["property_key"], **membership}
        )
        if not membership["eligible"]:
            failures.append(
                {
                    "building_id": bid,
                    "stage": "membership",
                    "reasons": membership["reasons"],
                }
            )
            continue  # Outside this pure programme cohort, retained in its coverage audit.
        from epsm_data.parametric import era_for_year

        row["era"] = (
            era_for_year(int(row["construction_year"]))
            if row.get("construction_year")
            else "unknown"
        )
        target = targets.get(bid, row.get("target"))
        if target is None and row.get("heat_kwh") is not None:
            target = row["heat_kwh"] / row["atemp_m2"]
        if target is None or not np.isfinite(target) or target <= 0:
            failures.append(
                {
                    "building_id": bid,
                    "stage": "target",
                    "reason": "No valid measured heating target",
                }
            )
            continue
        if row["correction_status"] != "normal_use_adjusted_pre_weather_correction":
            failures.append(
                {
                    "building_id": bid,
                    "stage": "target",
                    "reason": "Unresolved target correction boundary",
                }
            )
            continue
        prop = row["property_key"]
        observations.setdefault(prop, {})[bid] = float(target)
        feature = {
            k: row.get(k)
            for k in PARENT
            + (
                "building_id",
                "formular_id",
                "property_key",
                "municipality",
                "measurement_year",
                "roof_form",
                "modelled_roof",
                "roof_fallback_applied",
                "roof_assumption",
                "building_position",
                "renovation_timing",
                "completed_renovation_year",
                "completed_window_replacement",
                "reported_completed_measures",
                "design_flow_evidence_status",
            )
        }
        feature["atemp_m2"] = row["atemp_m2"]
        feature["membership"] = membership
        feature["apartments"] = row.get("apartments")
        feature["programme_context"] = programme_context(row)
        feature["semantic_caveats"] = {
            k: row.get(k)
            for k in (
                "basement_definition",
                "internal_area_status",
                "storey_definition",
                "area_note",
            )
        }
        locked = set(row.get("locked_parameters", []))
        component_controls = {
            "wall": ("wall_resistance",),
            "roof": ("roof_resistance",),
            "ground": ("ground_resistance",),
            "basement_wall": ("basement_wall_resistance",),
            "window": ("window_u_multiplier", "shgc_multiplier"),
            "airtightness": ("infiltration_multiplier",),
        }
        for component in row.get("component_states", {}):
            locked.update(component_controls.get(component, ()))
        if row.get("mf_evidence"):
            from ..mf_evidence import evidence_locks

            locked.update(evidence_locks(row))
            feature["mf_evidence"] = row["mf_evidence"]
        if row.get("active_dates"):
            locked.add("schedule_shift_hours")
        feature["locked_parameters"] = sorted(locked)
        if row.get("footprint_preparation"):
            feature["footprint_preparation"] = row["footprint_preparation"]
        try:
            if row.get("input_preparation_failure"):
                raise ValueError(row["input_preparation_failure"])
            if bid in specs:
                spec_path = Path(specs[bid])
                inputs[str(spec_path)] = file_sha256(spec_path)
                spec = BuildingSpec.model_validate(read(spec_path))
                if row.get("mf_evidence"):
                    from ..mf_evidence import apply_quantitative_evidence

                    spec, application_audit = apply_quantitative_evidence(spec, row)
                    feature["mf_evidence_application"] = application_audit
            else:
                feature.update(roof_model_inputs(row))
                footprint, receipt = prepare_footprint(row)
                feature["footprint_preparation"] = receipt
                if footprint.status != "accepted":
                    raise ValueError(
                        f"footprint_preparation_{footprint.status}: {'; '.join(footprint.reasons)}"
                    )
                spec = build_baseline(row, config)
            feature["compiled_input_audit"] = {
                "whole_window_u_w_m2k": spec.facade.glazing.u_value_w_m2k,
                "wall_nominal_u_w_m2k": spec.envelope.wall.u_value_w_m2k,
                "top_nominal_u_w_m2k": spec.envelope.top.u_value_w_m2k,
                "mechanical_flow_l_s_m2_atemp": spec.airflow.mechanical_vent_l_s_m2,
                "flow_basis": "independent_operating_evidence"
                if "operating_flow" in row.get("mf_evidence", {})
                else mechanical_flow_completion(
                    row["use_group"],
                    row.get(
                        "designed_flow_l_s_m2", row.get("designed_ventilation_l_s_m2")
                    ),
                    0.35,
                    zero_policy=config.get(
                        "mf_reported_zero_flow_policy", "retain_reported"
                    ),
                )[1],
                "reported_design_flow_l_s_m2": row.get(
                    "designed_flow_l_s_m2", row.get("designed_ventilation_l_s_m2")
                ),
                "mf_reported_zero_flow_policy": config.get(
                    "mf_reported_zero_flow_policy", "retain_reported"
                ),
                "atemp_m2": spec.load_reference_area_m2,
                "conditioned_volume_m3": spec.geometry.conditioned_volume_m3,
                "heated_basement_extent_observed": False,
            }
            if bid in contextual:
                from .context import apply_context

                spec, context_audit = apply_context(
                    spec,
                    row,
                    contextual[bid],
                    config["context_sources"].get("radius_m", 100),
                )
                feature["context_audit"] = context_audit
            if spec.load_reference_area_m2 != row["atemp_m2"]:
                raise ValueError(
                    "Native spec must use the reported Atemp load reference"
                )
            if spec.identifier != bid:
                raise ValueError("Native spec building identity mismatch")
            weather = Path(spec.climate.epw_path).resolve()
            if file_sha256(weather) != spec.climate.weather_sha256:
                raise ValueError("AMY checksum mismatch")
            from epsm_core.weather import validate_epw_calendar

            validate_epw_calendar(weather, spec.climate.calendar_year)
            if spec.climate.calendar_year != row["measurement_year"]:
                raise ValueError("AMY does not match measurement year")
            inputs[str(weather)] = file_sha256(weather)
            for provenance in (
                weather.with_suffix(".json"),
                weather.with_name(weather.stem + "-provenance.json"),
            ):
                if provenance.is_file():
                    inputs[str(provenance)] = file_sha256(provenance)
            feature.update(physical_descriptors(row, spec))
            feature["spec_path"] = f"specs/{bid}.json"
            atomic_json(root / feature["spec_path"], spec.model_dump(mode="json"))
            registers[bid] = applicable(
                spec,
                locked=feature["locked_parameters"],
                overrides=config.get("parameter_bounds"),
            )
            feature["status"] = "prepared"
            feature["unknowns"] = [
                "actual renovation state",
                "programme/occupancy",
                "leakage",
                "operating flow",
                "soil",
                "net losses",
                "basement setpoint and conditioned extent",
            ]
            if not spec.context.buildings:
                feature["unknowns"].append("neighbour context")
        except Exception as exc:  # noqa: BLE001 - preserve every failed scientific case
            feature.update(status="preparation_failed", spec_path=None)
            failures.append(
                {"building_id": bid, "stage": "physics", "reason": str(exc)}
            )
        features.append(feature)
    if boundary_audit is not None and (
        boundary_audit["source_buildings"] != len(rows)
        or boundary_audit["eligible_buildings"]
        != sum(r["eligible"] for r in membership_audit)
    ):
        raise ValueError("Boundary audit and source-only cohort membership differ")
    atomic_json(root / "features.json", cleaned(features))
    atomic_json(root / "config.json", config)
    atomic_json(root / "parameter-register.json", registers)
    atomic_json(root / "preparation-failures.json", failures)
    atomic_json(root / "membership-audit.json", cleaned(membership_audit))
    for prop, values in observations.items():
        atomic_json(root / "targets" / f"{digest(prop)}.json", values)
    prepared = {
        str(p.relative_to(root)): file_sha256(p) for p in root.rglob("*") if p.is_file()
    }
    engine = Path(config["engine"])
    if engine.is_file():
        inputs[str(engine)] = file_sha256(engine)
    atomic_json(
        root / "manifest.json",
        {
            "schema_version": 1,
            "sources": source_identity(),
            "inputs": inputs,
            "prepared": prepared,
            "study_kind": config["study_kind"],
            "new_building_observations": False,
        },
    )
    atomic_json(
        root / "audit.json",
        {
            "source_buildings": len(rows),
            "membership_eligible": sum(r["eligible"] for r in membership_audit),
            "membership_quarantined": sum(not r["eligible"] for r in membership_audit),
            "target_eligible": len(features),
            "prepared": sum(r["status"] == "prepared" for r in features),
            "failures": failures,
            "uses": dict(Counter(r["use_group"] for r in features)),
            "target": "normal-use-adjusted delivered space heat before weather correction",
            "assumptions_are_not_measurements": True,
        },
    )
    return root


class TargetStore:
    """Load only requested property's observations after asserting exclusion sets."""

    def __init__(self, root):
        self.root = Path(root)

    def training(self, rows, forbidden_properties=()):
        if set(forbidden_properties) & {r["property_key"] for r in rows}:
            raise ValueError("Evaluated property requested by a fitting stage")
        return self._load(rows)

    def evaluation(self, rows, frozen_path):
        if not Path(frozen_path).is_file():
            raise ValueError("Evaluation observations require a saved frozen model")
        return self._load(rows)

    def _load(self, rows):
        values = {}
        for property_key in sorted({r["property_key"] for r in rows}):
            values.update(read(self.root / "targets" / f"{digest(property_key)}.json"))
        return [dict(r, target=values[r["building_id"]]) for r in rows]
