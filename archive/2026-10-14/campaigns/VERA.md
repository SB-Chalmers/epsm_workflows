# Vera 60k: distilled design and operations

This workflow follows the operating pattern documented by
[VeraEP](https://github.com/SB-Chalmers/VeraEP): use the login node only for
lightweight preparation and submission, run EnergyPlus through Slurm, and keep
simulation I/O in node-local `$TMPDIR`.

Upload a frozen release and its input bundle, then from its `tree/epsm_workflows`
directory run `sbatch campaigns/vera.sh /path/to/epsm-60000`. This one script runs
tests and planning on a compute node, schedules exhaustive compile-only preflight,
submits the pilot after preflight passes, schedules production after the pilot
succeeds, and schedules finalization after production succeeds. Slurm dependencies
carry the campaign onward after SSH disconnects. Each stage has a persistent log and
END/FAIL email; the CLIs enforce preflight, complete-pilot, and quota gates. There is
no polling controller occupying a node.

The abandoned releases established four useful facts: EnergyPlus 24.2 should run
one process per allocated core; simulations belong in node-local `$TMPDIR`; Cephyr
must receive a durable checkpoint after every accepted case; and Core's strict
diagnostics must stop the campaign instead of being patched around. Uppsala TMY
triggered an engine psychrometric warning and is replaced by Uddevalla.

The scientific design remains seed 42: 9,000 diversity designs replicated over
three contexts and paired TMY/AMY weather (54,000 runs), plus 200 cross-site anchors
replicated over five sites, three contexts, and paired weather (6,000 runs). Sites
are Malmö, Gothenburg, Uddevalla, Umeå, and Ritsem. Uddevalla uses the nearby
Ljungskile 2011–2025 ERA5 TMY site (58.217° N, 11.933° E, 70 m). Its 2018 AMY uses
the nearest station with the complete required parameter set, SMHI Såtenäs 82260,
while retaining the TMY coordinates/elevation and the reviewed 48-hour gap and
pressure-conversion policies.

There is one operator CLI and one data path. `prepare` freezes configuration,
templates, and weather. `plan` writes all cases and balanced assignments. `preflight`
compiles every case without running EnergyPlus and checkpoints its results.
`submit` launches only missing pilot or production tasks. `worker` is the internal
Slurm entrypoint. `status`, `pause`, and `finalize` are the remaining controls. Every
task owns one DuckDB shard; another writer is rejected by a lock. Raw EnergyPlus
output is discarded after the shard transaction commits. A simulation failure creates
`PAUSE`, saves a small archive, and holds the array while already-running cases finish.

The normal dependency chain is `start → preflight → pilot → production → finalize`.
If the preflight compute job is interrupted by infrastructure or walltime, resubmit
only that stage from the frozen release tree:

```sh
sbatch campaigns/vera.sh /path/to/epsm-60000 preflight
```

The command verifies campaign, Core compiler, EnergyPlus executable and schema hashes
before reusing committed rows in `preflight.duckdb`. Case-level compilation failures
are collected exhaustively and make the stage fail without creating `PAUSE`; inspect
them with the query documented in the repository README. Pilot submission remains
impossible until all planned ordinals pass.

The pilot is 420 one-case jobs capped at 64. A complete pilot determines the
production batch size from p95 runtime: target 45 minutes, clamp to 8–128 cases,
and request twice the estimate plus ten minutes. Longest-processing-time balancing
uses geometry/context complexity. Production concurrency is at most 512 and must
be reduced for live scheduler, memory, scratch, or quota constraints. Submission
requires at least 500 GiB and 5,000 filesystem entries of headroom. Before every
submission, the CLI reads `MaxArraySize` from `scontrol`, chooses a production
batch size that fits the usable array capacity, and parses `C3SE_quota` for the
allocation containing the campaign path. The projection counts six entries per
task (shard, lock, receipt, Slurm log, WAL and temporary receipt) plus fixed
campaign overhead. The launcher also enforces a 50,000-entry budget across the
dedicated EPSM base, including software and previous campaigns, with 5,000 entries
kept free. A
missing or unparseable C3SE quota report blocks submission.

Only three 8,760-element double arrays are stored per accepted run: heating,
cooling and DHW. For 60,000 runs these are 11.75 GiB before compression. The
approximately 30 GiB quota projection is a peak allowance for both shards and the
final database, specifications and overhead; it is not the expected final file
size. The 420-case pilot occupied 530 MiB in separate one-case databases, where
database headers and indexes dominate, so linear extrapolation overestimates
the combined database. Cephyr stores durable results/checkpoints. Simulation
artifacts and finalization query spills use node-local scratch. Every accepted
simulation commits its result and deletes its raw artifacts immediately.

Each pilot and production array uses Slurm's campaign-level `END,FAIL` mail
notifications sent to the frozen `notification_email`. `ARRAY_TASKS` is
deliberately omitted to avoid one email per array element. A separate, finite
progress notifier in `operations/progress_mail.py` checks receipts every 15
minutes and sends at most hourly when progress changes. Its state is pinned to
the current campaign. It sends an initial message and one final notice, then
disables itself and removes its cron on completion, PAUSE, two hours without
progress, 48 hours from installation, or 24 total attempted messages. Every cron
invocation has a 90-second timeout; mail delivery has a 20-second timeout.
Attempts are recorded before sending so a stalled delivery is not retried.
An empty prior crontab was verified before installation. Slurm END/FAIL messages
remain separate and are tied to finite campaign jobs.

Development preparation verifies the Core Git HEAD and source cleanliness. The
minimal Vera release deliberately excludes Git history; there, the checksum-frozen
campaign configuration supplies the exact Core commit and the manifest records
hashes for every Core Python source file.

Remote cleanup is limited to
`/cephyr/NOBACKUP/groups/c3se2026-1-14/ssanjay/epsm-60000`. First inventory jobs,
cron, quota, and every first-level entry; cancel only `ssanjay`'s old `epsm-*` jobs;
remove only the stale EPSM status-email cron line; write a cleanup receipt; then
delete old campaign payloads while preserving reusable `software/`. Never touch
`~/eplus_project`, unrelated home data, other project users, or shared software.

The verified handoff is `handoff/epsm_surrogate_60000.duckdb` plus README, SHA-256,
and verification receipt. It contains manifest, weather, runs, scalars, and one-row-
per-run hourly arrays, with `training_rows` and lazy `hourly` views. Only after a
read-only reopen with external access disabled may task shards and transient payloads
be removed.
