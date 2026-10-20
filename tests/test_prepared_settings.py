# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EPSM Development contributors
# License: See LICENSE at the project root.
"""Prepared cases restore public engine settings after JSON serialization."""
# Optional integrations must be skipped before importing their public objects.
# ruff: noqa: E402

from types import SimpleNamespace

import pytest

epsm = pytest.importorskip("epsm")
from epsm_core import CompilerSettings, HourlyOutputRequest

from epsm_workflows import execution, integration
from epsm_workflows.records import json_value


def output_settings():
    return CompilerSettings(
        hourly_outputs=(HourlyOutputRequest(name="Zone Mean Air Temperature"),)
    )


def test_evaluation_restores_nonempty_energyplus_settings(monkeypatch):
    captured = {}

    def simulate(self, **options):
        captured.update(options)
        return {"result": "captured"}

    monkeypatch.setattr(epsm.Building, "simulate", simulate)
    settings = output_settings()
    payload = {
        "building": epsm.Building(preset="illustrative-reference").to_dict(),
        "simulation": {
            "engine": "energyplus",
            "weather": "unused.epw",
            "settings": json_value(settings),
        },
    }
    assert integration.evaluate(payload) == {"result": "captured"}
    assert isinstance(captured["settings"], CompilerSettings)
    assert captured["settings"] == settings
    assert isinstance(payload["simulation"]["settings"], dict)


def test_preparation_restores_nonempty_energyplus_settings(tmp_path, monkeypatch):
    import epsm_core.compiler

    observed = []

    def compiler(path, *, settings):
        observed.append(settings)
        assert isinstance(settings, CompilerSettings)
        return SimpleNamespace(
            engine_hash="a" * 64, schema_hash="b" * 64, version="24.2"
        )

    monkeypatch.setattr(epsm_core.compiler, "EnergyPlusCompiler", compiler)
    case = execution.PreparedEvaluation(
        {
            "simulation": {
                "engine": "energyplus",
                "settings": json_value(output_settings()),
            }
        }
    )
    execution.prepare(tmp_path / "plan", [case])
    assert observed == [output_settings()]


def test_invalid_frozen_settings_fail_before_plan_publication(tmp_path, monkeypatch):
    import epsm_core.compiler

    monkeypatch.setattr(
        epsm_core.compiler,
        "EnergyPlusCompiler",
        lambda *a, **kw: SimpleNamespace(
            engine_hash="a" * 64,
            schema_hash="b" * 64,
            version="24.2",
        ),
    )
    case = execution.PreparedEvaluation(
        {
            "simulation": {
                "engine": "energyplus",
                "settings": {"not_a_compiler_setting": True},
            }
        }
    )
    with pytest.raises(ValueError):
        execution.prepare(tmp_path / "plan", [case])
    assert not (tmp_path / "plan/manifest.json").exists()
