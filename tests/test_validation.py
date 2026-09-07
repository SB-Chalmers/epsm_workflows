import math

import pytest
from epsm_workflows.validation import (
    accuracy,
    district_heat_estimate,
    district_heat_observation,
    measurement_period,
)


def row():
    return dict(
        FormularId="17",
        EgenAtemp="1000,5",
        EgiBeraknatVarde="Nej",
        EgiForstaArManad="1701",
        EgiSistaArManad="1712",
        ExpertGodkannande="2018-02-01 12:00:00",
        EgiFjarrvarme="100000",
        EgiVVBered="25000",
        EgiGruppFjarrvarme="Fördelat",
    )


def test_export_schemas_have_different_dhw_accounting():
    old = district_heat_observation(row(), schema="fore_v260")
    newer = row() | dict(EgiFjarrvarmeUPPV="75000", EgiFjarrvarmeVV="25000")
    new = district_heat_observation(newer, schema="efter_v260")
    assert old["reported_district_heat_kwh"] == new["reported_district_heat_kwh"] == 100000
    assert old["atemp_m2"] == 1000.5
    assert old["measurement_kind"] == "Fördelat"
    assert new["reporting_regime"] == "specific_energy_pre_2019"


@pytest.mark.parametrize(
    "patch",
    [
        {"EgiFjarrvarme": ""},
        {"EgiFjarrvarme": "NaN"},
        {"EgenAtemp": "0"},
        {"EgiVVBered": "100001"},
        {"EgiPumpMark": "1"},
        {"EgiBeraknatVarde": "Ja"},
        {"EgiGruppFjarrvarme": ""},
    ],
)
def test_incomparable_targets_fail(patch):
    with pytest.raises(ValueError):
        district_heat_observation(row() | patch, schema="fore_v260")


def test_missing_new_dhw_is_not_zero():
    with pytest.raises(ValueError, match="Missing EgiFjarrvarmeVV"):
        district_heat_observation(row() | {"EgiFjarrvarmeUPPV": "100000"}, schema="efter_v260")


def test_period_preserves_leap_and_cross_year_dates():
    a, b = measurement_period("1903", "2002")
    assert (str(a), str(b)) == ("2019-03-01", "2020-02-29")
    with pytest.raises(ValueError):
        measurement_period("2001", "2011")


def test_delivery_accounts_for_loss_and_efficiency_once():
    estimate = district_heat_estimate(
        8000,
        2000,
        100,
        space_efficiency=0.8,
        dhw_efficiency=1,
        airing_kwh_m2=4,
        net_distribution_loss_kwh_m2=5,
    )
    assert estimate["delivered_district_heat_kwh"] == 13000


def test_metrics_are_paired_and_bias_does_not_hide_scatter():
    metrics = accuracy([100, 200], [150, 150])
    assert metrics["bias_percent"] == 0
    assert metrics["rmse"] == 50
    assert metrics["mape_percent"] == 37.5
    assert metrics["r2"] == 0
    assert metrics["within_20_percent_count"] == 0


@pytest.mark.parametrize(
    "observed,predicted",
    [
        ([], []),
        ([1], []),
        ([0], [1]),
        ([math.nan], [1]),
        ([1], [math.inf]),
        ([1], [-1]),
    ],
)
def test_metrics_reject_invalid_pairs(observed, predicted):
    with pytest.raises(ValueError):
        accuracy(observed, predicted)
