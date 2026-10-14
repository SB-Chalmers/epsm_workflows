import pytest

from epsm_workflows.mf_parent_dynamic import certificate_scope_check


def source():
    return {
        "building_id": "building-one",
        "formular_id": 1042601,
        "property_key": "0582|PROPERTY",
        "source_row_sha256": "a" * 64,
        "certificate_to_footprint_count": 1,
    }


def evidence(row, **updates):
    record = {
        **{
            name: row[name]
            for name in (
                "building_id",
                "formular_id",
                "property_key",
                "source_row_sha256",
            )
        },
        "release_subject_count": 1,
        "archive_subject_count": 1,
    }
    return {row["building_id"]: record | updates}


@pytest.mark.parametrize("release,archive", [(2, 2), (1, 2), (2, 1)])
def test_one_linked_footprint_does_not_establish_single_building_energy(
    release, archive
):
    row = source()
    result = certificate_scope_check(
        row, evidence(row, release_subject_count=release, archive_subject_count=archive)
    )
    assert result["eligible"] is False
    assert any(
        reason.startswith("multiple_certificate_subjects:")
        for reason in result["reasons"]
    )


@pytest.mark.parametrize("value", [None, 0, -1, True, 1.0, "1"])
def test_unknown_or_noninteger_certificate_scope_is_quarantined(value):
    row = source()
    result = certificate_scope_check(row, evidence(row, archive_subject_count=value))
    assert result["eligible"] is False


@pytest.mark.parametrize(
    "name,value",
    [
        ("building_id", "another-building"),
        ("formular_id", 1042602),
        ("property_key", "0582|ANOTHER"),
        ("source_row_sha256", "b" * 64),
    ],
)
def test_single_subject_scope_evidence_must_match_selected_identity(name, value):
    row = source()
    result = certificate_scope_check(row, evidence(row, **{name: value}))
    assert result["eligible"] is False
    assert f"scope_identity_mismatch:{name}" in result["reasons"]


def test_scope_requires_evidence_and_accepts_matching_single_subject():
    row = source()
    assert certificate_scope_check(row, {})["eligible"] is False
    assert certificate_scope_check(row, evidence(row))["eligible"] is True
