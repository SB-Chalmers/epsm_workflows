"""Joint identifiability across shared operating and construction controls."""

import pytest

from epsm_workflows import mf_parent_dynamic as study
from epsm_workflows.campaign import atomic_json


def sources(count=5):
    return [
        {
            "building_id": f"building-{i}",
            "property_key": f"property-{i}",
            "era": "SE.02",
            "base_heat": 120.0,
            "axis_index": i % 5,
        }
        for i in range(count)
    ]


class ResponseEvaluator:
    """Native stand-in whose physical response columns have known rank."""

    def __init__(self, rows, *, rank_two=False):
        self.rank_two = rank_two
        self.calls = []
        self.registers = {
            r["building_id"]: {
                "infiltration_multiplier": {
                    "bounds": [0.5, 2.0],
                    "baseline": 1.0,
                    "status": "screen_and_cohort_calibration",
                },
                "window_u_multiplier": {
                    "bounds": [0.4, 1.2],
                    "baseline": 1.0,
                    "status": "screen_and_cohort_calibration",
                },
                "wall_resistance": {
                    "bounds": [0.8, 1.6],
                    "baseline": 1.0,
                    "status": "screen_and_cohort_calibration",
                    "resistance_basis": "total_assembly_r",
                },
            }
            for r in rows
        }

    def prediction(self, row, settings):
        i = row["axis_index"]
        # With rank_two=True each fabric column is separately independent of
        # operating, but all three span just two response directions.
        operating = 6.0 if self.rank_two else 1.0
        window = i + 1.0 if self.rank_two else 1.0
        wall = 5.0 - i
        return (
            row["base_heat"]
            + 10 * operating * (settings.get("infiltration_multiplier", 1.0) - 1)
            + 30 * window * (settings.get("window_u_multiplier", 1.0) - 1)
            + 40 * wall * (1 / settings.get("wall_resistance", 1.0) - 1)
        )

    def batch(self, rows, designs):
        self.calls.append(([r["building_id"] for r in rows], dict(designs)))
        return {
            (r["building_id"], name): self.prediction(r, settings)
            for r in rows
            for name, settings in designs.items()
        }


def screening(tmp_path, monkeypatch, rows, evaluator):
    atomic_json(tmp_path / "features.json", rows)
    monkeypatch.setattr(study, "fit_group", lambda rows, count: {})
    monkeypatch.setattr(
        study, "representatives", lambda rows, *args, **kwargs: rows[:5]
    )
    return study.screen(tmp_path, evaluator)


def test_joint_screen_removes_operating_window_confounding_in_pool_and_era(
    tmp_path, monkeypatch
):
    rows = sources()
    result = screening(tmp_path, monkeypatch, rows, ResponseEvaluator(rows))
    assert result["operating"]["active"] == ["infiltration_multiplier"]
    assert result["pooled_fit_controls"] == ["infiltration_multiplier"]
    era = result["fabric_by_era"]["SE.02"]
    assert era["active"] == ["wall_resistance"]
    assert era["joint_independence"]["active"] == [
        "infiltration_multiplier",
        "wall_resistance",
    ]
    for diagnostic in (result["pooled_independence"], era["joint_independence"]):
        assert any(
            x["parameter"] == "window_u_multiplier"
            and "infiltration_multiplier" in x["with"]
            for x in diagnostic["confounding"]
        )
    assert result["observed_heating_used"] is False


def test_joint_screen_detects_three_column_confounding_despite_pairwise_support(
    tmp_path, monkeypatch
):
    rows = sources()
    evaluator = ResponseEvaluator(rows, rank_two=True)
    result = screening(tmp_path, monkeypatch, rows, evaluator)
    assert set(result["pooled_fit_controls"]) == {
        "infiltration_multiplier",
        "window_u_multiplier",
    }
    era = result["fabric_by_era"]["SE.02"]
    # Fabric-only screening supports both. The combined operating/fabric
    # matrix cannot identify three coefficients from its rank-two responses.
    assert not era["confounding"]
    assert len(era["active"]) == 1
    joint = era["joint_independence"]
    assert len(joint["active"]) == 2
    assert "infiltration_multiplier" in joint["active"]
    assert len(joint["confounding"]) == 1
    assert len(joint["confounding"][0]["with"]) == 2


@pytest.mark.parametrize("scope", ["pooled", "era"])
def test_confounded_diagnostic_window_axis_is_held_at_prior_during_fitting(
    tmp_path, monkeypatch, scope
):
    rows = sources(40)
    evaluator = ResponseEvaluator(rows)
    result = screening(tmp_path, monkeypatch, rows, evaluator)
    models = study.emulators(tmp_path, evaluator, result)
    model = models["SE.02"]
    # Rejected controls remain diagnostic evidence, not emulator directions.
    assert "window_u_multiplier" not in model["active"]
    assert "window_u_multiplier" not in model["fit_fabric_controls"]
    assert "window_u_multiplier" not in model["pooled_fit_controls"]
    assert model["maximum_validated_relative_error"] < 1e-12
    evaluator.calls.clear()
    for row in rows:
        row["target"] = evaluator.prediction(row, {"window_u_multiplier": 0.4})
    fitted = study.fit_parents(
        rows,
        (),
        evaluator,
        models,
        scope=scope,
        output=tmp_path / "fitted.json",
    )
    assert fitted["pooled"].get("window_u_multiplier", 1.0) == 1.0
    assert all(
        parameters.get("window_u_multiplier", 1.0) == 1.0
        for parameters in fitted["by_era"].values()
    )
    assert all(
        parameters.get("window_u_multiplier", 1.0) == 1.0
        for _, designs in evaluator.calls
        for parameters in designs.values()
    )


def test_independent_roof_backfills_confounded_wall_window_direction(
    tmp_path, monkeypatch
):
    rows = sources()

    class RoofEvaluator(ResponseEvaluator):
        def __init__(self, rows):
            super().__init__(rows)
            for register in self.registers.values():
                register["roof_resistance"] = dict(register["wall_resistance"])

        def prediction(self, row, settings):
            i = row["axis_index"]
            return (
                row["base_heat"]
                + 10 * (settings.get("infiltration_multiplier", 1.0) - 1)
                + 40 * (5 - i) * (1 / settings.get("wall_resistance", 1.0) - 1)
                + 10 * (5 - i) * (settings.get("window_u_multiplier", 1.0) - 1)
                + 9
                * [1, 5, 2, 5, 1][i]
                * (1 / settings.get("roof_resistance", 1.0) - 1)
            )

    result = screening(tmp_path, monkeypatch, rows, RoofEvaluator(rows))
    joint = result["fabric_by_era"]["SE.02"]["joint_independence"]
    assert joint["active"] == [
        "infiltration_multiplier",
        "wall_resistance",
        "roof_resistance",
    ]
    assert result["fabric_by_era"]["SE.02"]["active"] == [
        "wall_resistance",
        "roof_resistance",
    ]
    assert any(
        item["parameter"] == "window_u_multiplier" for item in joint["confounding"]
    )
