"""The existing CLI delegates to the same typed workflow interface."""

import json

import pytest

from epsm_workflows.execution import main


def test_template_and_schema_commands(capsys):
    assert main(["template", "--method", "monte_carlo", "--samples", "8"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["sampling"] == {
        "method": "monte_carlo",
        "samples": 8,
        "seed": 0,
        "correlation": None,
    }
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert (
        "discriminator"
        in schema["$defs"]["ParameterSpec"]["properties"]["distribution"]["anyOf"][0]
    )
    assert "facade.wwr" in schema["x-epsm-inputs"]


def test_preview_incomplete_draft_reports_requirements(tmp_path, capsys):
    main(["template"])
    path = tmp_path / "draft.json"
    path.write_text(capsys.readouterr().out)
    assert main(["preview", str(path)]) == 1
    value = json.loads(capsys.readouterr().out)
    assert {x["code"] for x in value["diagnostics"]} >= {
        "missing_subjects",
        "missing_weather",
    }
    with pytest.raises(SystemExit):
        main(["prepare", str(path)])
