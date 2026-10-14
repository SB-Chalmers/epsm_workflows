"""Bounded public inspection uses only already loaded metadata."""
from test_harmonised_workflows import Building, result


def test_public_workflow_representations_are_bounded_without_io(monkeypatch):
    from pathlib import Path
    from epsm_workflows import PreparedEvaluation, optimise_renovations, VeraExecutor
    from epsm_workflows.design import Parameter, sensitivity_design

    tree = optimise_renovations(Building(), bundles=[], max_depth=0,
        evaluator=lambda b: result(b, 100, 50))
    tree.nodes[0].evaluation["hourly"] = {"energy": [123.456] * 8760}
    unusual = "\\\n'🪟" * 100
    objects = [tree, tree.nodes[0], Parameter(unusual, 0, 1),
               PreparedEvaluation({"secret": unusual}, name=unusual,
               files={"weather": "/missing/file.epw"}),
               sensitivity_design([Parameter(unusual, 0, 1)], 2),
               VeraExecutor("test-host", "/scratch/" + "x" * 300)]

    def no_io(*args, **kwargs):
        raise AssertionError("Representation must use loaded metadata")

    monkeypatch.setattr(Path, "read_bytes", no_io)
    for value in objects:
        for text in (repr(value), str(value)):
            assert len(text) <= 160
            assert "\n" not in text
            assert "123.456" not in text
        assert isinstance(value.summary(), dict)
