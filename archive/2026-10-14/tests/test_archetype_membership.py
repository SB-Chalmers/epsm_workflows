"""Source-semantic regressions for pure archetype calibration membership."""

import csv
import hashlib
import json

import pytest

from epsm_workflows.archetype_membership import (
    assess_membership,
    hydrate_source_membership,
)


def residential(**changes):
    return {
        "use_group": "multi_family",
        "epc_category": "FLERBOSTADSHUS",
        "residential_percent": 100,
        "apartments": 24,
        "purpose": "Bostad;Flerfamiljshus",
        "building_position": "Friliggande",
        "construction_year": 1970,
        "epc_year": 1968,
        "registry_year": 1972,
        "ventilation": "F",
        "observed_ventilation": "F",
    } | changes


def test_detached_is_placement_not_single_family_use():
    assert assess_membership(residential())["eligible"]
    row = residential(
        use_group="single_family",
        epc_category="EN- OCH TVÅBOSTADSHUS",
        apartments=1,
        purpose="Bostad;Småhus friliggande",
    )
    assert assess_membership(row)["eligible"]
    assert not assess_membership(row | {"use_group": "multi_family"})["eligible"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"purpose": "Bostad;Småhus friliggande"}, "gis_purpose_disagrees"),
        (
            {"purpose": "Bostad;Småhus med flera lägenheter"},
            "gis_small_multi_definition_ambiguous",
        ),
        ({"purpose": "Komplementbyggnad;", "apartments": 0}, "fewer_than_three"),
        ({"apartments": 2}, "fewer_than_three"),
        ({"residential_percent": 98, "office_percent": 2}, "mixed_programme"),
        ({"residential_percent": None}, "primary_use_share_unknown"),
        ({"purpose2": "Verksamhet;"}, "gis_purpose_disagrees"),
        (
            {"residential_percent": 100, "school_percent": 1},
            "other_programme_share_positive",
        ),
        ({"semantic_compatibility": "incompatible"}, "link_semantic_incompatibility"),
        ({"certificate_to_footprint_count": 2}, "one_footprint"),
        ({"registry_year": 1990}, "cross_archetype_eras"),
        ({"era": "SE.03"}, "assigned_era_disagrees"),
        ({"observed_ventilation": "FTX"}, "assigned_ventilation_disagrees"),
    ],
)
def test_contradictions_cannot_enter_pure_cohort(changes, reason):
    result = assess_membership(residential(**changes))
    assert not result["eligible"]
    assert any(reason in value for value in result["reasons"])


def test_membership_is_energy_blind_and_does_not_use_size_to_relabel():
    row = residential()
    assert assess_membership(row) == assess_membership(
        row | {"target": 999999, "heat_kwh": -100, "atemp_m2": 80}
    )
    assert assess_membership(row | {"apartments": None})["eligible"]


def test_nonresidential_programmes_need_pure_share_and_compatible_gis():
    office = {
        "use_group": "office",
        "epc_category": "LOKALBYGGNADER",
        "office_percent": 100,
        "purpose": "Verksamhet;",
    }
    assert assess_membership(office)["eligible"]
    assert not assess_membership(office | {"purpose": "Samhällsfunktion;Skola"})[
        "eligible"
    ]
    school = office | {
        "use_group": "school",
        "office_percent": 0,
        "school_percent": 100,
        "purpose": "Samhällsfunktion;Skola",
    }
    assert assess_membership(school)["eligible"]
    assert not assess_membership(school | {"purpose": "Samhällsfunktion;Universitet"})[
        "eligible"
    ]


def test_source_hydration_checks_original_id_hash_and_omitted_fields(tmp_path):
    raw = {
        "FormularId": "12",
        "EgenByggnadsKat": "Flerbostadshus",
        "EgenAtempBostad": "98",
        "EgenAtempKontor": "2",
        "EgenAntalBolgh": "24",
        "EgenByggnadsTyp": "Friliggande",
    }
    path = tmp_path / "raw.txt"
    with path.open("w", encoding="utf-16", newline="") as stream:
        writer = csv.DictWriter(stream, list(raw), delimiter="|")
        writer.writeheader()
        writer.writerow(raw)
    row = {
        "formular_id": 12,
        "source_file": str(path),
        "source_line": 2,
        "source_row_sha256": hashlib.sha256(
            json.dumps(raw, sort_keys=True).encode()
        ).hexdigest(),
    }
    hydrated, sources = hydrate_source_membership([row])
    assert hydrated[0]["residential_percent"] == 98
    assert hydrated[0]["apartments"] == 24 and sources[str(path)]
    with pytest.raises(ValueError, match="checksum"):
        hydrate_source_membership([row | {"source_row_sha256": "changed"}])
    with pytest.raises(ValueError, match="formular ID"):
        hydrate_source_membership([row | {"formular_id": 11}])
    with pytest.raises(ValueError, match="disagrees"):
        hydrate_source_membership([row | {"residential_percent": 100}])


