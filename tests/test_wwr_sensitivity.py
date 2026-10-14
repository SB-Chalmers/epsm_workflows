"""Check isolation of the WWR experiment on real frozen cohort inputs."""

import json
from pathlib import Path

import pytest

from epsm_workflows import wwr_sensitivity as experiment


@pytest.mark.parametrize("state", ["existing", "standard", "advanced"])
def test_only_wwr_changes_and_both_models_compile(state):
    from epsm_core import EnergyPlusCompiler

    root = (
        Path(__file__).parents[1]
        / "output/archetype-diagnostics-mf-se02-2026-10-01-reviewed"
    )
    if not (root / "cohort.json").exists():
        pytest.skip("Frozen local cohort not available")
    row = json.loads((root / "cohort.json").read_text())[0]
    weather = json.loads((root / "audit.json").read_text())["source_weather"]
    a = experiment.build_spec(row, state, weather, 0.2)
    b = experiment.build_spec(row, state, weather, 0.3)
    assert a.model_dump(exclude={"facade"}) == b.model_dump(exclude={"facade"})
    assert a.facade.model_dump(exclude={"default_wwr"}) == b.facade.model_dump(
        exclude={"default_wwr"}
    )
    assert a.envelope.foundation.method == b.envelope.foundation.method == "kiva"
    assert a.physical_hash() != b.physical_hash()
    compiler = EnergyPlusCompiler(
        "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus"
    )
    ma, ra = compiler.compile(a)
    mb, rb = compiler.compile(b)
    assert ra.data["conditioned_floor_area_m2"] == rb.data["conditioned_floor_area_m2"]
    assert ra.data["conditioned_volume_m3"] == rb.data["conditioned_volume_m3"]
    assert ma["Material"] == mb["Material"]
    assert (
        ma["ZoneInfiltration:DesignFlowRate"] == mb["ZoneInfiltration:DesignFlowRate"]
    )
    assert ma["Foundation:Kiva:Settings"] == mb["Foundation:Kiva:Settings"]
    assert ra.data["facades"] != rb.data["facades"]
