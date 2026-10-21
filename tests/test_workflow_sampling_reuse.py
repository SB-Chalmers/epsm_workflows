"""Workflow marginals adapt the existing public experiment-design functions."""

import hashlib

import pytest

from epsm_workflows import WorkflowSpec, design
from epsm_workflows.distributions import normal, sample_quantile


def lhs_workflow():
    return (
        WorkflowSpec.template("lhs", samples=7, seed=73)
        .vary("facade.wwr", min=0.1, max=0.4)
        .vary(
            "operation.heating_setpoint",
            min=18,
            max=23,
            distribution=normal(mean=20, std=2),
            scope="per_building",
        )
        .vary(
            "hvac.airflow.topology",
            choices=["S", "F", "FT", "FTX"],
            scope="per_group",
        )
    )


@pytest.mark.parametrize("scope", ["shared", "building-one", "group-two"])
def test_lhs_reuses_public_unit_design_with_exact_existing_scoped_seeds(
    monkeypatch, scope
):
    workflow = lhs_workflow()
    definition = workflow.to_dict()
    columns, expected_calls = [], []
    for parameter in definition["parameters"]:
        name = parameter["binding"]
        selected_scope = scope if parameter["scope"] != "shared" else "shared"
        seed = int.from_bytes(
            hashlib.sha256(f"73:{name}:{selected_scope}".encode()).digest(), "big"
        )
        probabilities = design.latin_hypercube(
            [design.Parameter(name, 0, 1)], 7, seed=seed
        )
        columns.append(
            [
                sample_quantile(
                    parameter["distribution"],
                    max(row[name], 1e-15),
                    lower=parameter["min"],
                    upper=parameter["max"],
                    choices=parameter["choices"],
                    bounds_policy=parameter["bounds_policy"],
                )
                for row in probabilities
            ]
        )
        expected_calls.append(([design.Parameter(name, 0, 1)], 7, seed))
    expected = [
        dict(zip((p["binding"] for p in definition["parameters"]), row))
        for row in zip(*columns)
    ]
    owner = design.latin_hypercube
    calls = []

    def observe(parameters, n, *, seed=0):
        calls.append((parameters, n, seed))
        return owner(parameters, n, seed=seed)

    monkeypatch.setattr(design, "latin_hypercube", observe)
    assert workflow.samples(scope=scope) == expected
    assert calls == expected_calls


@pytest.mark.parametrize("method", ["grid", "explicit"])
def test_finite_design_reuses_public_grid_without_changing_row_order(
    monkeypatch, method
):
    workflow = (
        WorkflowSpec.template("comparative")
        .set("sampling.method", method)
        .vary("facade.wwr", choices=[0.4, 0.2])
        .vary("hvac.airflow.topology", choices=["FTX", "S", "F"])
    )
    values = {"facade.wwr": [0.4, 0.2], "hvac.airflow.topology": ["FTX", "S", "F"]}
    expected = design.grid(values)
    owner = design.grid
    calls = []

    def observe(domains):
        calls.append(domains)
        return owner(domains)

    monkeypatch.setattr(design, "grid", observe)
    assert workflow.samples(scope="unused-scope") == expected
    assert calls == [values]


def test_empty_workflow_keeps_single_empty_sample():
    assert WorkflowSpec.template().samples() == [{}]