def test_preparation_quarantines_before_native_spec_or_targets_are_used(tmp_path):
    from epsm_workflows.campaign import atomic_json
    from epsm_workflows.cohort_transfer.preparation import prepare

    row = residential(purpose="Bostad;Småhus friliggande", apartments=1)
    row.update(
        building_id="mislinked",
        property_key="property",
        atemp_m2=100,
        target=100,
        correction_status="normal_use_adjusted_pre_weather_correction",
    )
    atomic_json(tmp_path / "cohort.json", [row])
    atomic_json(
        tmp_path / "specs.json", {"mislinked": str(tmp_path / "does-not-exist.json")}
    )
    atomic_json(
        tmp_path / "config.json",
        {"cohort_file": "cohort.json", "spec_manifest": "specs.json"},
    )
    result = prepare(tmp_path / "config.json", tmp_path / "campaign")
    assert json.loads((result / "features.json").read_text()) == []
    assert not list((result / "targets").glob("*.json"))
    audit = json.loads((result / "membership-audit.json").read_text())
    assert not audit[0]["eligible"]
    failures = json.loads((result / "preparation-failures.json").read_text())
    assert failures[0]["stage"] == "membership"


def test_national_candidate_filter_keeps_detached_mf_and_blocks_smallhouse():
    import pandas as pd
    from shapely.geometry import box

    from epsm_workflows.stock.prepare import simulation_candidates

    row = residential()
    row.update(
        building_id="apartment-block",
        heating_family="district_heat",
        heated_basement_floors=0,
        garage_percent=0,
        height_status="determined",
        representative_height_m=9,
        eave_height_m=None,
        ridge_height_m=None,
        roof_form="flat",
        storeys=3,
        gross_to_atemp_ratio=1.1,
        geometry_status="valid",
        outline="Fasad",
        age_conflict="none",
        era="SE.02",
        geometry_wkb_hex=box(0, 0, 20, 15).wkb_hex,
        designed_flow_l_s_m2=0.35,
    )
    smallhouse = row | {
        "building_id": "mislinked-smallhouse",
        "apartments": 1,
        "purpose": "Bostad;Småhus friliggande",
    }
    selected, failures = simulation_candidates(pd.DataFrame([row, smallhouse]))
    assert not failures
    assert list(selected.building_id) == ["apartment-block"]


def test_gis_leads_residential_assignment_without_silently_relabelling_epc():
    from epsm_workflows.archetype_membership import gis_assignment

    conflict = residential(purpose="Bostad;Småhus friliggande", apartments=1)
    assert gis_assignment(conflict)["use_group"] == "single_family"
    audit = assess_membership(conflict)
    assert audit["assigned_archetype_use"] == "single_family"
    assert not audit["eligible"]
    assert not assess_membership(residential(purpose=None))["eligible"]
    assert not assess_membership(residential(purpose="Bostad;"))["eligible"]


def test_multiple_gis_purposes_do_not_invent_area_weights():
    from epsm_workflows.archetype_membership import gis_assignment

    mixed = residential(purpose2="Verksamhet;Kontor")
    assignment = gis_assignment(mixed)
    assert assignment["status"] == "mixed"
    assert assignment["use_group"] is None
    assert not assignment["area_shares_inferred"]
    # Broad business labels still need the EPC's specific heated programme.
    office = {"use_group": "office", "office_percent": 100, "purpose": "Verksamhet;"}
    assert gis_assignment(office)["status"] == "unspecified"
    assert assess_membership(office)["assigned_archetype_use"] == "office"


def test_existing_source_design_flow_and_dated_measures_are_retained_without_u():
    from epsm_workflows.archetype_membership import source_operation_evidence

    raw = {
        "EgenProjVentFlode": "0,66",
        "AtgUtfordaUtfortAr": "2012",
        "AtgUtfordaByteFonster": "Ja",
    }
    evidence = source_operation_evidence(raw, 2018)
    assert evidence["designed_flow_l_s_m2"] == 0.66
    assert evidence["renovation_timing"] == "during_or_before"
    assert evidence["completed_window_replacement"] == "reported_before_or_during"
    assert evidence["reported_completed_measures"]["AtgUtfordaByteFonster"] == "Ja"
    assert "component_states" not in evidence and "window_u_value" not in evidence
    future = source_operation_evidence(raw, 2010)
    assert future["renovation_timing"] == "after_measurement"
    assert future["completed_window_replacement"] == "reported_after_measurement"
    undated = source_operation_evidence(raw | {"AtgUtfordaUtfortAr": ""}, 2018)
    assert undated["renovation_timing"] == "unknown"
    assert undated["completed_window_replacement"] == "undated_report"
    absent = source_operation_evidence(
        {"AtgUtfordaByteFonster": "Nej", "EgenProjVentFlode": "99"}, 2018
    )
    assert absent["completed_window_replacement"] == "unknown"
    assert absent["designed_flow_l_s_m2"] is None
