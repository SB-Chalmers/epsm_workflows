"""Bounded public inspection uses only already loaded metadata."""

from test_harmonised_workflows import Building, result


def test_public_workflow_representations_are_bounded_without_io(monkeypatch):
    from pathlib import Path

    from epsm_workflows import PreparedEvaluation, VeraExecutor, optimise_renovations
    from epsm_workflows.design import Parameter, sensitivity_design

    tree = optimise_renovations(
        Building(), bundles=[], max_depth=0, evaluator=lambda b: result(b, 100, 50)
    )
    tree.nodes[0].evaluation["hourly"] = {"energy": [123.456] * 8760}
    unusual = "\\\n'🪟" * 100
    objects = [
        tree,
        tree.nodes[0],
        Parameter(unusual, 0, 1),
        PreparedEvaluation(
            {"secret": unusual}, name=unusual, files={"weather": "/missing/file.epw"}
        ),
        sensitivity_design([Parameter(unusual, 0, 1)], 2),
        VeraExecutor("test-host", "/scratch/" + "x" * 300),
    ]

    def no_io(*args, **kwargs):
        raise AssertionError("Representation must use loaded metadata")

    monkeypatch.setattr(Path, "read_bytes", no_io)
    for value in objects:
        for text in (repr(value), str(value)):
            assert len(text) <= 160
            assert "\n" not in text
            assert "123.456" not in text
        assert isinstance(value.summary(), dict)


def test_typed_study_inputs_outputs_and_distributions_have_safe_inspection(monkeypatch):
    import socket
    import subprocess
    from pathlib import Path

    from epsm_workflows import OutputRequirement, ParameterSpec
    from epsm_workflows.distributions import categorical, normal

    unusual = "\\\n'🪟" * 100
    parameter = ParameterSpec(
        binding=unusual,
        kind="categorical",
        choices=[
            {"label": str(index), "private": "never-print-this-record"}
            for index in range(2048)
        ],
        baseline={"private": "never-print-this-record"},
    )
    output = OutputRequirement(
        name=unusual, resolution="hourly", required=False, unit=unusual
    )
    distribution = categorical(probabilities=[1 / 2048] * 2048)
    objects = [parameter, output, distribution, normal(mean=20, std=1)]
    originals = [value.model_dump(mode="json") for value in objects]

    def no_work(*args, **kwargs):
        raise AssertionError("Inspection must use loaded metadata only")

    monkeypatch.setattr(Path, "read_bytes", no_work)
    monkeypatch.setattr(Path, "read_text", no_work)
    monkeypatch.setattr(socket, "create_connection", no_work)
    monkeypatch.setattr(subprocess, "run", no_work)
    for value, original in zip(objects, originals):
        for text in (repr(value), str(value)):
            assert len(text) <= 160
            assert "\n" not in text
            assert type(value).__name__ in text
            assert "never-print-this-record" not in text
        assert isinstance(value.summary(), dict)
        assert value.model_dump(mode="json") == original
        assert type(value).model_validate(original) == value
    assert "2048" in repr(parameter)
    assert "2048" in repr(distribution)
    assert "hourly" in repr(output)
    assert parameter.summary()["binding"] == unusual
    detached = parameter.summary()
    detached["choices"][0]["label"] = "changed"
    assert parameter.choices[0]["label"] == "0"
