"""Fixed BEN household reference, calendar conservation and exact inversion."""

import math

import pytest
from epsm_core import (
    AirflowSpec,
    BuildingSpec,
    ClimateSpec,
    FacadeSpec,
    GeometrySpec,
    ScheduleSpec,
    ZoneUseSpec,
    load_sveby_spec,
)
from epsm_data import resolve_building_archetype
from shapely.geometry import box

from epsm_workflows.mf_normal_use import (
    BEN_HOUSEHOLD_BASIS,
    SVEBY_HOUSEHOLD_BASIS,
    ben_household_reference,
    current_sveby_reference,
    household_reference_provenance,
)


def reference_spec(year, *, legacy_daily=False):
    physical = resolve_building_archetype(
        category="multi_family", construction_year=1965,
        climate_zone="SE.Zone3", ventilation="F",
    )
    standard = load_sveby_spec()
    use = standard.use_spec(calendar_year=None if legacy_daily else year)
    common = standard.common_use_spec()
    return BuildingSpec(
        identifier="fixed-reference",
        geometry=GeometrySpec.from_footprint(
            box(0, 0, 20, 10), storey_elevations_m=(0, 3, 6)
        ).updated(basement_depth_m=2.5, basement_conditioned=True),
        envelope=physical["envelope"],
        facade=FacadeSpec(
            default_wwr=0.2, glazing=physical["glazing"], solar_shading_factor=0.5
        ),
        use=use,
        common_use=common,
        zone_uses=(ZoneUseSpec(
            zone_kind="basement", use=common,
            source="Fixed service-space use; retained in the household comparison",
        ),),
        airflow=AirflowSpec(
            topology="F", mechanical_vent_l_s_m2=0.35, **physical["core_airflow_inputs"]
        ),
        climate=ClimateSpec(
            epw_path="unused.epw", weather_sha256="0" * 64,
            latitude_deg=57, longitude_deg=12, elevation_m=0,
            time_zone_hours=1, calendar_year=year,
        ),
        load_reference_area_m2=550,
    )


def useful_gain(spec):
    return spec.use.equipment_gain_w_m2 * math.fsum(
        spec.use.equipment_fraction.hourly(spec.climate.calendar_year)
    ) / 1000


@pytest.mark.parametrize("year", [2019, 2020])
def test_real_sveby_common_and_leap_years_conserve_fixed_annual_ben_gain(year):
    original = reference_spec(year)
    assert useful_gain(original) == pytest.approx(14.7, abs=1e-10)
    changed = ben_household_reference(original)
    assert useful_gain(changed) == pytest.approx(21.0, abs=1e-10)
    assert changed.use.equipment_gain_w_m2 == pytest.approx(
        original.use.equipment_gain_w_m2 * 30 / 21, abs=1e-10
    )
    assert changed.use.equipment_fraction == original.use.equipment_fraction
    assert len(changed.use.equipment_fraction.hourly(year)) == (
        8784 if year == 2020 else 8760
    )
    restored = current_sveby_reference(changed)
    assert restored == original
    assert restored.physical_hash() == original.physical_hash()
    assert changed.physical_hash() != original.physical_hash()


@pytest.mark.parametrize("year", [2019, 2020])
def test_legacy_daily_rounding_is_normalised_for_ben_and_restored_exactly(year):
    original = reference_spec(year, legacy_daily=True)
    assert original.use.equipment_gain_w_m2 == 1.68
    changed = ben_household_reference(original)
    assert useful_gain(changed) == pytest.approx(21.0, abs=1e-10)
    assert changed.use.equipment_fraction == original.use.equipment_fraction
    restored = current_sveby_reference(changed)
    assert restored.use.equipment_gain_w_m2 == 1.68
    assert restored.physical_hash() == original.physical_hash()


def test_only_equipment_gain_changes_all_other_use_geometry_and_area_stay_fixed():
    original = reference_spec(2020)
    before = original.model_dump()
    changed = ben_household_reference(original)
    after = changed.model_dump()
    assert before["use"].pop("equipment_gain_w_m2") != after["use"].pop(
        "equipment_gain_w_m2"
    )
    assert after == before
    assert changed.load_reference_area_m2 == 550
    assert changed.geometry == original.geometry
    assert changed.common_use == original.common_use
    assert changed.zone_uses == original.zone_uses
    assert changed.common_use.equipment_gain_w_m2 == 0
    assert changed.zone_uses[0].use.equipment_gain_w_m2 == 0


def test_declared_already_converted_input_is_validated_and_never_double_scaled():
    original = reference_spec(2019)
    ben = ben_household_reference(original)
    assert ben_household_reference(ben, source_basis=BEN_HOUSEHOLD_BASIS) is ben
    assert current_sveby_reference(
        original, source_basis=SVEBY_HOUSEHOLD_BASIS
    ) is original
    with pytest.raises(ValueError, match="contradicts declared reference"):
        ben_household_reference(ben)
    with pytest.raises(ValueError, match="contradicts declared reference"):
        current_sveby_reference(original)
    with pytest.raises(ValueError, match="contradicts declared reference"):
        ben_household_reference(original, source_basis=BEN_HOUSEHOLD_BASIS)


@pytest.mark.parametrize("convert", [ben_household_reference, current_sveby_reference])
@pytest.mark.parametrize("basis", ["fitted_gain", "", None])
def test_unknown_reference_basis_fails(convert, basis):
    with pytest.raises(ValueError, match="Unknown household-gain reference"):
        convert(reference_spec(2019), source_basis=basis)


def test_modified_source_scalar_and_timing_fail_instead_of_absorbing_them():
    original = reference_spec(2019)
    with pytest.raises(ValueError, match="contradicts declared reference"):
        ben_household_reference(original.updated(
            use=original.use.updated(equipment_gain_w_m2=2)
        ))
    with pytest.raises(ValueError, match="timing must match"):
        ben_household_reference(original.updated(
            use=original.use.updated(equipment_fraction=ScheduleSpec.constant(0.5))
        ))
    with pytest.raises(ValueError, match="timing must match|calendar differs"):
        ben_household_reference(original.updated(
            climate=original.climate.updated(calendar_year=2020)
        ))


def test_nonresidential_and_second_heat_fraction_fail():
    original = reference_spec(2019)
    with pytest.raises(ValueError, match="requires residential use"):
        ben_household_reference(original.updated(
            use=original.use.updated(occupancy_category="non_residential")
        ))
    for update in ({"equipment_heat_fraction": 0.7}, {
        "equipment_heat_fraction_schedule": ScheduleSpec.constant(0.7)
    }):
        with pytest.raises(ValueError, match="already represent useful heat"):
            ben_household_reference(original.updated(use=original.use.updated(**update)))


def test_provenance_declares_proxy_and_no_individual_target_normalisation_claim():
    declared = household_reference_provenance()
    assert declared["household_electricity_kwh_m2_year"] == 30
    assert declared["useful_household_gain_kwh_m2_year"] == 21
    assert declared["thermally_available_fraction"] == 0.7
    assert declared["current_catalogue_useful_gain_kwh_m2_year"] == 14.7
    assert declared["individual_target_normalization_verified"] is False
    assert declared["measured_target_changed"] is False
    assert "proxy" in declared["timing_proxy"] or "Sveby" in declared["timing_proxy"]
    assert declared["source"].startswith("https://www.boverket.se/")
    declared["useful_household_gain_kwh_m2_year"] = 999
    assert household_reference_provenance()["useful_household_gain_kwh_m2_year"] == 21
