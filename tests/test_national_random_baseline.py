"""Sampling cannot be changed by targets, input preparation or parquet ordering."""

import importlib.util
import json
from pathlib import Path

import pandas as pd

HELPER = (
    Path(__file__).resolve().parents[1]
    / "examples/epc_validation/run_national_random_baseline.py"
)
spec = importlib.util.spec_from_file_location("random_baseline", HELPER)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_full_population_draw_is_stable_and_retains_unpreparable_rows(tmp_path):
    rows = [
        dict(
            building_id=f"id-{i:02}",
            property_key=f"property-{i // 2}",
            municipality="1480",
            use_group="multi_family",
            era="SE.02",
            ventilation="F",
            heating_family="district_heat",
            correction_status="normal_use_adjusted_pre_weather_correction",
            measurement_year=2018,
            prior_study_property=i % 2 == 0,
            roof_form="unclear",
            height_status="determined",
            storeys=4,
            heated_basement_floors=0,
        )
        for i in range(10)
    ]
    inventory = tmp_path / "features.parquet"
    audit = tmp_path / "audit.parquet"
    source = tmp_path / "source.parquet"
    pd.DataFrame(rows).to_parquet(inventory)
    pd.DataFrame(
        [{"building_id": r["building_id"], "eligible": True} for r in rows]
    ).to_parquet(audit)
    pd.DataFrame(
        [
            {
                "building_id": r["building_id"],
                "purpose": "Bostad; Flerfamiljshus",
                "purpose2": None,
                "purpose3": None,
                "purpose4": None,
                "purpose5": None,
            }
            for r in rows
        ]
    ).to_parquet(source)
    first, second = tmp_path / "first", tmp_path / "second"
    module.select(first, inventory, audit, source, size=7, seed=123)
    pd.DataFrame(rows[::-1]).to_parquet(inventory)
    module.select(second, inventory, audit, source, size=7, seed=123)
    chosen = module.read(first / "selected-inputs.json")
    assert chosen == module.read(second / "selected-inputs.json")
    assert len(chosen) == len({r["building_id"] for r in chosen}) == 7
    assert all("target" not in r for r in chosen)
    prepared = module.prepare_geometry(chosen)
    assert [r["building_id"] for r in prepared] == [r["building_id"] for r in chosen]
    assert all(
        r["input_preparation_failure"]
        == "missing_or_invalid_representative_height_for_flat_roof_fallback"
        for r in prepared
    )
    assert all("input_preparation_failure" not in r for r in chosen)
    manifest = json.loads((first / "sampling-manifest.json").read_text())
    assert manifest["population_buildings"] == 10
    assert manifest["inclusion_probability"] == 0.7
    assert not manifest["failed_cases_replaced"]
    assert not manifest["previously_studied_buildings_excluded"]


def test_nonrectangular_pitched_roof_passes_native_preflight_without_proxy():
    from shapely.geometry import Polygon

    outline = Polygon([(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)])
    row = dict(
        building_id="building",
        storeys=4,
        heated_basement_floors=0,
        roof_form="pitched_ridge",
        height_status="determined",
        eave_height_m=12,
        ridge_height_m=15,
        geometry_wkb_hex=outline.wkb_hex,
    )
    prepared = module.prepare_geometry([row])[0]
    assert "input_preparation_failure" not in prepared
    assert prepared["native_geometry_preflight"] == "accepted"
    assert prepared["geometry_proxy_area_error"] == 0
    from shapely import from_wkb

    assert from_wkb(bytes.fromhex(prepared["geometry_wkb_hex"])).equals(outline)
    assert prepared["source_geometry_wkb_hex"] == row["geometry_wkb_hex"]
    assert prepared["footprint_preparation"]["status"] == "accepted"


