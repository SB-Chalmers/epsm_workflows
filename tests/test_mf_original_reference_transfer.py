"""History corrections cannot invalidate or weaken native physical cache guards."""

import pytest

from epsm_workflows import mf_parent_dynamic as study
from epsm_workflows.campaign import file_sha256


@pytest.mark.parametrize(
    "name", ["mf_parent_dynamic.py", "mf_stock_history.py", "mf_parent_subdivision.py"]
)
def test_parent_transfer_records_only_selection_history_version_exceptions(
    tmp_path, monkeypatch, name
):
    source = tmp_path / "epsm_workflows" / "src" / "epsm_workflows"
    source.mkdir(parents=True)
    monkeypatch.setattr(study, "__file__", str(source / "mf_parent_dynamic.py"))
    path = source / name
    path.write_text("previous history version")
    previous = file_sha256(path)
    path.write_text("corrected historical construction source")
    result = study.parent_transfer_runtime_checks({str(path): previous})
    assert result[str(path)]["previous_sha256"] == previous
    assert result[str(path)]["current_sha256"] == file_sha256(path)
    assert "no dynamic-bank transfer" in result[str(path)]["basis"]


@pytest.mark.parametrize(
    "relative",
    [
        "epsm_workflows/src/epsm_workflows/mf_physics.py",
        "epsm_workflows/src/epsm_workflows/cohort_transfer/simulation.py",
        "epsm_workflows/src/epsm_workflows/cohort_transfer/parameters.py",
        "epsm_core/src/epsm_core/runner.py",
        "epsm_data/src/epsm_data/application.py",
    ],
)
def test_parent_transfer_rejects_changed_native_execution_modules(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("previous physical version")
    previous = file_sha256(path)
    path.write_text("different physical execution")
    with pytest.raises(ValueError, match="physical/diagnostic implementation changed"):
        study.parent_transfer_runtime_checks({str(path): previous})


def test_unchanged_native_execution_has_no_version_exception(tmp_path):
    path = tmp_path / "epsm_core" / "src" / "epsm_core" / "runner.py"
    path.parent.mkdir(parents=True)
    path.write_text("identical execution")
    assert study.parent_transfer_runtime_checks({str(path): file_sha256(path)}) == {}


def test_runtime_metadata_is_not_treated_as_a_source_file():
    assert (
        study.parent_transfer_runtime_checks({"python": "3.13", "platform": "darwin"})
        == {}
    )


def test_current_receipt_transfer_keeps_pinned_ancestor_and_excludes_history(tmp_path):
    prior = tmp_path / "newer"
    local = prior / "current-native/simulations/local/receipt.json"
    parent = prior / "simulations/parent/receipt.json"
    ancestor = tmp_path / "older/current-native/simulations/inherited/receipt.json"
    dynamic = tmp_path / "older/dynamic-native/simulations/state/receipt.json"
    from epsm_workflows.campaign import atomic_json

    for path in (local, parent, ancestor, dynamic):
        atomic_json(path, {"status": "accepted"})
    manifest = {
        "sources": {str(path): file_sha256(path) for path in (ancestor, dynamic)}
    }
    assert dict(study.parent_transfer_receipts(prior, manifest)) == {
        local: True,
        parent: False,
        ancestor: True,
    }
    ancestor.write_text("changed")
    with pytest.raises(ValueError, match="ancestral current native receipt changed"):
        study.parent_transfer_receipts(prior, manifest)
