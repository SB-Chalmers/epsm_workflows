"""Identity, missingness and energy-boundary safeguards for rebuilt MF EDA."""

import json

import duckdb
import pandas as pd
import pytest

from epsm_workflows.mf_renovation_eda import (
    build_longitudinal,
    declared_heat_sum,
    harmonise_certificate,
    rebuild_certificates,
    run_eda,
)


def certificate(**updates):
    value = {
        "FormularId": 1,
        "EgenByggnadsKat": "Flerbostadshus",
        "IdKommunkod": 1480,
        "IdLankod": 14,
        "IdFastBet": "EXAMPLE PROPERTY",
        "IdHusnr": 1,
        "IdAdr": "Address relation A",
        "EgenNybyggAr": 1965,
        "EgenAtemp": 1000,
        "EgenAntalPlan": 3,
        "EgenAntalBolgh": 12,
        "EgenAtempBostad": 100,
        "EgiForstaArManad": 1801,
        "EgiSistaArManad": 1812,
        "EgiBeraknatVarde": "Nej",
        "EgiFjarrvarmeUPPV": 100000,
        "EgiFjarrvarmeVV": 25000,
        "EgiFjarrvarme": None,
        "EgiVVBered": None,
        "EgiEnergiPrestanda": 140,
        "EgiVersion": 2019,
        "Version": 2.6,
        "ExpertGodkannande": "2019-01-15",
        "_schema_version": "efter_v260",
        "VentTypF": "Ja",
        "AtgUtfordaByteFonster": None,
        "AtgUtfordaIsolTak": None,
        "AtgUtfordaUtfortAr": None,
    }
    value.update(updates)
    return value


def rebuilt(rows):
    with duckdb.connect() as connection:
        frame = pd.DataFrame(rows)
        connection.register("epc", frame)
        return rebuild_certificates(connection)


def test_missing_carriers_are_missing_and_explicit_zero_is_zero():
    assert declared_heat_sum({}) is None
    assert declared_heat_sum({"EgiFjarrvarme": 0}) == 0
    assert declared_heat_sum({"EgiFjarrvarmeUPPV": 100}, suffix="UPPV") == 100
    item = harmonise_certificate(certificate())
    assert item["legacy_unsuffixed_heat_missing"] is True
    assert item["space_heat_kwh_m2"] == 100
    assert item["eligible"] is True


def test_duplicate_addresses_do_not_multiply_energy_observations():
    original = certificate()
    frame = rebuilt([original, original, certificate(IdAdr="Address relation B")])
    assert len(frame) == 1
    assert frame.iloc[0].source_row_count == 3
    assert frame.iloc[0].scope_building_count == 1
    assert frame.iloc[0].eligible


def test_multi_building_certificate_retains_one_complete_scope():
    frame = rebuilt([certificate(), certificate(IdHusnr=2)])
    assert len(frame) == 1
    assert frame.iloc[0].scope_building_count == 2
    assert frame.iloc[0].eligible
    assert frame.iloc[0].scope_key != rebuilt([certificate()]).iloc[0].scope_key


@pytest.mark.parametrize("change", [
    {"EgiFjarrvarmeUPPV": 110000}, {"Version": 2.7},
    {"AtgUtfordaByteFonster": "Ja"},
])
def test_conflicting_payloads_are_quarantined(change):
    frame = rebuilt([certificate(), certificate(**change)])
    assert frame.iloc[0].payload_variant_count == 2
    assert not frame.iloc[0].eligible
    assert "conflicting_certificate_payloads" in frame.iloc[0].reject_reasons
    assert pd.isna(frame.iloc[0].space_heat_kwh_m2)


def test_missing_identity_prevents_assignment():
    frame = rebuilt([certificate(IdHusnr=None)])
    assert not frame.iloc[0].eligible
    assert "incomplete_certificate_identity" in frame.iloc[0].reject_reasons


def test_optional_flags_never_establish_component_condition():
    missing = harmonise_certificate(certificate())
    negative = harmonise_certificate(certificate(AtgUtfordaByteFonster="Nej"))
    positive = harmonise_certificate(certificate(
        AtgUtfordaByteFonster="Ja", AtgUtfordaUtfortAr=2020,
    ))
    assert missing["renovation_evidence_status"] == "unanswered_optional_interval_fields"
    assert negative["renovation_evidence_status"] == "no_positive_optional_interval_evidence"
    assert positive["optional_measure_timing"] == "after_measurement"
    assert all("renovation_state" not in item for item in [missing, negative, positive])
    assert missing["eligible"] == negative["eligible"] == positive["eligible"]