def test_unusual_height_is_reviewed_but_not_rejected():
    from shapely.geometry import box

    for height in (1.28, 6.018):
        row = dict(
            building_id="height",
            storeys=1,
            heated_basement_floors=1,
            roof_form="flat",
            height_status="determined",
            representative_height_m=height,
            geometry_wkb_hex=box(0, 0, 10, 10).wkb_hex,
        )
        prepared = module.prepare_geometry([row])[0]
        assert "input_preparation_failure" not in prepared
        assert prepared["floor_height_m"] == height
        assert prepared["geometry_review_flags"] == [
            "floor_height_outside_typical_range"
        ]


def test_invalid_polygon_is_still_rejected():
    from shapely.geometry import Polygon

    row = dict(
        building_id="invalid",
        storeys=3,
        heated_basement_floors=0,
        roof_form="flat",
        height_status="determined",
        representative_height_m=9,
        geometry_wkb_hex=Polygon([(0, 0), (10, 10), (0, 10), (10, 0)]).wkb_hex,
    )
    prepared = module.prepare_geometry([row])[0]
    assert prepared["input_preparation_failure"] == "invalid_footprint_geometry"


def test_native_failure_is_preserved_and_sample_is_not_replaced(monkeypatch):
    from shapely.geometry import box
    import epsm_core.pipeline

    def reject(*args, **kwargs):
        raise ValueError("Duplicate thermal surfaces: native diagnostic")

    monkeypatch.setattr(epsm_core.pipeline, "resolve_geometry", reject)
    row = dict(
        building_id="native-failure",
        storeys=3,
        heated_basement_floors=0,
        roof_form="flat",
        height_status="determined",
        representative_height_m=9,
        geometry_wkb_hex=box(0, 0, 10, 10).wkb_hex,
    )
    prepared = module.prepare_geometry([row])
    assert len(prepared) == 1
    assert prepared[0]["building_id"] == row["building_id"]
    assert (
        prepared[0]["input_preparation_failure"]
        == "Duplicate thermal surfaces: native diagnostic"
    )
    assert "input_preparation_failure" not in row


def test_thin_eave_cleanup_is_audited_and_shared_with_baseline():
    from shapely import from_wkb
    from shapely.geometry import Polygon
    from epsm_workflows.cohort_transfer.preparation import build_geometry

    outline = Polygon(
        [
            (0, 1.575),
            (20.026, 0),
            (20.234, 3.683),
            (20.563, 10.003),
            (0.579, 11.546),
            (0.589, 11.53),
            (0.575, 11.279),
        ]
    )
    row = dict(
        building_id="thin-eave",
        storeys=3,
        heated_basement_floors=0,
        roof_form="pitched_ridge",
        height_status="determined",
        eave_height_m=8.276482543794174,
        ridge_height_m=11,
        geometry_wkb_hex=outline.wkb_hex,
    )
    prepared = module.prepare_geometry([row])[0]
    assert "input_preparation_failure" not in prepared
    assert prepared["source_geometry_wkb_hex"] == row["geometry_wkb_hex"]
    receipt = prepared["footprint_preparation"]
    assert receipt["status"] == "accepted"
    assert receipt["source_crs"] == receipt["target_crs"] == "EPSG:3006"
    assert (
        receipt["metrics"]["source_topology"] == receipt["metrics"]["cleaned_topology"]
    )
    assert receipt["edge_lineage"]
    cleaned = from_wkb(bytes.fromhex(prepared["geometry_wkb_hex"]))
    assert len(cleaned.exterior.coords) == 5
    assert build_geometry(prepared, {}).parts[0].footprint.polygon.equals(cleaned)
    assert len(outline.exterior.coords) == 8


