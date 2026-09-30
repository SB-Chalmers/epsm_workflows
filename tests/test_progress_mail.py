import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


source = Path(__file__).parents[1] / "campaigns/progress_mail.py"
if not source.exists():  # Vera copies tests to scratch but imports the release.
    import epsm_workflows
    source = Path(epsm_workflows.__file__).parents[2] / "campaigns/progress_mail.py"
spec = importlib.util.spec_from_file_location("progress_mail", source)
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)


def state():
    return dict(expires_at=200000, attempts=1, progress=[10, "production"],
                emailed_progress=[10, "production"], last_progress_at=10000,
                last_email_at=10000, stall_notified=False)


def test_unchanged_progress_sends_one_recoverable_stall_alert():
    s = state()
    send, reason = mail.decide(s, 11800, 10, "production")
    assert send and "30 minutes" in reason
    assert mail.decide(s, 11900, 10, "production") == (False, "")


def test_progress_clears_stall_suppression():
    s = state()
    s["stall_notified"] = True
    mail.decide(s, 12000, 11, "production")
    assert s["stall_notified"] is False


def test_monitor_ramps_only_after_durable_milestone(monkeypatch, tmp_path):
    root = tmp_path / "campaigns/current"
    (root / "submissions").mkdir(parents=True)
    (root / "submissions/production.json").write_text(json.dumps({
        "job_id": "123", "concurrency": 128
    }))
    calls = []
    monkeypatch.setattr(mail.subprocess, "run", lambda command, **kwargs: (
        calls.append(command) or SimpleNamespace(returncode=0, stderr="")
    ))
    health = dict(running=128, waiting=100, job_id="123", concurrency=128,
                  free_bytes=800 * 1024**3, free_files=12000, base_entries=30000)
    monitor_state = {"last_progress_at": 1000}
    mail._monitor(root, monitor_state, 1100, 420 + 511, "production", health)
    assert not calls
    mail._monitor(root, monitor_state, 1200, 420 + 512, "production", health)
    assert calls[-1][-1] == "ArrayTaskThrottle=256"
    assert json.loads((root / "submissions/production.json").read_text())[
        "current_concurrency"
    ] == 256


def test_monitor_holds_when_file_headroom_is_unsafe(monkeypatch, tmp_path):
    root = tmp_path / "campaigns/current"
    root.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(mail.subprocess, "run", lambda command, **kwargs: (
        calls.append(command) or SimpleNamespace(returncode=0, stderr="")
    ))
    health = dict(running=1, waiting=0, job_id="123", concurrency=128,
                  free_bytes=800 * 1024**3, free_files=6999, base_entries=30000)
    mail._monitor(root, {"last_progress_at": 1000}, 1100, 500, "production", health)
    assert (root / "PAUSE").exists()
    assert calls[-1][:2] == ["scontrol", "hold"]


def test_updates_are_hourly_and_expiry_cannot_slide():
    s = state()
    assert not mail.decide(s, 11000, 20, "production")[0]
    assert mail.decide(s, 13600, 30, "production") == (True, "")
    assert s["expires_at"] == 200000
    assert "expired" in mail.decide(s, 200001, 40, "production")[1]


def test_terminal_conditions_and_disabled_state():
    assert "failure" in mail.decide(state(), 11000, 10, "production", paused=True)[1]
    assert "complete" in mail.decide(state(), 11000, 60000, "finalization", verified=True)[1]
    s = state()
    s["attempts"] = 7
    assert "8-email" in mail.decide(s, 11000, 10, "production")[1]
    s["disabled"] = "finished"
    assert mail.decide(s, 999999, 60000, "finalization") == (False, "")


def test_terminal_email_is_reserved_before_send_and_never_repeated(monkeypatch, tmp_path):
    root = tmp_path / "campaigns/current"
    root.mkdir(parents=True)
    state_path = tmp_path / "state.json"
    mail.save(state_path, dict(campaign=str(root), recipient="test@example.org",
                              started_at=1, expires_at=2, last_progress_at=1,
                              attempts=0, samples=[], stall_notified=False))
    attempts = []
    removed = []
    monkeypatch.setattr(mail, "remove_cron", lambda: removed.append(True))

    def send(*args, **kwargs):
        persisted = json.loads(state_path.read_text())
        assert persisted["disabled"] and persisted["attempts"] == 1
        attempts.append(True)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(mail.subprocess, "run", send)
    mail.tick(state_path)
    mail.tick(state_path)
    assert len(attempts) == 1 and removed
