from epsm_workflows.epc import (
    raw_certificate_rows,
    reporting_regime,
    study_eligibility,
    study_report,
)


def test_reporting_is_not_export_schema():
    assert reporting_regime("2018-12-31") == "specific_energy_pre_2019"
    assert reporting_regime("2019-01-01") == "primary_energy_2019_weights"
    assert reporting_regime("2020-09-01") == "primary_energy_2020_weights"


def test_raw_join_preserves_rows(tmp_path):
    p = tmp_path / "source.txt"
    p.write_text("FormularId|Energy\n7|001.20\n7|unknown\n8|9\n")
    rows = list(raw_certificate_rows([p], [7], encoding="utf-8", source_schema="fore_v260"))
    assert len(rows) == 2
    assert rows[0]["original_values"]["Energy"] == "001.20"
    assert rows[0]["units"] is None


def test_study_does_not_duplicate_energy():
    record = dict(
        formular_id=7,
        certificate_to_footprint_count=2,
        buildings=[{"insamlingslage": "Fasad"}] * 2,
        subjects=[{"source_schemas": "fore_v260"}],
    )
    case = study_eligibility(record, {})
    assert not case["eligible"]
    assert "multiple_buildings_for_certificate" in case["reasons"]
    assert "boverket_mapping_workbooks_unverified" in case["reasons"]
    report = study_report([record])
    assert report["quotas"]["fore_v260"]["eligible"] == 0
    assert report["status"] == "ineligible_cohort"


def test_high_confidence_requires_meter_and_normal_use_alignment():
    record = dict(
        formular_id=7,
        certificate_to_footprint_count=1,
        buildings=[{"insamlingslage": "Fasad"}],
        subjects=[{"source_schemas": "fore_v260"}],
    )
    review = dict(
        reviewer="reviewer",
        reviewed_at="2026-09-06",
        measurement_start="2017-01-01",
        measurement_end="2017-12-31",
        energy_boundary="delivered_district_heat_heat_and_dhw",
        delivery_losses_accounted=True,
        weather_provenance={"station": 71420},
        measurement_basis="allocated",
    )
    result = study_eligibility(record, review, mapping_verified=True)
    assert not result["eligible"]
    assert "individual_building_measurement_unverified" in result["reasons"]
    assert "normal_use_alignment_unverified" in result["reasons"]
    review.update(
        measurement_basis="individual_building_meter",
        normal_use_alignment_verified=True,
        source_totals_reconciled=True,
    )
    assert study_eligibility(record, review, mapping_verified=True)["eligible"]