def test_preparation_review_status_is_not_silently_accepted(monkeypatch):
    from dataclasses import replace
    from shapely.geometry import box
    import epsm_core.preparation

    actual = epsm_core.preparation.prepare_geometry

    def reviewed(geometry):
        return replace(
            actual(geometry), status="needs_review", reasons=("protected_boundary",)
        )

    monkeypatch.setattr(epsm_core.preparation, "prepare_geometry", reviewed)
    row = dict(
        building_id="review",
        storeys=3,
        heated_basement_floors=0,
        roof_form="flat",
        height_status="determined",
        representative_height_m=9,
        geometry_wkb_hex=box(0, 0, 10, 10).wkb_hex,
    )
    prepared = module.prepare_geometry([row])[0]
    assert prepared["footprint_preparation"]["status"] == "needs_review"
    assert (
        prepared["input_preparation_failure"]
        == "footprint_preparation_needs_review: protected_boundary"
    )


def test_unclear_roof_defaults_flat_at_measured_height_with_audit():
    from shapely.geometry import box
    from epsm_workflows.cohort_transfer.preparation import build_geometry

    row = dict(
        building_id="unclear",
        storeys=4,
        heated_basement_floors=0,
        roof_form="unclear",
        height_status="determined",
        representative_height_m=11.5,
        floor_height_m=99,
        geometry_wkb_hex=box(0, 0, 10, 10).wkb_hex,
    )
    prepared = module.prepare_geometry([row])[0]
    assert "input_preparation_failure" not in prepared
    assert prepared["roof_form"] == row["roof_form"] == "unclear"
    assert prepared["modelled_roof"] == "flat"
    assert prepared["roof_fallback_applied"] is True
    assert prepared["roof_assumption"]["height_m"] == 11.5
    assert prepared["floor_height_m"] == 11.5 / 4
    geometry = build_geometry(prepared, {})
    assert geometry.roof.shape == "flat"
    assert geometry.roof.topology == "compact"
    assert geometry.parts[0].storey_elevations_m[-1] == 11.5
    assert row["floor_height_m"] == 99


def test_missing_height_or_unsupported_roof_is_not_defaulted():
    from shapely.geometry import box
    import pytest
    from epsm_workflows.cohort_transfer.preparation import build_geometry

    row = dict(
        storeys=3,
        heated_basement_floors=0,
        roof_form="unclear",
        height_status="determined",
        geometry_wkb_hex=box(0, 0, 10, 10).wkb_hex,
    )
    with pytest.raises(ValueError, match="representative_height"):
        build_geometry(row, {})
    with pytest.raises(ValueError, match="unsupported roof"):
        build_geometry(
            {**row, "roof_form": "mono_pitch", "representative_height_m": 9}, {}
        )


def test_fallback_scores_keep_failure_in_its_own_denominator():
    rows = [
        dict(
            roof_fallback_applied=True,
            reported_kwh_m2=100,
            predicted_kwh_m2=100,
            property_key="a",
        ),
        dict(
            roof_fallback_applied=True,
            reported_kwh_m2=100,
            predicted_kwh_m2=None,
            property_key="b",
        ),
        dict(
            roof_fallback_applied=False,
            reported_kwh_m2=100,
            predicted_kwh_m2=100,
            property_key="c",
        ),
    ]
    groups = module.roof_completion_metrics(rows)
    assert groups["unclear_flat_fallback"]["attempted"] == 2
    assert groups["unclear_flat_fallback"]["accepted"] == 1
    assert groups["unclear_flat_fallback"]["coverage20"] == 0.5
    assert groups["classified_roofs"]["coverage20"] == 1
    assert (
        module.roof_completion_metrics(rows[:2])["classified_roofs"]["attempted"] == 0
    )


def test_unclear_preparation_failure_remains_in_unresolved_roof_group():
    failed = dict(
        roof_form="unclear",
        roof_fallback_applied=False,
        reported_kwh_m2=100,
        predicted_kwh_m2=None,
        property_key="failed",
    )
    groups = module.roof_completion_metrics([failed])
    assert groups["unclear_flat_fallback"]["attempted"] == 1
    assert groups["unclear_flat_fallback"]["failed"] == 1
    assert groups["classified_roofs"]["attempted"] == 0
