"""Offline checks for diverse, calendar-explicit EPSM Core weather-study cases."""

import csv
import json
from datetime import datetime, timedelta

import pytest

pytest.importorskip("epsm_core")

from examples.weather_imputation_sensitivity import archetypes as A


@pytest.fixture
def weather_files(tmp_path):
    """Complete deterministic actual-year EPWs, without downloads or EnergyPlus."""
    paths = {}
    for year in (2023, 2024):
        path = tmp_path / f"weather-{year}.epw"
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                ["LOCATION", "Study", "", "SWE", "test", "0", 57.7, 12, 1, 100]
            )
            for header in (
                "DESIGN CONDITIONS",
                "TYPICAL/EXTREME PERIODS",
                "GROUND TEMPERATURES",
            ):
                writer.writerow([header, 0])
            writer.writerow(["HOLIDAYS/DAYLIGHT SAVINGS", "No", 0, 0, 0])
            writer.writerow(["COMMENTS 1", "Synthetic test fixture"])
            writer.writerow(["COMMENTS 2", ""])
            writer.writerow(["DATA PERIODS", 1, 1, "Data", "Sunday", "1/1", "12/31"])
            current = datetime(year, 1, 1)
            while current.year == year:
                row = [year, current.month, current.day, current.hour + 1, 60, ""] + [
                    0
                ] * 29
                for column, value in {
                    6: 5,
                    7: 0,
                    8: 70,
                    9: 101325,
                    12: 300,
                    20: 180,
                    21: 3,
                }.items():
                    row[column] = value
                writer.writerow(row)
                current += timedelta(hours=1)
        paths[year] = path
    return paths


def test_profiles_are_distinct_and_have_explicit_boundaries(weather_files):
    specs = [
        A.make_archetype_spec(weather_files[2023], 2023, key) for key in A.ARCHETYPES
    ]
    assert len(specs) == len({spec.physical_hash() for spec in specs}) == 6
    assert len({A.archetype_hash(key) for key in A.ARCHETYPES}) == 6
    for key, spec in zip(A.ARCHETYPES, specs):
        description = A.describe_archetype(key)
        assert spec.geometry.conditioned_floor_area_m2 == pytest.approx(
            description["floor_area_m2"]
        )
        assert spec.geometry.conditioned_volume_m3 == pytest.approx(
            description["volume_m3"]
        )
        assert spec.load_reference_area_m2 == description["floor_area_m2"]
        assert spec.envelope.foundation.method == "prescribed_temperature"
        assert spec.envelope.ground_temperature.monthly_temperatures_c == (18.0,) * 12
        assert "uncalibrated" in spec.envelope.ground_temperature.source
        assert spec.use.cooling_enabled and spec.use.dhw_kwh_m2_year == 0
        assert spec.airflow.heating_availability.hourly(2023) == (1.0,) * 8760
        assert spec.airflow.cooling_availability.hourly(2023) == (1.0,) * 8760
        assert spec.use.occupancy_fraction.weekday_values is not None
        assert spec.geometry.zoning.mode == "per_storey"
        assert json.loads(json.dumps(description))["common_assumptions"]["limitations"]


@pytest.mark.parametrize("key", ["office_glazed", "school_daytime"])
def test_daytime_programmes_use_real_weekdays_and_setbacks(weather_files, key):
    spec = A.make_archetype_spec(weather_files[2023], 2023, key)
    # 2023-01-02 is Monday,2023-01-07 is Saturday. Jan1 is Sunday.
    monday_09, saturday_09 = 24 + 9, 6 * 24 + 9
    occupancy = spec.use.occupancy_fraction.hourly(2023)
    ventilation = spec.airflow.mechanical_vent_fraction.hourly(2023)
    heating = spec.use.heating_setpoint.hourly(2023)
    assert occupancy[monday_09] == 1 and occupancy[saturday_09] == 0
    assert ventilation[monday_09] == 1 and ventilation[saturday_09] == 0.05
    assert heating[monday_09] == 21 and heating[saturday_09] == 17
    # Actual calendar expansion includes leap day; weekday identity shifts by year.
    leap = A.make_archetype_spec(weather_files[2024], 2024, key)
    assert len(leap.use.occupancy_fraction.hourly(2024)) == 8784
    assert leap.use.occupancy_fraction.hourly(2024)[9] == 1  # Jan1 Monday.
    assert spec.physical_hash() != leap.physical_hash()


