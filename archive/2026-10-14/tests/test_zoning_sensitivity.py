"""Scientific isolation of cleanup and zoning on the pinned diagnostic cohort."""
import json
import pytest
from epsm_workflows import zoning_sensitivity as z

@pytest.mark.parametrize('state',['existing','standard','advanced'])
def test_frozen_inputs_and_load_totals(state):
    if not (z.SOURCE/'cohort.json').exists():pytest.skip('Local diagnostic cohort unavailable')
    row=json.loads((z.SOURCE/'cohort.json').read_text())[0]
    weather=json.loads((z.SOURCE/'audit.json').read_text())['source_weather']
    base=z.build_spec(row,state,weather,'per_storey')
    for mode in z.MODES[2:]:
        b=z.build_spec(row,state,weather,mode)
        assert b.geometry.zoning.on_failure=='error'
        assert b.envelope==base.envelope
        assert b.facade==base.facade
        assert b.airflow==base.airflow
        assert b.use==base.use
    old=z.build_spec(row,state,weather,'original_per_storey')
    assert not old.geometry.footprint_cleanup.enabled
    assert old.airflow.envelope_infiltration_ach==base.airflow.envelope_infiltration_ach
    for key in ('occupant_density_per_m2','equipment_gain_w_m2','lighting_gain_w_m2','dhw_kwh_m2_year'):
        assert getattr(old.use,key)*old.geometry.conditioned_floor_area_m2==pytest.approx(getattr(base.use,key)*base.geometry.conditioned_floor_area_m2)
    assert old.airflow.mechanical_vent_l_s_m2*old.geometry.conditioned_floor_area_m2==pytest.approx(base.airflow.mechanical_vent_l_s_m2*base.geometry.conditioned_floor_area_m2)
