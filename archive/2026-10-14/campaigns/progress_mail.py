"""Finite, campaign-specific progress mail; independent of simulation releases."""

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import time
from email.message import EmailMessage
from pathlib import Path

MARKER = "# epsm-progress-mail"


def save(path, value):
    pending = path.with_suffix(".pending")
    with pending.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(pending), str(path))


def cron_read():
    result = subprocess.run(["crontab", "-l"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
    if result.returncode and "no crontab" not in result.stderr.lower():
        raise RuntimeError(result.stderr)
    return result.stdout


def cron_write(contents):
    subprocess.run(["crontab", "-"], input=contents, universal_newlines=True,
                   check=True, timeout=10)


def remove_cron():
    current = cron_read()
    updated = "".join(line for line in current.splitlines(True) if MARKER not in line)
    if current != updated:
        cron_write(updated)


def snapshot(root):
    complete = sum(json.loads(path.read_text())["complete"]
                   for path in (root / "receipts").glob("*.json"))
    if (root / "submissions/production.json").exists():
        phase = "production" if complete < 60000 else "finalization"
    elif (root / "submissions/pilot.json").exists():
        phase = "pilot"
    else:
        phase = "planning"
    return complete, phase


def decide(state, now, complete, phase, paused=False, verified=False):
    """Return (send, reason); keep a single recoverable stall notification."""
    if state.get("disabled"):
        return False, ""
    progress = [complete, phase]
    changed = progress != state.get("progress")
    phase_changed = phase != state.get("progress", [None, None])[1]
    if changed:
        state["progress"] = progress
        state["last_progress_at"] = now
        state["stall_notified"] = False
    terminal = ""
    if paused:
        terminal = "campaign paused after a failure"
    elif verified:
        terminal = "final database verified; campaign complete"
    elif now >= state["expires_at"]:
        terminal = "12-hour notification window expired"
    elif state["attempts"] >= 7:
        terminal = "8-email notification limit reached"
    if terminal:
        return True, terminal
    if now - state["last_progress_at"] >= 30 * 60 and not state.get("stall_notified"):
        state["stall_notified"] = True
        return True, "no durable progress for 30 minutes; campaign needs attention"
    due = not state.get("last_email_at") or phase_changed or (
        changed and now - state.get("last_email_at", 0) >= 3600
    )
    return bool(due), ""


def _health(root):
    health = {"storage": "unavailable", "files": "unavailable", "base_entries": "unavailable"}
    quota = shutil.which("C3SE_quota")
    if quota and str(root).startswith("/cephyr/"):
        env = os.environ.copy()
        env["TERM"] = "xterm"
        result = subprocess.run([quota], capture_output=True, text=True, timeout=20, env=env)
        for section in re.split(
            r"(?=^Path:)", getattr(result, "stdout", ""), flags=re.MULTILINE
        ):
            path = re.search(r"^Path:\s*(\S+)", section, re.MULTILINE)
            space = re.search(r"Space used:\s*([0-9.]+\w+iB)\s+Quota:\s*([0-9.]+\w+iB)", section)
            files = re.search(r"Files used:\s*(\d+)\s+Quota:\s*(\d+)", section)
            if path and space and files:
                try:
                    root.relative_to(Path(path.group(1)))
                except ValueError:
                    continue
                health["storage"] = f"{space.group(1)} of {space.group(2)} used"
                health["files"] = f"{int(files.group(1)):,} of {int(files.group(2)):,} used"
                units = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
                amount = re.fullmatch(r"([0-9.]+)(\w+iB)", space.group(1))
                limit = re.fullmatch(r"([0-9.]+)(\w+iB)", space.group(2))
                health["free_bytes"] = (
                    float(limit.group(1)) * units[limit.group(2)]
                    - float(amount.group(1)) * units[amount.group(2)]
                )
                health["free_files"] = int(files.group(2)) - int(files.group(1))
                break
    base = root.parent.parent
    health["base_entries"] = sum(
        len(directories) + len(files) for _, directories, files in os.walk(base)
    )
    running = waiting = 0
    for phase in ("production", "pilot"):
        record = root / "submissions" / f"{phase}.json"
        if not record.exists() or not shutil.which("squeue"):
            continue
        job = json.loads(record.read_text()).get("job_id")
        result = subprocess.run(
            ["squeue", "-h", "-r", "-j", str(job), "-o", "%T"],
            capture_output=True, text=True, timeout=20
        )
        states = result.stdout.split()
        running += sum(value in {"RUNNING", "COMPLETING"} for value in states)
        waiting += sum(value in {"PENDING", "CONFIGURING"} for value in states)
        submission = json.loads(record.read_text())
        health["concurrency"] = submission.get(
            "current_concurrency", submission.get("concurrency", "unknown")
        )
        health["job_id"] = job
        break
    health.update(running=running, waiting=waiting)
    return health


def _monitor(root, state, now, complete, phase, health):
    """Apply conservative throttle ramps and hard operational holds."""
    active = health.get("running", 0) + health.get("waiting", 0) > 0
    stalled = active and now - state["last_progress_at"] >= 30 * 60
    unsafe = []
    if stalled:
        unsafe.append("no durable progress for 30 minutes")
    if health.get("free_bytes", float("inf")) < 500 * 1024**3:
        unsafe.append("less than 500 GiB project storage free")
    if health.get("free_files", float("inf")) < 7000:
        unsafe.append("fewer than 7,000 project files free")
    if health["base_entries"] > 45000:
        unsafe.append("more than 45,000 EPSM area entries")
    if unsafe and active and not (root / "PAUSE").exists():
        save(root / "PAUSE", {"reason": "; ".join(unsafe), "time": now, "source": "progress monitor"})
        subprocess.run(["scontrol", "hold", str(health["job_id"])], timeout=20)
        return
    healthy = (
        active
        and not (root / "PAUSE").exists()
        and not any((root / "diagnostics").glob("*.tar.gz"))
        and not stalled
        and health.get("free_bytes", 0) > 750 * 1024**3
        and health.get("free_files", 0) > 10000
        and health["base_entries"] < 40000
    )
    if phase != "production" or not healthy:
        return
    production_done = max(0, complete - 420)
    current = int(health.get("concurrency", 128))
    target = None
    if current == 128 and production_done >= 512:
        target = 256
    elif current == 256 and production_done >= 5000 and state.get("ramped_to_256_at", now) < now:
        target = 512
    if not target:
        return
    result = subprocess.run(
        ["scontrol", "update", f"JobId={health['job_id']}", f"ArrayTaskThrottle={target}"],
        capture_output=True, text=True, timeout=20
    )
    if result.returncode:
        state["ramp_error"] = result.stderr[:300]
        return
    record = root / "submissions/production.json"
    submission = json.loads(record.read_text())
    submission["current_concurrency"] = target
    submission["last_ramped_at"] = now
    save(record, submission)
    state[f"ramped_to_{target}_at"] = now
    health["concurrency"] = target


def _rate_eta(state, now, complete):
    samples = state.setdefault("samples", [])
    if not samples or complete != samples[-1][1]:
        samples.append([now, complete])
    samples[:] = [sample for sample in samples if sample[0] >= now - 3600][-12:]
    if len(samples) < 2 or samples[-1][0] == samples[0][0]:
        return None, "not enough recent progress yet"
    rate = (samples[-1][1] - samples[0][1]) * 3600 / (samples[-1][0] - samples[0][0])
    if rate <= 0:
        return None, "waiting for durable progress"
    eta_seconds = max(0, 60000 - complete) * 3600 / rate
    eta_clock = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now + eta_seconds))
    return rate, eta_clock


