"""Ensure the new diagnostic cohort uses the correct frozen catalogue and no energy fitting."""
import json
from pathlib import Path

import pytest

from epsm_workflows.clean_cohort_test import build_spec

COHORT = Path('output/clean-cohort-gothenburg-mf-2026-10-02-v3/cohort.json')
WEATHER = Path('output/clean-cohort-mf-se05-baseline-2026-10-02/weather')


@pytest.mark.skipif(not COHORT.exists() or not WEATHER.exists(), reason='Local pinned diagnostic artifacts unavailable')
@pytest.mark.parametrize('state', ['existing', 'standard', 'advanced'])
def test_new_cohort_catalogue_and_energy_blind_inputs(state):
    from epsm_data import ArchetypeKey, load_parametric_catalogue
    from epsm_data.release import core_inputs

    row = json.loads(COHORT.read_text())[0]
    spec = build_spec(row, state, WEATHER)
    changed = build_spec(row | {'heat_kwh': row['heat_kwh'] * 100}, state, WEATHER)
    assert spec == changed
    source = core_inputs('mf_se05', state)
    assert spec.envelope.wall == source['envelope'].wall
    assert spec.envelope.top == source['envelope'].top
    assert spec.envelope.ground == source['envelope'].ground
    assert spec.facade.glazing == source['glazing']
    expected = load_parametric_catalogue().get_slot(ArchetypeKey('multi_family', 'SE.05', 'SE.Zone3', state, 'F')).infiltration.value
    assert spec.airflow.envelope_infiltration_ach == expected
    assert spec.envelope.ground_temperature is None
    assert spec.envelope.foundation.method == 'kiva'
    assert spec.geometry.basement_depth_m == 0
    ratio = row['atemp_m2'] / spec.geometry.conditioned_floor_area_m2
    assert spec.airflow.mechanical_vent_l_s_m2 == pytest.approx(.35 * ratio)
    assert spec.climate.calendar_year == row['measurement_year']


@pytest.mark.skipif(not COHORT.exists() or not WEATHER.exists(), reason='Local pinned diagnostic artifacts unavailable')
def test_exploration_explicit_catalogue_ventilation_and_fixed_recovery():
    from epsm_data.release import core_inputs
    row = json.loads(COHORT.read_text())[0] | {'archetype_id': 'mf_se03'}
    spec = build_spec(row, 'existing', WEATHER, archetype_id='mf_se03', ventilation_system='FTX', recovery_effectiveness=.7)
    assert spec.envelope.wall == core_inputs('mf_se03', 'existing')['envelope'].wall
    assert spec.airflow.topology == 'FTX'
    assert spec.airflow.sensible_recovery_effectiveness == .7
    assert spec == build_spec(row | {'heat_kwh':1}, 'existing', WEATHER, archetype_id='mf_se03', ventilation_system='FTX', recovery_effectiveness=.7)
    with pytest.raises(ValueError, match='recovery'):
        build_spec(row, 'existing', WEATHER, archetype_id='mf_se03', ventilation_system='FT', recovery_effectiveness=.7)