def test_old_total_and_dhw_are_not_harmonised_normal_use_target():
    old = harmonise_certificate(certificate(
        _schema_version="fore_v260", Version=2.5,
        EgiFjarrvarmeUPPV=None, EgiFjarrvarmeVV=None,
        EgiFjarrvarme=125000, EgiVVBered=25000,
        EgiGruppFjarrvarme="Mätt",
    ))
    assert old["space_heat_kwh"] == 100000
    assert old["declared_heating_kwh"] == 125000
    assert old["normal_use_status"] == "unverified"
    assert not old["eligible"]
    assert "old_form_normal_use_boundary_unverified" in old["reject_reasons"]


@pytest.mark.parametrize("updates,reason", [
    ({"EgiSistaArManad": 1811}, "invalid_or_nonannual_measurement_period"),
    ({"EgiBeraknatVarde": "Ja"}, "calculated_or_unknown_energy"),
    ({"EgiElDirektUPPV": 1000}, "not_single_district_heat_carrier"),
    ({"Version": 9}, "unverified_normal_use_form"),
    ({"EgiFjarrvarmeUPPV": None}, "missing_space_heat"),
    ({"EgiFjarrvarmeUPPV": 0}, "nonpositive_space_heat"),
])
def test_invalid_boundaries_stay_in_rejection_audit(updates, reason):
    item = harmonise_certificate(certificate(**updates))
    assert not item["eligible"]
    assert reason in item["reject_reasons"]


def pair(**last_updates):
    second = certificate(
        FormularId=2, EgiForstaArManad=2301, EgiSistaArManad=2312,
        ExpertGodkannande="2024-01-15", EgiFjarrvarmeUPPV=90000,
    )
    second.update(last_updates)
    return build_longitudinal(rebuilt([certificate(), second]))


def test_pairs_require_stable_area_and_boundary():
    result = pair(EgenAtemp=1100)
    assert len(result) == 1 and result.iloc[0].eligible
    assert result.iloc[0].atemp_relative_change == pytest.approx(0.10)
    large = pair(EgenAtemp=1101)
    assert not large.iloc[0].eligible
    assert "atemp_change_exceeds_10_percent" in large.iloc[0].reject_reasons
    mixed = pair(EgiElDirektUPPV=1000)
    assert not mixed.iloc[0].eligible
    assert "heating_carrier_changed" in mixed.iloc[0].reject_reasons


def test_invalid_endpoint_is_not_skipped_for_an_acceptable_pair():
    rows = [
        certificate(EgiFjarrvarmeUPPV=None),
        certificate(FormularId=2, EgiForstaArManad=2001, EgiSistaArManad=2012),
        certificate(FormularId=3, EgiForstaArManad=2301, EgiSistaArManad=2312),
    ]
    result = build_longitudinal(rebuilt(rows))
    assert len(result) == 1 and not result.iloc[0].eligible
    assert "first_missing_space_heat" in result.iloc[0].reject_reasons
    assert result.iloc[0].certificate_count == 3


def test_scope_change_and_overlap_cannot_be_longitudinal_evidence():
    changed = pair(IdHusnr=2)
    assert changed.empty
    overlapping = pair(EgiForstaArManad=1806, EgiSistaArManad=1905)
    assert not overlapping.iloc[0].eligible
    assert "overlapping_or_repeated_measurement_period" in overlapping.iloc[0].reject_reasons


def test_end_to_end_archive_remains_read_only_and_outputs_are_reproducible(tmp_path):
    database = tmp_path / "archive.duckdb"
    rows = [certificate(), certificate(IdAdr="second address")]
    with duckdb.connect(str(database)) as connection:
        source = pd.DataFrame(rows)
        connection.register("input_rows", source)
        connection.execute("CREATE TABLE epc AS SELECT * FROM input_rows")
    before = database.read_bytes()
    output = tmp_path / "evidence"
    audit = run_eda(database, output)
    assert database.read_bytes() == before
    assert audit["raw_mf_rows"] == 2
    assert audit["unique_mf_certificates"] == 1
    assert audit["eligible_certificates"] == 1
    assert audit["legacy_catalogue_selection"]["rows"] == 2
    assert audit["eda"]["age_period_cohort_policy"].startswith("construction period")
    assert json.loads((output / "audit.json").read_text())["source_database_sha256"]
    assert "EXAMPLE PROPERTY" not in (output / "audit.json").read_text()
    assert "EXAMPLE PROPERTY" not in pd.read_parquet(output / "certificates.parquet").to_json()
    with pytest.raises(FileExistsError):
        run_eda(database, output)