def tick(state_path):
    state = json.loads(state_path.read_text())
    if state.get("disabled"):
        remove_cron()
        return
    now = time.time()
    root = Path(state["campaign"])
    rate, eta = None, "not enough recent progress yet"
    try:
        complete, phase = snapshot(root)
        rate, eta = _rate_eta(state, now, complete)
        health = _health(root)
        _monitor(root, state, now, complete, phase, health)
        state.pop("read_error", None)
    except (OSError, ValueError, KeyError) as exc:
        complete, phase = state.get("progress", [0, "planning"])
        state["read_error"] = str(exc)[:300]
    verification = root.parent.parent / "handoff/epsm_surrogate_60000.verification.json"
    verified = False
    if verification.exists() and verification.stat().st_mtime >= state["started_at"]:
        verified = json.loads(verification.read_text()).get("reopened_read_only") == 60000
    send, terminal = decide(state, now, complete, phase,
                            (root / "PAUSE").exists(), verified)
    if not send:
        save(state_path, state)
        return
    # Reserve the attempt before transport: a failed or stalled send is not retried.
    state["attempts"] += 1
    state["last_email_at"] = now
    state["emailed_progress"] = [complete, phase]
    is_terminal = terminal in {
        "campaign paused after a failure",
        "final database verified; campaign complete",
        "12-hour notification window expired",
        "8-email notification limit reached",
    }
    if is_terminal:
        state["disabled"] = terminal
    save(state_path, state)
    if is_terminal:
        try:
            remove_cron()
        except Exception as exc:
            state["cron_cleanup_error"] = str(exc)[:300]
            save(state_path, state)
    message = EmailMessage()
    message["To"] = state["recipient"]
    message["From"] = "EPSM on Vera <ssanjay@vera1.c3se.chalmers.se>"
    health = locals().get("health") or _health(root)
    message["Subject"] = "EPSM 60k: {} ({:,}/60,000)".format(terminal or phase, complete)
    rate_text = "measuring" if rate is None else f"{rate:,.0f} accepted runs/hour"
    message.set_content(
        "EPSM 60k update\n\nDone: {:,}/60,000 ({:.1f}%)\n"
        "Current step: {}\nAverage recent throughput: {}\nEstimated finish: {}\n"
        "Vera jobs: {} running, {} waiting (limit {})\n"
        "Project storage: {}\nProject files: {}\nEPSM area entries: {:,}/50,000\n\n{}\n\n"
        "Updates are at most hourly while progress changes, plus one alert if durable "
        "progress stops for 30 minutes. Maximum 8 messages over 12 hours.\n"
        "This notifier does not cancel simulations. Slurm END/FAIL mail is separate.\n"
        .format(complete, complete / 600, phase, rate_text, eta,
                health["running"], health["waiting"], health.get("concurrency", "unknown"),
                health["storage"], health["files"], health["base_entries"],
                terminal or "The campaign is progressing normally.")
    )
    result = subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"],
                            input=message.as_bytes(), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=20)
    state["transport_returncode"] = result.returncode
    state["transport_response"] = result.stderr.decode(errors="replace")[:300]
    save(state_path, state)
    if result.returncode:
        raise RuntimeError("sendmail rejected this notification")


