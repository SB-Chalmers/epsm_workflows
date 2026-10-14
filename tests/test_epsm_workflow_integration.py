"""Cross-package physical branches and serialized EPSM evaluation contracts."""
import pytest

epsm = pytest.importorskip("epsm")

from epsm_data import RenovationMeasure
from epsm_data.domain import BuildingElement
from epsm_data.operations import ConstructionOperation
from epsm.results import EnergyResult, Evaluation
from epsm_lca import LCAResult
from epsm_workflows import optimise_renovations
from epsm_workflows.records import digest


def test_real_building_branch_lineage_resume_and_computed_objectives(tmp_path):
    baseline = epsm.Building()
    window = baseline.construction_set.elements[BuildingElement.WINDOW]
    measures = [RenovationMeasure(
        measure_id=f"windows-{u}", category="envelope", name=f"Window {u}",
        description="Synthetic physical choice for contract test", target="window",
        operations=(ConstructionOperation("replace_element", BuildingElement.WINDOW,
                                          element=window.with_changes(u_value=u)),),
    ) for u in (1.0, 1.5)]
    calls = []
    def evaluate(building):
        calls.append(building)
        value = next(e.construction.u_value for e in building.elements if e.role == BuildingElement.WINDOW)
        energy = EnergyResult(engine="synthetic", scalars={}, hourly={}, units={},
                              diagnostics={}, artifacts={}, delivered_by_carrier={"electricity": value * 100})
        carbon = LCAResult("complete", len(building.stages) * 10, 50, 50 + len(building.stages) * 10)
        return Evaluation(id=digest(building.to_dict()), state_id=building.snapshot().sha256,
                          path_id=digest(building.material_flows), energy_id=digest(value),
                          lca_id=digest(building.material_flows), energy=energy.to_dict(),
                          lca=carbon.to_dict())
    options = dict(bundles=[(m,) for m in measures], evaluator=evaluate,
                   evaluator_id="synthetic-contract-1", directory=tmp_path, max_depth=2)
    first = optimise_renovations(baseline, budget=2, **options)
    resumed = optimise_renovations(baseline, budget=5, **options)
    assert len(calls) == 5
    assert len(baseline.stages) == 0 and len(baseline.results) == 0
    assert first.nodes[1].evaluation_id == resumed.nodes[1].evaluation_id
    assert all(node.energy_kwh is not None and node.feasible for node in resumed.nodes)
    assert sorted(len(b.stages) for b in calls) == [0, 1, 1, 2, 2]
    for node in resumed.nodes:
        restored = epsm.Building.from_dict(node.building)
        assert len(restored.stages) == node.depth
        assert len(restored.material_flows) == node.depth
