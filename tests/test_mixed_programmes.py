"""An EPC mixture must not fabricate programmes, fractions or linkage evidence."""

import pytest
from epsm_core.specs import AirflowSpec, ScheduleSpec, UseSpec

from epsm_workflows.mixed_programmes import program_blend_from_epc


def profiles():
    use = UseSpec(
        heating_setpoint=ScheduleSpec.constant(21),
        occupant_density_per_m2=0.02,
        equipment_gain_w_m2=2,
        dhw_kwh_m2_year=25,
    )
    air = AirflowSpec(topology="F", envelope_infiltration_ach=0.2)
    return {
        "residential": (use, air, "Synthetic residential"),
        "office": (use.updated(equipment_gain_w_m2=10), air, "Synthetic office"),
    }


def test_epc_programme_area_shares_are_used_without_energy_or_gis_order():
    row = {"residential_percent": 80, "office_percent": 20}
    blend = program_blend_from_epc(
        row, profiles(), source="Existing shares; shared height"
    )
    other = program_blend_from_epc(
        row | {"target": 9999, "purpose": "Office;Residential"},
        profiles(),
        source="Existing shares; shared height",
    )
    assert blend == other
    assert blend.resolve(calendar_year=2024)[0].equipment_gain_w_m2 == pytest.approx(
        3.6
    )


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"office_percent": 10}, "sum to 100"),
        ({"office_percent": -20}, "outside"),
        ({"office_percent": "invalid"}, "Invalid"),
        ({"office_percent": 0, "retail_percent": 20}, "No sourced programme"),
        ({"garage_percent": 2}, "garage"),
        ({"semantic_compatibility": "incompatible"}, "incompatible"),
        ({"certificate_to_footprint_count": 2}, "one footprint"),
    ],
)
def test_incomplete_unsupported_or_mislinked_mixtures_are_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        program_blend_from_epc(
            {"residential_percent": 80, "office_percent": 20} | changes,
            profiles(),
            source="Synthetic test",
        )