def install(state_path, root):
    import shlex
    if state_path.exists():
        raise ValueError("Existing notifier state cannot be reset implicitly")
    config = json.loads((root / "config.json").read_text())
    now = time.time()
    save(state_path, {"campaign": str(root), "recipient": config["notification_email"],
                      "started_at": now, "expires_at": now + 12 * 3600,
                      "last_progress_at": now, "attempts": 0, "samples": [],
                      "stall_notified": False})
    current = cron_read()
    save(state_path.with_suffix(".cron-backup.json"), {"previous_crontab": current})
    retained = [line for line in current.splitlines() if MARKER not in line]
    command = "/usr/bin/timeout 90s /usr/bin/python3 {} {}".format(
        shlex.quote(str(Path(__file__).resolve())), shlex.quote(str(state_path)))
    retained.append("*/15 * * * * " + command + " >/dev/null 2>&1 " + MARKER)
    cron_write("\n".join(retained) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--install", type=Path)
    args = parser.parse_args()
    # Bound filesystem scans too, in case a network filesystem stops responding.
    signal.alarm(80)
    with args.state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.install:
            install(args.state, args.install.resolve())
            # Installation runs inside the orchestration compute job, where Vera's
            # login-node mail transport is intentionally unavailable. Cron sends
            # the initial update from the login node on its next scheduled tick.
            return
        tick(args.state)


if __name__ == "__main__":
    main()