def test_leakage_is_separate_documented_and_weather_responsive(weather_files):
    old = A.make_archetype_spec(weather_files[2023], 2023, "detached_old_light")
    new = A.make_archetype_spec(weather_files[2023], 2023, "detached_new_heavy")
    c, t, v, w = old.airflow.infiltration_coefficients
    assert old.airflow.infiltration_source and v > 0 and t > 0
    assert c + t * 20 + v * 2.5 + w * 2.5**2 == pytest.approx(1)
    assert c + t * 20 + v * 8 + w * 8**2 > 1
    assert old.airflow.topology == "S" and old.airflow.mechanical_vent_l_s_m2 == 0
    assert old.airflow.natural_exchange_ach > 0
    assert new.airflow.infiltration_coefficients == (1, 0, 0, 0)
    assert new.airflow.sensible_recovery_effectiveness == 0.8
    assert new.airflow.latent_recovery_effectiveness == 0


def test_physical_identity_ignores_host_path_but_tracks_weather(
    weather_files, tmp_path
):
    original = weather_files[2023]
    copied = tmp_path / "another-host-name.epw"
    copied.write_bytes(original.read_bytes())
    first = A.make_archetype_spec(original, 2023, "multifamily_old")
    second = A.make_archetype_spec(copied, 2023, "multifamily_old")
    assert first.climate.epw_path != second.climate.epw_path
    assert first.physical_hash() == second.physical_hash()
    rows = list(csv.reader(copied.read_text().splitlines()))
    rows[8][6] = "6"
    with copied.open("w", newline="") as stream:
        csv.writer(stream).writerows(rows)
    changed = A.make_archetype_spec(copied, 2023, "multifamily_old")
    assert first.physical_hash() != changed.physical_hash()


def test_metadata_returns_defensive_copies():
    original_hash = A.archetype_hash("office_glazed")
    description = A.describe_archetype("office_glazed")
    description["wall_layers"][0][1] = 99
    description["common_assumptions"]["materials"]["concrete"]["conductivity_w_mk"] = 99
    assert A.archetype_hash("office_glazed") == original_hash


def test_site_year_ground_is_explicit_and_can_be_frozen_across_profiles(weather_files):
    ground = (-0.5,) * 12
    hashes = []
    for key in A.ARCHETYPES:
        spec = A.make_archetype_spec(
            weather_files[2023], 2023, key, ground_temperatures_c=ground
        )
        assert spec.envelope.ground_temperature.monthly_temperatures_c == ground
        assert "caller_supplied_frozen" in spec.envelope.ground_temperature.source
        hashes.append(spec.physical_hash())
    assert len(set(hashes)) == 6
    default = A.make_spec(weather_files[2023])
    supplied = A.make_spec(weather_files[2023], ground_temperatures_c=ground)
    assert default.physical_hash() != supplied.physical_hash()
    with pytest.raises(ValueError, match="12"):
        A.make_spec(weather_files[2023], ground_temperatures_c=(0.0,) * 11)


@pytest.mark.parametrize("year", [True, "2023", 1899, 2101])
def test_invalid_years_fail_before_file_access(year):
    with pytest.raises(ValueError, match="integer actual calendar"):
        A.make_archetype_spec("missing.epw", year, "office_glazed")


def test_wrong_actual_year_and_unknown_profile_are_rejected(weather_files, tmp_path):
    # Core ignores source-year labels for TMY calendars; this experiment must not.
    mixed = tmp_path / "mixed.epw"
    rows = list(csv.reader(weather_files[2023].read_text().splitlines()))
    rows[8][0] = "2022"
    with mixed.open("w", newline="") as stream:
        csv.writer(stream).writerows(rows)
    with pytest.raises(ValueError, match="requested AMY year"):
        A.make_archetype_spec(mixed, 2023, "office_glazed")
    with pytest.raises(ValueError, match="Unknown archetype"):
        A.make_archetype_spec(weather_files[2023], 2023, "missing")
    assert (
        A.make_spec(weather_files[2023]).physical_hash()
        == A.make_archetype_spec(
            weather_files[2023], 2023, "detached_new_heavy"
        ).physical_hash()
    )
