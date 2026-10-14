"""Frozen MF SE.02/F EPC cohort and bounded, out-of-sample calibration.

Run with ``python -m epsm_workflows.archetype_pilot --help``.  Source repositories
are read only.  Every study stage writes into its own caller-owned directory.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import json
import math
import random
import shutil
import sqlite3
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import duckdb

from .campaign import atomic_json, file_sha256
from .validation import measurement_period, number

STATES = ("existing", "standard", "advanced")
THICKNESS_GRID = (0.8, 0.9, 1.0, 1.1, 1.2)
FLOW_GRID = (0.30, 0.35, 0.40)
SEED = "epsm-mf-se02-f-pilot-v1"


def _read(path):
    return json.loads(Path(path).read_text())


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _year(value, bounds=(1961, 1975)):
    try:
        year = int(value)
    except (ValueError, TypeError):
        return None
    return year if bounds[0] <= year <= bounds[1] else None


def _raw_rows(epc_root):
    for path in sorted((Path(epc_root) / "efter_v260" / "data").glob("*.txt")):
        with path.open("rb") as stream:
            encoding = (
                "utf-16"
                if stream.read(4).startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                else "latin-1"
            )
        with path.open(encoding=encoding, newline="") as stream:
            for line, row in enumerate(csv.DictReader(stream, delimiter="|"), 2):
                yield path, line, row


def _raw_signature(row):
    return tuple(
        (key, row[key])
        for key in sorted(row)
        if key.startswith(("Egi", "Egen", "VentTyp", "ExpertGodk"))
    )


def _singleton_link(links):
    if len(links) != 1:
        raise ValueError("ambiguous_link")
    building_id, property_key, subject, category = links[0]
    if not property_key:
        raise ValueError("missing_property_key")
    return building_id, property_key, subject, category


def _observation(row, *, year_bounds=(1961, 1975), ventilation_system="F"):
    """Screen one newer EPC; do not read observed heat during sampling/splitting."""
    if (row.get("EgenByggnadsKat") or "").casefold() != "flerbostadshus":
        raise ValueError("not_multifamily")
    epc_year = _year(row.get("EgenNybyggAr"), year_bounds)
    if epc_year is None:
        raise ValueError("year_outside_se02")
    vents = {
        "VentTypSjalvdrag": "S",
        "VentTypF": "F",
        "VentTypFT": "FT",
        "VentTypFTX": "FTX",
        "VentTypFmed": "Fmed",
    }
    if [value for key, value in vents.items() if row.get(key) == "Ja"] != [
        ventilation_system
    ]:
        raise ValueError("ventilation_not_single_" + ventilation_system.lower())
    if row.get("EgiBeraknatVarde") != "Nej":
        raise ValueError("calculated_or_unknown_heat")
    try:
        start, end = measurement_period(row["EgiForstaArManad"], row["EgiSistaArManad"])
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid_measurement_period") from exc
    if (start.month, start.day, end.month, end.day) != (1, 1, 12, 31):
        raise ValueError("not_calendar_year")
    area = number(row, "EgenAtemp")
    heat = number(row, "EgiFjarrvarmeUPPV")
    hot_water = number(row, "EgiFjarrvarmeVV")
    if area <= 0 or heat <= 0 or hot_water < 0:
        raise ValueError("invalid_area_or_heat")
    alternative = (
        "Olja",
        "Gas",
        "Ved",
        "Flis",
        "OvrBiobransle",
        "ElVatten",
        "ElDirekt",
        "ElLuft",
        "PumpMark",
        "PumpFranluft",
        "PumpLuftLuft",
        "PumpLuftVatten",
    )
    if (
        any(
            (number(row, "Egi" + fuel + suffix, required=False) or 0) > 0
            for fuel in alternative
            for suffix in ("UPPV", "VV")
        )
        or (number(row, "EgiElVV", required=False) or 0) > 0
    ):
        raise ValueError("mixed_heat_carriers")
    if (number(row, "EgiSolvarme", required=False) or 0) > 0:
        raise ValueError("solar_thermal")
    try:
        storeys = int(row["EgenAntalPlan"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("invalid_storeys") from exc
    if not 1 <= storeys <= 20:
        raise ValueError("invalid_storeys")
    return dict(
        epc_year=epc_year,
        measurement_year=start.year,
        atemp_m2=area,
        heat_kwh=heat,
        hot_water_kwh=hot_water,
        storeys=storeys,
    )


def _geometry(height, storeys):
    """Require measured positive heights, without an arbitrary floor-height gate."""
    if height is None or height.get("height_status") != "determined":
        raise ValueError("height_not_determined")
    if (
        isinstance(storeys, bool)
        or not isinstance(storeys, (int, float))
        or not math.isfinite(storeys)
        or storeys != int(storeys)
        or storeys <= 0
    ):
        raise ValueError("invalid_or_missing_storeys")
    roof = height.get("roof_form")
    if roof == "flat":
        reference = height.get("representative_height_m")
    elif roof == "pitched_ridge":
        reference = height.get("eave_height_m")
        ridge = height.get("ridge_height_m")
        if reference is None or ridge is None:
            raise ValueError("pitched_roof_incomplete")
        if not math.isfinite(float(ridge)) or float(ridge) <= float(reference):
            raise ValueError("invalid_pitched_roof_heights")
    else:
        raise ValueError("roof_form_unsupported")
    if (
        reference is None
        or not math.isfinite(float(reference))
        or float(reference) <= 0
    ):
        raise ValueError("invalid_measured_height")
    return roof, float(reference) / storeys


def _model_footprint(geometry, roof):
    """Preserve valid polygonal outlines; Core supports irregular pitched plans."""
    from shapely import from_wkb

    footprint = from_wkb(bytes(geometry))
    if (
        footprint.geom_type not in ("Polygon", "MultiPolygon")
        or footprint.is_empty
        or not footprint.is_valid
        or footprint.has_z
        or not math.isfinite(footprint.area)
        or footprint.area <= 0
    ):
        raise ValueError("invalid_footprint_geometry")
    # Compatibility return field: no proxy and therefore no proxy area error.
    return footprint.wkb.hex(), 0.0


def _preflight_geometry(row):
    """Reject geometry that Core cannot resolve, before sampling or splitting."""
    from epsm_core import ClimateSpec
    from epsm_core.pipeline import resolve_geometry

    climate = ClimateSpec(
        epw_path="/geometry-preflight-only",
        weather_sha256="0" * 64,
        latitude_deg=57.7089,
        longitude_deg=11.9746,
        elevation_m=0,
        time_zone_hours=1,
        calendar_year=row["measurement_year"],
    )
    for state in STATES:
        spec = build_spec(
            {**row, "split": "train"}, state, None, climate_override=climate
        )
        resolve_geometry(spec, panelize=True)


def _split(rows):
    """Choose without observing heat, then separate all buildings on one property."""
    by_property = defaultdict(list)
    for row in rows:
        by_property[row["property_key"]].append(row)
    groups = sorted(
        by_property.values(), key=lambda g: _hash(SEED + g[0]["property_key"])
    )
    # Round-robin across non-target strata before applying the 200-building cap.
    strata = defaultdict(list)
    for group in groups:
        row = group[0]
        area_bin = min(4, int(math.log(max(row["atemp_m2"], 1), 2) // 2))
        height_bin = min(4, int(row["representative_height_m"] // 5))
        strata[
            (row["roof_form"], row["measurement_year"], area_bin, height_bin)
        ].append(group)
    chosen = []
    while strata and len(chosen) < 200:
        for key in sorted(tuple(strata), key=lambda x: _hash(SEED + str(x))):
            if len(chosen) >= 200:
                break
            group = strata[key].pop(0)
            if len(chosen) + len(group) <= 200:
                chosen.extend(group)
            if not strata[key]:
                del strata[key]
    chosen_groups = defaultdict(list)
    for row in chosen:
        chosen_groups[row["property_key"]].append(row)
    partitions = {"train": [], "validation": [], "test": []}
    counts = Counter()
    for key in sorted(chosen_groups, key=lambda k: _hash(SEED + "split:" + k)):
        # Greedy deficit assignment preserves groups and approaches 60/20/20.
        n = len(chosen_groups[key])
        target = {"train": 0.6, "validation": 0.2, "test": 0.2}
        part = max(target, key=lambda p: (target[p] * len(chosen) - counts[p], p))
        partitions[part].extend(chosen_groups[key])
        counts[part] += n
    return partitions


def prepare(args):
    root = Path(args.output).resolve()
    if root.exists():
        raise FileExistsError(root)
    publication = Path(args.publication).resolve()
    height_release = Path(args.height_release).resolve()
    epc_root = Path(args.epc_root).resolve()
    registry = Path(args.registry).resolve()
    selection = Path(args.height_selection).resolve()
    links_path = publication / "catalogue/accepted_links.parquet"
    buildings_path = publication / "building_results/kommunkod=1480/part.parquet"
    with duckdb.connect() as db:
        links = db.execute(
            """SELECT formular_id,building_id,property_key,
                           epc_subject_id,epc_building_category
                           FROM read_parquet(?) WHERE building_municipality_code='1480'
                           AND certificate_to_footprint_count=1
                           AND building_to_certificate_count=1""",
            [str(links_path)],
        ).fetchall()
    by_fid = defaultdict(list)
    for fid, bid, property_key, subject, category in links:
        by_fid[int(fid)].append((bid, property_key, subject, category))
    candidate = {}
    exclusions = []
    signatures = {}
    for path, line, raw in _raw_rows(epc_root):
        try:
            fid = int(raw["FormularId"])
        except (KeyError, TypeError, ValueError):
            continue
        if fid not in by_fid:
            continue
        signature = _raw_signature(raw)
        if fid in signatures:
            if signatures[fid] != signature:
                exclusions.append(dict(formular_id=fid, reason="conflicting_raw_rows"))
                candidate.pop(fid, None)
            continue
        signatures[fid] = signature
        try:
            observation = _observation(raw)
        except ValueError as exc:
            exclusions.append(dict(formular_id=fid, reason=str(exc)))
            continue
        try:
            bid, property_key, subject, category = _singleton_link(by_fid[fid])
        except ValueError as exc:
            exclusions.append(dict(formular_id=fid, reason=str(exc)))
            continue
        candidate[fid] = dict(
            formular_id=fid,
            building_id=bid,
            property_key=property_key,
            epc_subject_id=subject,
            epc_category=category,
            source_file=str(path),
            source_line=line,
            **observation,
        )
    for fid in by_fid.keys() - signatures.keys():
        exclusions.append(dict(formular_id=fid, reason="raw_epc_row_missing"))
    ids = sorted({r["building_id"] for r in candidate.values()})
    with duckdb.connect() as db:
        db.execute("CREATE TEMP TABLE ids(building_id VARCHAR)")
        db.executemany("INSERT INTO ids VALUES (?)", [(x,) for x in ids])
        buildings = db.execute(
            """SELECT b.building_id,b.geometry,b.building_area_m2,
                              b.insamlingslage,b.andamal1,b.kommunkod
                              FROM read_parquet(?) b JOIN ids USING(building_id)""",
            [str(buildings_path)],
        ).fetchall()
        tile_rows = db.execute(
            """SELECT DISTINCT owner_tile_id FROM read_parquet(?) h
                                JOIN ids USING(building_id)""",
            [str(selection)],
        ).fetchall()
        files = [
            height_release / f"results/buildings/owner_tile_id={tile}/part.parquet"
            for (tile,) in tile_rows
        ]
        files = [str(x) for x in files if x.is_file()]
        heights = (
            db.execute(
                """SELECT h.building_id,h.height_status,
                             h.representative_height_m,h.roof_form,h.eave_height_m,
                             h.ridge_height_m FROM read_parquet(?,hive_partitioning=false,
                             union_by_name=true) h JOIN ids USING(building_id)""",
                [files],
            ).fetchall()
            if files
            else []
        )
    building_by_id = {r[0]: r[1:] for r in buildings}
    height_by_id = {
        r[0]: dict(
            zip(
                (
                    "height_status",
                    "representative_height_m",
                    "roof_form",
                    "eave_height_m",
                    "ridge_height_m",
                ),
                r[1:],
            )
        )
        for r in heights
    }
    connection = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    age = {}
    for offset in range(0, len(ids), 500):
        batch = ids[offset : offset + 500]
        query = (
            "SELECT building_id,construction_year FROM registry_buildings WHERE building_id IN ("
            + ",".join("?" * len(batch))
            + ")"
        )
        age.update(connection.execute(query, batch))
    connection.close()
    final = []
    seen_buildings = set()
    for fid, row in sorted(candidate.items()):
        bid = row["building_id"]
        if bid in seen_buildings:
            exclusions.append(dict(formular_id=fid, reason="duplicate_building"))
            continue
        seen_buildings.add(bid)
        if bid not in building_by_id:
            exclusions.append(dict(formular_id=fid, reason="missing_footprint"))
            continue
        geometry, footprint_area, outline, purpose, municipality = building_by_id[bid]
        try:
            registry_year = age.get(bid)
            if registry_year is not None and not 1961 <= registry_year <= 1975:
                raise ValueError("registry_year_conflicts_with_era")
            roof, floor_height = _geometry(height_by_id.get(bid), row["storeys"])
            model_wkb, model_area_error = _model_footprint(geometry, roof)
            if (
                not footprint_area
                or not 0.75 <= footprint_area * row["storeys"] / row["atemp_m2"] <= 1.25
            ):
                raise ValueError("atemp_geometry_mismatch")
            if outline not in ("Fasad", "Takkant"):
                raise ValueError("unsupported_outline")
        except ValueError as exc:
            exclusions.append(dict(formular_id=fid, reason=str(exc)))
            continue
        height = height_by_id[bid]
        prepared = {
            **row,
            "registry_year": registry_year,
            "construction_year": registry_year or row["epc_year"],
            "roof_form": roof,
            "floor_height_m": floor_height,
            "representative_height_m": height["representative_height_m"],
            "eave_height_m": height["eave_height_m"],
            "ridge_height_m": height["ridge_height_m"],
            "footprint_area_m2": footprint_area,
            "outline": outline,
            "purpose": purpose,
            "municipality": municipality,
            "geometry_wkb_hex": model_wkb,
            "geometry_proxy_area_error": model_area_error,
        }
        try:
            _preflight_geometry(prepared)
        except Exception as exc:
            exclusions.append(
                dict(
                    formular_id=fid,
                    reason="core_geometry_preflight_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        final.append(prepared)
    partitions = _split(final)
    selected = [
        {**row, "split": part} for part, values in partitions.items() for row in values
    ]
    root.mkdir(parents=True)
    atomic_json(root / "cohort.json", selected)
    atomic_json(
        root / "split_ids.json",
        {
            part: sorted(r["building_id"] for r in values)
            for part, values in partitions.items()
        },
    )
    atomic_json(root / "exclusions.json", exclusions)
    source_files = [
        links_path,
        buildings_path,
        registry,
        selection,
        height_release / "manifest.json",
        publication / "release_manifest.json",
    ]
    try:
        import epsm_data
        from epsm_core import __file__ as core_init

        data_root = Path(epsm_data.__file__).resolve().parents[2]
        core_root = Path(core_init).resolve().parents[2]
        source_files.extend(
            (
                data_root / "BASELINE.json",
                data_root / "src/epsm_data/datasets/layered/archetypes.json",
                data_root / "src/epsm_data/datasets/parametric/catalogue.json",
                *(
                    core_root / f"src/epsm_core/{name}.py"
                    for name in (
                        "compiler",
                        "pipeline",
                        "roofs",
                        "topology",
                        "surface_checks",
                    )
                ),
            )
        )
    except ImportError as exc:
        raise RuntimeError(
            "epsm_data and epsm_core are required for preparation"
        ) from exc
    # The immutable study pins content rather than relying on modification times.
    inputs = {str(p): dict(sha256=file_sha256(p)) for p in source_files if p.is_file()}
    for path in sorted((epc_root / "efter_v260/data").glob("*.txt")):
        inputs[str(path)] = dict(sha256=file_sha256(path))
    atomic_json(
        root / "manifest.json",
        dict(
            schema_version=1,
            study="mf_se02_f_gothenburg",
            seed=SEED,
            inputs=inputs,
            eligible=len(final),
            selected=len(selected),
            splits={p: len(v) for p, v in partitions.items()},
            exclusions=dict(Counter(x["reason"] for x in exclusions)),
            states=STATES,
            thickness_grid=THICKNESS_GRID,
            flow_grid=FLOW_GRID,
            efficiency=1.0,
            net_loss_kwh_m2=5.0,
            pitched_proxy_max_relative_area_error=0.05,
            attic_ach=0.0,
            attic_note="Core sealed cold-attic assumption; not fitted",
        ),
    )
    return root / "manifest.json"


def _insulation_scaled(construction, factor):
    """Change only the highest-resistance insulation layer; preserve material IDs."""
    candidates = [
        (i, layer.thickness_m / layer.material.conductivity_w_mk)
        for i, layer in enumerate(construction.layers)
        if layer.material.conductivity_w_mk < 0.08
    ]
    if not candidates:
        raise ValueError("No insulating layer in calibrated role")
    selected = max(candidates, key=lambda x: x[1])[0]
    layers = list(construction.layers)
    layers[selected] = layers[selected].updated(
        thickness_m=layers[selected].thickness_m * factor
    )
    return construction.updated(layers=tuple(layers))


@lru_cache(maxsize=3)
def _state_inputs(state):
    from epsm_data import ArchetypeKey, load_parametric_catalogue
    from epsm_data.release import core_inputs

    physical = core_inputs("mf_se02", state)
    infiltration = (
        load_parametric_catalogue()
        .get_slot(ArchetypeKey("multi_family", "SE.02", "SE.Zone3", state, "F"))
        .infiltration.value
    )
    return physical, infiltration


@lru_cache(maxsize=16)
def _climate(weather_path, year):
    from epsm_core import climate_from_epw

    return climate_from_epw(weather_path, calendar_year=year)


def build_spec(
    row, state, weather_dir, *, wall=1.0, top=1.0, flow=0.35, climate_override=None
):
    """Build one variant from public Data/Core APIs; no mutation of catalogue data."""
    from epsm_core import (
        AirflowSpec,
        BuildingSpec,
        FacadeSpec,
        GeometrySpec,
        RoofSpec,
        load_sveby_spec,
    )
    from shapely import from_wkb

    if (
        state not in STATES
        or wall not in THICKNESS_GRID
        or top not in THICKNESS_GRID
        or flow not in FLOW_GRID
    ):
        raise ValueError("Variant exceeds the frozen calibration grid")
    if row["split"] not in {"train", "validation", "test"}:
        raise ValueError("Invalid study split")
    inputs, infiltration = _state_inputs(state)
    from epsm_core import GroundTemperatureSpec

    # Historical pilot comparisons retain their explicitly declared boundary.
    envelope = inputs["envelope"].updated(
        ground_temperature=GroundTemperatureSpec(),
        wall=_insulation_scaled(inputs["envelope"].wall, wall),
        top=_insulation_scaled(inputs["envelope"].top, top),
    )
    roof = (
        RoofSpec(shape="flat", topology="compact")
        if row["roof_form"] == "flat"
        else RoofSpec(shape="gable", topology="cold_attic")
    )
    geometry = GeometrySpec.from_footprint(
        from_wkb(bytes.fromhex(row["geometry_wkb_hex"])),
        storey_elevations_m=tuple(
            row["floor_height_m"] * i for i in range(row["storeys"] + 1)
        ),
        source_crs="EPSG:3006",
        roof=roof,
    )
    kind = "facade" if row["outline"] == "Fasad" else "roof_edge_proxy"
    geometry = geometry.updated(
        parts=tuple(
            p.updated(outline_kind=kind, outline_reviewed=False) for p in geometry.parts
        )
    )
    area_ratio = row["atemp_m2"] / geometry.conditioned_floor_area_m2
    sveby = load_sveby_spec()
    use = sveby.use_spec()
    use = use.updated(
        occupant_density_per_m2=use.occupant_density_per_m2 * area_ratio,
        equipment_gain_w_m2=use.equipment_gain_w_m2 * area_ratio,
        dhw_kwh_m2_year=use.dhw_kwh_m2_year * area_ratio,
    )
    if climate_override is None:
        weather = Path(weather_dir) / f"weather-{row['measurement_year']}.epw"
        if not weather.is_file():
            raise FileNotFoundError(
                f"Pinned measurement-year weather required: {weather}"
            )
        climate = _climate(str(weather.resolve()), row["measurement_year"])
    else:
        climate = climate_override
    spec = BuildingSpec(
        identifier=row["building_id"],
        geometry=geometry,
        envelope=envelope,
        facade=FacadeSpec(default_wwr=0.2, glazing=inputs["glazing"]),
        airflow=AirflowSpec(
            topology="F",
            envelope_infiltration_ach=infiltration,
            mechanical_vent_l_s_m2=flow * area_ratio,
        ),
        use=use,
        climate=climate,
    )
    return spec


def _variant_key(row, state, wall, top, flow):
    return _hash(
        json.dumps(
            (row["building_id"], state, wall, top, flow, row["measurement_year"])
        )
    )[:24]


def _run_variant(
    root, row, state, weather_dir, runner, *, wall=1.0, top=1.0, flow=0.35
):
    key = _variant_key(row, state, wall, top, flow)
    receipt = root / "runs" / key / "receipt.json"
    if receipt.is_file():
        value = _read(receipt)
        if value.get("status") != "complete":
            raise RuntimeError(f"Failed simulation retained at {receipt}")
        return float(value["space_heating_demand_kwh"])
    spec = build_spec(row, state, weather_dir, wall=wall, top=top, flow=flow)
    run_dir = receipt.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = runner.run(spec, run_dir / "energyplus")
        heating = float(result.scalars["space_heating_demand_kwh"])
        if not math.isfinite(heating) or heating < 0:
            raise ValueError("Invalid simulated space heat")
        atomic_json(
            receipt,
            dict(
                status="complete",
                building_id=row["building_id"],
                split=row["split"],
                state=state,
                wall=wall,
                top=top,
                flow=flow,
                spec_sha256=hashlib.sha256(spec.model_dump_json().encode()).hexdigest(),
                run_id=result.run_id,
                space_heating_demand_kwh=heating,
                diagnostics=result.diagnostics,
            ),
        )
        return heating
    except Exception as exc:
        atomic_json(
            receipt,
            dict(
                status="failed",
                building_id=row["building_id"],
                split=row["split"],
                state=state,
                wall=wall,
                top=top,
                flow=flow,
                error=f"{type(exc).__name__}: {exc}",
            ),
        )
        raise


def _predictions(
    root,
    rows,
    weather_dir,
    runner,
    *,
    wall=1.0,
    top=1.0,
    flow=0.35,
    tolerate_failures=False,
):
    jobs = [(r, state) for r in rows for state in STATES]

    def execute(job):
        row, state = job
        try:
            value = (
                _run_variant(
                    root, row, state, weather_dir, runner, wall=wall, top=top, flow=flow
                )
                / row["atemp_m2"]
                + 5.0
            )
            return row["building_id"], state, value, None
        except Exception as exc:
            if not tolerate_failures:
                raise
            return row["building_id"], state, None, f"{type(exc).__name__}: {exc}"

    predictions = defaultdict(dict)
    failures = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for building_id, state, value, error in pool.map(execute, jobs):
            if error is None:
                predictions[building_id][state] = value
            else:
                failures.append(dict(building_id=building_id, state=state, error=error))
    complete = {
        r["building_id"]: [predictions[r["building_id"]][state] for state in STATES]
        for r in rows
        if len(predictions[r["building_id"]]) == len(STATES)
    }
    return (complete, failures) if tolerate_failures else complete


def _mae(rows, predictions, weights):
    return sum(
        abs(
            sum(w * p for w, p in zip(weights, predictions[r["building_id"]]))
            - r["heat_kwh"] / r["atemp_m2"]
        )
        for r in rows
    ) / len(rows)


def _fit_mix(rows, predictions):
    # A simplex grid avoids fitted building-specific state labels and makes the
    # search fully reproducible. Ties prefer the neutral equal-weight prior.
    choices = (
        (a / 20, b / 20, (20 - a - b) / 20) for a in range(21) for b in range(21 - a)
    )
    return min(
        choices,
        key=lambda w: (_mae(rows, predictions, w), sum((x - 1 / 3) ** 2 for x in w), w),
    )


def _metrics(rows, predictions, weights, *, loss=5.0):
    actual = [r["heat_kwh"] / r["atemp_m2"] for r in rows]
    predicted = [
        sum(
            w * (p - 5.0 + loss) for w, p in zip(weights, predictions[r["building_id"]])
        )
        for r in rows
    ]
    errors = [p - a for a, p in zip(actual, predicted)]
    sorted_actual, sorted_pred, sorted_errors = (
        sorted(actual),
        sorted(predicted),
        sorted(errors),
    )

    def q(values, fraction):
        return values[int((len(values) - 1) * fraction)]

    return dict(
        n=len(rows),
        mae_kwh_m2=sum(map(abs, errors)) / len(rows),
        median_ape_percent=100
        * statistics.median(abs(e / a) for e, a in zip(errors, actual)),
        bias_kwh_m2=sum(errors) / len(rows),
        observed_quantiles_kwh_m2=[q(sorted_actual, p) for p in (0.1, 0.5, 0.9)],
        predicted_quantiles_kwh_m2=[q(sorted_pred, p) for p in (0.1, 0.5, 0.9)],
        signed_error_quantiles_kwh_m2=[q(sorted_errors, p) for p in (0.1, 0.5, 0.9)],
    )


def _require_study(root):
    root = Path(root).resolve()
    manifest = _read(root / "manifest.json")
    rows = _read(root / "cohort.json")
    if manifest["selected"] != len(rows):
        raise ValueError("Study cohort and manifest disagree")
    for path, identity in manifest["inputs"].items():
        # Simulation reads the active Data/Core package resources. Recheck those
        # lightweight files, while large raw inputs are frozen in cohort.json.
        if (
            "/src/epsm_data/" in path
            or "/src/epsm_core/" in path
            or path.endswith("/BASELINE.json")
        ):
            if not Path(path).is_file() or file_sha256(path) != identity["sha256"]:
                raise ValueError(f"Source changed after cohort freeze: {path}")
    return root, manifest, rows


def _weather_manifest(root, rows, weather_dir, *, create=False):
    required = sorted({r["measurement_year"] for r in rows})
    hashes = {}
    for year in required:
        path = Path(weather_dir).resolve() / f"weather-{year}.epw"
        if not path.is_file():
            raise FileNotFoundError(path)
        hashes[str(year)] = file_sha256(path)
    target = root / "weather_manifest.json"
    if target.exists():
        pinned = _read(target)
        if any(pinned.get(year) != sha for year, sha in hashes.items()):
            raise ValueError("Weather changed after baseline freeze")
    elif create:
        atomic_json(target, hashes)
    else:
        raise FileNotFoundError("Run baseline to freeze weather before calibration")
    return hashes


def _runner(executable):
    from epsm_core import EnergyPlusRunner

    return EnergyPlusRunner(energyplus=executable)


def prepare_weather(args):
    """Build one pinned actual-year Gothenburg EPW for each study year."""
    from .weather import compile_measurement_weather

    root, _, rows = _require_study(args.study)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    overrides = {}
    for item in args.station:
        year, station = item.split(":", 1)
        overrides[int(year)] = int(station)
    for year in sorted({r["measurement_year"] for r in rows}):
        target = output / f"weather-{year}.epw"
        if target.exists():
            continue
        per_year = output / "sources" / str(year)
        _, provenance = compile_measurement_weather(
            f"{year}-01-01",
            f"{year}-12-31",
            per_year,
            latitude=57.7089,
            longitude=11.9746,
            station_id=overrides.get(year),
        )
        shutil.copy2(per_year / f"weather-{year}.epw", target)
        atomic_json(output / f"weather-{year}-provenance.json", provenance)
    return output


def run_baseline(args, *, smoke=False):
    root, manifest, rows = _require_study(args.study)
    trainval = [r for r in rows if r["split"] != "test"]
    if smoke:
        available = [
            r
            for r in trainval
            if (
                Path(args.weather_dir) / f"weather-{r['measurement_year']}.epw"
            ).is_file()
        ]
        chosen = []
        for roof in ("flat", "pitched_ridge"):
            chosen.extend(
                r
                for r in available
                if r["roof_form"] == roof
                and r not in chosen
                and len(chosen) < 1 + (roof == "pitched_ridge")
            )
        chosen.extend(r for r in available if r not in chosen and len(chosen) < 3)
        trainval = chosen
        if len(trainval) < 3:
            raise ValueError(
                "Smoke requires three non-test buildings with local weather"
            )
    if not smoke:
        _weather_manifest(root, rows, args.weather_dir, create=True)
    runner = _runner(args.energyplus)
    predictions, failures = _predictions(
        root, trainval, args.weather_dir, runner, tolerate_failures=True
    )
    if smoke:
        if failures:
            atomic_json(root / "smoke.json", dict(status="failed", failures=failures))
            raise RuntimeError("Smoke EnergyPlus variants failed; see smoke.json")
        atomic_json(
            root / "smoke.json",
            dict(
                buildings=len(trainval),
                states=list(STATES),
                simulation_count=3 * len(trainval),
                status="passed",
            ),
        )
        return root / "smoke.json"
    failed_ids = {f["building_id"] for f in failures}
    failed_properties = {
        r["property_key"] for r in trainval if r["building_id"] in failed_ids
    }
    trainval = [r for r in trainval if r["property_key"] not in failed_properties]
    active_ids = [r["building_id"] for r in trainval]
    if (
        not trainval
        or not any(r["split"] == "train" for r in trainval)
        or not any(r["split"] == "validation" for r in trainval)
    ):
        raise RuntimeError("No complete training and validation cohort")
    atomic_json(
        root / "baseline.json",
        dict(
            analysis_building_ids=active_ids,
            simulation_failures=failures,
            excluded_property_keys=sorted(failed_properties),
            training={
                state: _metrics(
                    [r for r in trainval if r["split"] == "train"],
                    predictions,
                    tuple(float(i == j) for i in range(3)),
                )
                for j, state in enumerate(STATES)
            },
            validation={
                state: _metrics(
                    [r for r in trainval if r["split"] == "validation"],
                    predictions,
                    tuple(float(i == j) for i in range(3)),
                )
                for j, state in enumerate(STATES)
            },
            equal_mixture={
                part: _metrics(
                    [r for r in trainval if r["split"] == part],
                    predictions,
                    (1 / 3,) * 3,
                )
                for part in ("train", "validation")
            },
        ),
    )
    return root / "baseline.json"


def calibrate(args):
    root, manifest, rows = _require_study(args.study)
    if not (root / "baseline.json").is_file():
        raise FileNotFoundError("Run the frozen baseline before calibration")
    baseline = _read(root / "baseline.json")
    active_ids = set(baseline["analysis_building_ids"])
    rows = [r for r in rows if r["split"] == "test" or r["building_id"] in active_ids]
    if len(rows) < 120:
        atomic_json(
            root / "calibration.json", dict(status="skipped", reason="fewer_than_120")
        )
        return root / "calibration.json"
    train = [r for r in rows if r["split"] == "train"]
    validation = [r for r in rows if r["split"] == "validation"]
    trainval = train + validation
    _weather_manifest(root, rows, args.weather_dir)
    runner = _runner(args.energyplus)
    base = _predictions(root, trainval, args.weather_dir, runner)
    weights = _fit_mix(train, base)
    chosen = dict(wall=1.0, top=1.0, flow=0.35)
    stages = [
        dict(
            name="state_mixture",
            weights=weights,
            validation=_metrics(validation, base, weights),
        )
    ]
    for name, grid in (
        ("wall", THICKNESS_GRID),
        ("top", THICKNESS_GRID),
        ("flow", FLOW_GRID),
    ):
        candidates = []
        for value in grid:
            params = {**chosen, name: value}
            pred, failures = _predictions(
                root,
                validation,
                args.weather_dir,
                runner,
                tolerate_failures=True,
                **params,
            )
            score = math.inf if failures else _mae(validation, pred, weights)
            candidates.append((value, score, pred, failures))
        reference = chosen[name]
        selected = min(
            candidates, key=lambda x: (x[1], x[0] != reference, abs(x[0] - reference))
        )
        if not math.isfinite(selected[1]):
            raise RuntimeError(f"No valid {name} setting; inspect simulation receipts")
        chosen[name] = selected[0]
        stages.append(
            dict(
                name=name,
                value=selected[0],
                validation=_metrics(validation, selected[2], weights),
                grid=[
                    dict(
                        value=x[0],
                        validation_mae_kwh_m2=(x[1] if math.isfinite(x[1]) else None),
                        failures=x[3],
                    )
                    for x in candidates
                ],
            )
        )
    atomic_json(
        root / "calibration.json",
        dict(
            status="complete",
            weights=weights,
            parameters=chosen,
            stages=stages,
            frozen_before_test=True,
        ),
    )
    return root / "calibration.json"


def _bootstrap_intervals(rows, baseline, final, weights, *, repetitions=1000):
    grouped = defaultdict(list)
    for r in rows:
        grouped[r["property_key"]].append(r)
    keys = sorted(grouped)
    rng = random.Random(SEED)
    results = defaultdict(list)
    for _ in range(repetitions):
        sample = [r for key in (rng.choice(keys) for _ in keys) for r in grouped[key]]
        base = _metrics(sample, baseline, (1 / 3,) * 3)
        fitted = _metrics(sample, final, weights)
        results["baseline_mae_kwh_m2"].append(base["mae_kwh_m2"])
        results["final_mae_kwh_m2"].append(fitted["mae_kwh_m2"])
        results["mae_improvement_kwh_m2"].append(
            base["mae_kwh_m2"] - fitted["mae_kwh_m2"]
        )
        results["final_bias_kwh_m2"].append(fitted["bias_kwh_m2"])
    return {
        name: [sorted(values)[int(p * (repetitions - 1))] for p in (0.025, 0.975)]
        for name, values in results.items()
    }


def evaluate(args):
    root, manifest, rows = _require_study(args.study)
    calibration = _read(root / "calibration.json")
    if calibration.get("status") != "complete":
        raise ValueError("Calibration must complete before opening the test set")
    if (root / "test_report.json").exists():
        raise FileExistsError("Test set is already evaluated")
    test = [r for r in rows if r["split"] == "test"]
    _weather_manifest(root, rows, args.weather_dir)
    runner = _runner(args.energyplus)
    baseline, baseline_failures = _predictions(
        root, test, args.weather_dir, runner, tolerate_failures=True
    )
    final, final_failures = _predictions(
        root,
        test,
        args.weather_dir,
        runner,
        tolerate_failures=True,
        **calibration["parameters"],
    )
    complete_ids = set(baseline) & set(final)
    excluded_properties = {
        r["property_key"] for r in test if r["building_id"] not in complete_ids
    }
    planned_n = len(test)
    test = [r for r in test if r["property_key"] not in excluded_properties]
    if not test:
        raise RuntimeError("No complete test buildings; see simulation receipts")
    weights = tuple(calibration["weights"])

    def grouped_metrics(field):
        values = sorted(r[field] for r in test)
        first, second = values[len(values) // 3], values[2 * len(values) // 3]
        groups = (
            [(r) for r in test if r[field] <= first],
            [(r) for r in test if first < r[field] <= second],
            [(r) for r in test if r[field] > second],
        )
        return {
            name: dict(
                baseline=_metrics(group, baseline, (1 / 3,) * 3),
                final=_metrics(group, final, weights),
            )
            for name, group in zip(
                ("lower_third", "middle_third", "upper_third"), groups
            )
            if group
        }

    result = dict(
        status="complete",
        n=len(test),
        planned_n=planned_n,
        simulation_failures=dict(baseline=baseline_failures, final=final_failures),
        excluded_property_keys=sorted(excluded_properties),
        baseline_states={
            s: _metrics(test, baseline, tuple(float(i == j) for i in range(3)))
            for j, s in enumerate(STATES)
        },
        baseline_equal_mix=_metrics(test, baseline, (1 / 3,) * 3),
        final=_metrics(test, final, weights),
        loss_sensitivity={
            str(loss): dict(
                baseline=_metrics(test, baseline, (1 / 3,) * 3, loss=loss),
                final=_metrics(test, final, weights, loss=loss),
                mae_improved=_metrics(test, final, weights, loss=loss)["mae_kwh_m2"]
                < _metrics(test, baseline, (1 / 3,) * 3, loss=loss)["mae_kwh_m2"],
            )
            for loss in (0.0, 5.0, 10.0)
        },
        property_bootstrap_95pct_intervals=_bootstrap_intervals(
            test, baseline, final, weights
        ),
        test_mae_improved=_mae(test, final, weights)
        < _mae(test, baseline, (1 / 3,) * 3),
        by_roof={
            roof: dict(
                baseline=_metrics(
                    [r for r in test if r["roof_form"] == roof], baseline, (1 / 3,) * 3
                ),
                final=_metrics(
                    [r for r in test if r["roof_form"] == roof], final, weights
                ),
            )
            for roof in ("flat", "pitched_ridge")
            if any(r["roof_form"] == roof for r in test)
        },
        by_atemp=grouped_metrics("atemp_m2"),
        by_height=grouped_metrics("representative_height_m"),
        selected_parameters=calibration["parameters"],
        renovation_weights=weights,
        interpretation="Predictive mixture, not observed renovation states",
        buildings=[
            dict(
                building_id=r["building_id"],
                property_key=r["property_key"],
                observed_kwh_m2=r["heat_kwh"] / r["atemp_m2"],
                baseline_kwh_m2=sum(baseline[r["building_id"]]) / 3,
                final_kwh_m2=sum(
                    w * p for w, p in zip(weights, final[r["building_id"]])
                ),
            )
            for r in test
        ],
    )
    atomic_json(root / "test_report.json", result)
    _write_test_summary(root / "test_report.md", result)
    return root / "test_report.json"


def _write_test_summary(path, report):
    baseline = report["baseline_equal_mix"]
    final = report["final"]
    interval = report["property_bootstrap_95pct_intervals"]["mae_improvement_kwh_m2"]
    lines = [
        "# MF SE.02/F untouched-test results",
        "",
        f"Buildings: {report['n']}. Predictive state weights: "
        + ", ".join(
            f"{state} {weight:.2f}"
            for state, weight in zip(STATES, report["renovation_weights"])
        )
        + ".",
        "",
        "| Metric | Frozen equal-state baseline | Calibrated mixture |",
        "|---|---:|---:|",
        f"| MAE (kWh/m²) | {baseline['mae_kwh_m2']:.2f} | {final['mae_kwh_m2']:.2f} |",
        f"| Median absolute percentage error | {baseline['median_ape_percent']:.1f}% | {final['median_ape_percent']:.1f}% |",
        f"| Signed bias (kWh/m²) | {baseline['bias_kwh_m2']:.2f} | {final['bias_kwh_m2']:.2f} |",
        "",
        f"Test MAE improved: **{'yes' if report['test_mae_improved'] else 'no'}**. "
        f"Property bootstrap 95% interval for MAE improvement: "
        f"[{interval[0]:.2f}, {interval[1]:.2f}] kWh/m².",
        "",
        "## Delivery net-loss sensitivity",
        "",
        "| Assumed net loss (kWh/m²) | Baseline MAE | Final MAE | Improved |",
        "|---:|---:|---:|---|",
    ]
    for loss, values in report["loss_sensitivity"].items():
        lines.append(
            f"| {float(loss):.0f} | {values['baseline']['mae_kwh_m2']:.2f} | "
            f"{values['final']['mae_kwh_m2']:.2f} | "
            f"{'yes' if values['mae_improved'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            "The state weights are predictive only; they do not identify "
            "renovation histories. See `test_report.json` for quantiles, "
            "property intervals, subgroup results and building predictions.",
            "",
        ]
    )
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="Freeze eligible one-building EPC cohort")
    for flag in (
        "publication",
        "epc-root",
        "registry",
        "height-release",
        "height-selection",
        "output",
    ):
        p.add_argument("--" + flag, required=True)
    p = commands.add_parser("weather", help="Generate the needed actual-year EPWs")
    p.add_argument("--study", required=True)
    p.add_argument("--output", required=True)
    p.add_argument(
        "--station",
        action="append",
        default=[],
        metavar="YEAR:ID",
        help="Explicit SMHI station for a measurement year with a data gap",
    )
    for name in ("smoke", "baseline", "calibrate", "evaluate"):
        p = commands.add_parser(name)
        p.add_argument("--study", required=True)
        p.add_argument("--weather-dir", required=True)
        p.add_argument("--energyplus", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(prepare(args))
    elif args.command == "weather":
        print(prepare_weather(args))
    elif args.command == "smoke":
        print(run_baseline(args, smoke=True))
    elif args.command == "baseline":
        print(run_baseline(args))
    elif args.command == "calibrate":
        print(calibrate(args))
    elif args.command == "evaluate":
        print(evaluate(args))


if __name__ == "__main__":
    main()
