"""A compact CLI report identifies the exact input document it describes."""

import hashlib
import json

from epsm_workflows import WorkflowSpec
from epsm_workflows.authoring_cli import main


def test_compact_cli_report_retains_source_bytes_digest(tmp_path, capsys):
    path = tmp_path / "workflow.json"
    raw = (json.dumps(WorkflowSpec.template().to_dict(), indent=2) + "\n").encode()
    path.write_bytes(raw)
    assert main(["describe", str(path), "--structural"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["format"] == "epsm-design-preview/1"
    assert report["readiness_checked"] is False


def test_existing_preview_output_keeps_its_original_contract(tmp_path, capsys):
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(WorkflowSpec.template().to_dict()))
    assert main(["preview", str(path), "--structural"]) == 1
    assert "source_sha256" not in json.loads(capsys.readouterr().out)
