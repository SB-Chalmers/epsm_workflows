# EPSM Vera 60k campaign handoff

Last updated: 2026-09-13 (Europe/Stockholm)

## Current outcome

**2026-09-13: all 60,000 simulations completed and the final database passed
the finalizer's acceptance checks.** The successful scientific release is
`3878a30-f0c74381316e`. Pilot `10277055` passed 420/420; production array
`10277476` supplied the other 59,580 accepted runs. No simulation diagnostics
or campaign `PAUSE` were present at completion.

Handoff on Vera:
`/cephyr/NOBACKUP/groups/c3se2026-1-14/ssanjay/epsm-60000/handoff/epsm_surrogate_60000.duckdb`

Size: 7,687,909,376 bytes (7.69 GB / 7.16 GiB).
SHA-256: `c09bb59e71d0619bc06997e4ba20049e1f165165cb20bfa599b38ac3c35d3fa1`.
On 2026-09-13 the database and four companion handoff files were also copied
directly from Vera to csd-lab (`129.16.74.124`), at
`/home/csd-lab/Documents/epsm-60000/handoff/`. Destination `sha256sum -c`
passed. The Vera original was retained unchanged.
Beside it are `README.md`, `epsm_surrogate_60000.sha256`,
`epsm_surrogate_60000.verification.json`, and `epsm_surrogate_60000.cleanup.json`.
The verification receipt records 60,000 unique runs and training rows, 9,200
groups, 87,600 weather rows, zero invalid arrays/scalars/calendars, zero missing
joins, correct group sizes, and a successful read-only reopen with external
access disabled. Energy reconciliation is checked before that receipt is issued.

The finalizer then removed this campaign's shards, receipts, locks, weather
copies, plan database and templates after verification. Older campaigns were
not deleted. The accepted results remain in the final database; the removed
intermediate files are not retained as a separate backup.

Important operational distinction: Slurm finalizer `10277578` reports `FAILED`
after 01:00:17 because its *subsequent email step* called `/usr/sbin/sendmail`,
which is absent on the compute node. The database verification and cleanup
had already succeeded. Login-node Postfix was also inactive at prior checks;
mail acceptance there never proved delivery. The notifier disabled itself on
completion and the user crontab is empty. Its displayed zero progress after
cleanup comes from summing deleted receipts, not missing database rows.

An independent read-only reopen and checksum check after shard removal passed
as compute job `10279369` (`COMPLETED`, exit 0, 58 seconds). It confirmed
60,000 rows each in runs, scalars, hourly_results and training_rows; 87,600
weather rows; a working hourly view with exact heating-plus-DHW derivation;
and a matching SHA-256, with external access disabled and shards absent.
Its result is recorded in
`operations/3878a30-f0c74381316e-handoff-verify-10279369.log` on Vera.
The heartbeat is now paused because the handoff has verified successfully.

### Previous production failure (superseded)

**2026-09-12, approximately 22:53 Stockholm: release
`3878a30-d16dd73c24e6` is paused for a production geometry failure.** Its pilot
`10274806` passed 420/420. Production array `10276712` checkpointed 1,951
accepted cases before all in-flight jobs drained: 2,371 accepted including the
pilot. Case ordinal 58265 in task `production-00233` failed during Core
compilation, before EnergyPlus, with `GEOMETRY_INVARIANT_FAILURE`:
`Near-coincident consecutive surface vertices; minimum edge 0.010001 m`.
The traceback points to window validation in `surface_checks.py:362`, then
`properties()` at line 142. This is distinct from the older node I/O incident.

The campaign's `PAUSE` and compact diagnostic bundle are preserved at
`campaigns/epsm-surrogate-60000-3878a30-d16dd73c24e6/diagnostics/production-00233-58265.tar.gz`
under the dedicated EPSM base on Vera. Remaining production work and dependent
finalization job `10276841` are both `JobHeldUser`; no jobs remained running at
the follow-up check. No retry, source patch, or deletion was performed.
Review the captured building and Core's window geometry validation before
authorizing any resumption. No valid completion ETA exists while paused.

Storage checks were healthy: 9,430.9 GiB project space free, 24,356 project file
entries free, and 27,037 entries under the EPSM base. The production throttle
had risen from 128 to 256 before the failure. The terminal pause email was
accepted by Vera's mail service (attempt 3); the notifier disabled itself and
removed its cron, leaving the user crontab empty. The thread heartbeat is being
paused pending operator input. Older campaigns were not altered.

### Previous release outcome (superseded)

**2026-09-12, approximately 17:56 Stockholm: campaign paused for a node I/O
stall.** The current pilot array `10274200` completed 369/420 cases. The other
51 tasks were all on `vera-r01-20`, blocked for about 36 minutes before any
EnergyPlus execution. A process snapshot through the existing Slurm allocation
showed every Python process in `D` state at `folio_wait_bit_common`, zero CPU
time and no EnergyPlus processes. This suggests a node-associated I/O problem;
the underlying storage fault has not been diagnosed.

`PAUSE` was created. Cancellation was requested for the blocked pilot tasks
and dependent production stage `10274265`; the latter never submitted a
production array. Accepted shards are retained. No retries or source changes
were made. The cancelled pilot tasks were still `COMPLETING` at the first check.
Compact evidence: `operations/incident-3878a30-27c40ae72e56-node-io.json` on Vera.
The follow-up check confirmed that no `ssanjay` jobs remain in the queue. Vera's
mail service accepted the terminal notice; notifier state is disabled with two
total attempted messages and the user crontab is empty. Check node health before
any separately authorized resume.

The current release is `3878a30-27c40ae72e56`, using Core commit
`3878a30b63fc997a0d65cd0325675acef35851b3`. The uploaded `campaigns/vera.sh`
was submitted as Slurm job `10274199`. It runs tests and planning on a compute
node, submits the 420-case pilot at 64 concurrent one-core workers, then uses
Slurm dependencies to schedule measured production batching and finalization.
SSH is only needed for uploading, submitting and inspecting status.

Campaign path:
`/cephyr/NOBACKUP/groups/c3se2026-1-14/ssanjay/epsm-60000/campaigns/epsm-surrogate-60000-3878a30-27c40ae72e56`

The Core fix passed 529 tests on Vera and both archived Ritsem failures.
The preceding `3878a30-f643046258f5` pilot completed 420/420 successfully,
but its production planner was interrupted during nontransactional inserts;
no production simulations were submitted. That campaign is superseded.
Production planning now commits assignments and a completion marker in one
transaction. The regression test injects an interruption and verifies rollback.
The latest local workflow suite passed all 29 tests, including immediate raw
artifact cleanup after a committed result.

Source archive SHA-256:
`08d2d54871e4959711990983ef5590a2fe0c9b1ac5f5f46f32d2b4db7b69edb9`

Input archive SHA-256:
`09be3d887773575ae301762b7f40ce76cbd8a7de303191a11c70daaf5846ea70`

Storage behavior: each accepted simulation commits three hourly arrays to its
task-owned DuckDB on Cephyr, then immediately deletes its node-local raw output.
Three double arrays for 60,000 runs total 11.75 GiB uncompressed. The approximately
30 GiB quota allowance covers shards plus final materialization and overhead,
not an expected final file size. Submission counts six entries per task,
including lock/WAL/temporary files, and enforces 50,000 entries for the entire
EPSM base with 5,000 entries of headroom. The latest base count was 18,176.
Intensive simulation I/O and finalization query spills use `$TMPDIR`; Cephyr
holds persistent inputs, checkpoints and final results.

All stages and arrays request Slurm `END,FAIL` mail to
`sanjay.somanath@chalmers.se`, without `ARRAY_TASKS`. The user subsequently
requested progress mail: one `# epsm-progress-mail` cron checks every 15 minutes,
sends at most hourly when progress changes, and stops on completion, failure,
two hours without progress, a fixed 48-hour expiry or 24 attempted messages.
It persists its disabled state before the final mail and removes its own cron.
Notifier state is `operations/progress-mail-3878a30-27c40ae72e56.json`.
The previous user crontab was empty; no stale email cron entries remained.

## Historical failure and resolution context

The new Vera campaign was prepared, planned, tested, and launched in accordance
with the [VeraEP](https://github.com/SB-Chalmers/VeraEP) operating pattern. Its
420-case pilot paused after two strict EnergyPlus failures. Production was never
submitted.

The failures expose a second, distinct polygon-overlap constraint. Core commit
`f7adba6` successfully raised EnergyPlus's total PolygonClipping workspace from
15,000 to 1,000,000 figures. The new failures instead report:

```text
DeterminePolygonOverlap: Too many vertices [>15] detected in an overlap calculation.
```

Increasing `maximum_figures_in_shadow_overlap_calculations` cannot resolve this
per-overlap vertex limit. The campaign remains paused, the pilot array has been
cancelled after in-flight work drained, and all accepted shards and diagnostics
are preserved.

## Active release and campaign

- Core commit: `f7adba6b3167ee1b22ca3b2037e2948c5fcc9d1c`.
- Release: `f7adba6-501cf61204f7`.
- Release source SHA-256:
  `2eb11f9e566e8b2c87d381c32723dd5d27545bb7d44eff4ac7d54360dca5601d`.
- Release input SHA-256:
  `27c8f8f21bc23b5e95fd2511f52776631f00d1dcbde13cace7c17554884fcf6d`.
- Campaign:
  `/cephyr/NOBACKUP/groups/c3se2026-1-14/ssanjay/epsm-60000/campaigns/epsm-surrogate-60000-f7adba6-501cf61204f7`.
- Campaign identity:
  `8bfe88a635a33736c944a576be87e3bd6c13759c1c6c3048e17f94c67e4410d4`.
- Plan SHA-256:
  `0566c97384d06c301484771c53eff911ca2786cb3788fd746fb86f8af4e25c1a`.
- Pilot array: `10272528`, cancelled after pause and drain.
- Pilot state: 409 accepted, 2 failed, 9 not started, `PAUSE` present.
- Production: not submitted.

The previous campaign and its 203 accepted pilot results remain scientifically
obsolete and are not reused by this campaign.

## Validation completed before launch

- Core tests on Vera compute node, job `10272523`: 528 passed, 2 skipped.
- Corrected workflow release tests on Vera compute node, job `10272525`: 27 passed.
- Plan job `10272526`: completed successfully.
- Fresh plan: 60,000 unique runs, 9,200 groups, 420 pilot runs, and 87,600
  embedded weather rows.
- Sampling checks: bounds, categorical balance, and all 30 LHS strata passed.
- Sites: Malmö, Gothenburg, Uddevalla, Umeå, and Ritsem.
- The release archives were checksum-verified on Vera and the extracted source
  tree was made read-only.

Two earlier release-test jobs failed only because the minimal validation harness
omitted or could not write test fixtures. The harness was corrected without
changing Core science. The successful jobs above are the acceptance evidence.

## Email and scheduler configuration

The frozen campaign configuration contains:

```json
"notification_email": "sanjay.somanath@chalmers.se"
```

Vera's live Slurm record for the pilot confirmed:

```text
MailUser=sanjay.somanath@chalmers.se MailType=END,FAIL
```

`ARRAY_TASKS` is deliberately absent, so notifications apply to the array rather
than producing one email for every case. No status-email cron entry was restored.

The pilot used one EnergyPlus process per allocated core, node-local `$TMPDIR`, a
64-task concurrency cap, and task-owned DuckDB shards on Cephyr. The submission
receipt used the project-specific `C3SE_quota` allocation and Vera's live
`MaxArraySize=5001`.

At submission, the receipt recorded 34,898 free project file entries and about
9.2 TiB free. It projected 1,310 pilot/campaign files and about 30.2 GiB while
retaining the required 5,000-file and 500-GiB headroom.

## New failure evidence

Both failures are the TMY/AMY pair for one anchor group:

| Field | Value |
| --- | --- |
| Group | `anchors_all_complex_00011` |
| Ordinals | 5158 and 5159 |
| Site | Ritsem |
| Context | `urban_highrise_dense` |
| Footprint family | `complex` |
| Weather | TMY and AMY |
| Planning complexity | 60.25 |
| Context radius | 141.012 m |
| Emitted shading faces | 307 |
| Total figure workspace | 1,000,000 |
| Failure code | `SHADOW_COMPLEXITY_LIMIT` |

Diagnostic bundles:

- `diagnostics/pilot-0388-05158.tar.gz`
- `diagnostics/pilot-0389-05159.tar.gz`

Each bundle contains `building.json`, compilation and diagnostic receipts,
`eplusout.err`, `eplusout.end`, `failure.json`, and `process.log`. The identical
failure across TMY and AMY makes weather an unlikely cause. There were no severe
or fatal EnergyPlus messages; the one warning is rejected by the strict campaign
policy.

## Required scientific decision

Do not remove `PAUSE`, retry these cases, suppress the warning, or submit
production without a reviewed Core resolution. Investigation must determine which
input and receiving polygons produce the greater-than-15-vertex overlap and then
choose a deterministic remedy that preserves shading physics. Candidate remedies
must be validated on the two failing cases and nearby passing cases, with annual
loads and runtime compared where possible.

After a reviewed fix:

1. Add a regression test reproducing the `>15`-vertex overlap condition.
2. Freeze a new Core commit and workflow release; do not modify this release.
3. Run Core and workflow tests on a Vera compute node.
4. Create a fresh 60,000-case campaign from seed 42.
5. Rerun all 420 pilot cases at a maximum concurrency of 64.
6. Submit production only after the complete pilot is warning-free and all live
   quota, file-count, scratch, memory, and array-size checks pass.

## Final acceptance target

The intended handoff remains:

`/cephyr/NOBACKUP/groups/c3se2026-1-14/ssanjay/epsm-60000/handoff/epsm_surrogate_60000.duckdb`

It must contain exactly 60,000 complete unique runs, the ten-file weather dataset,
scalar targets, 8,760-value heating/cooling/DHW arrays, and the documented views.
It must reopen read-only with external access disabled after shards are made
unavailable. Publish a README, SHA-256 checksum, and verification receipt beside
the database before removing transient campaign payloads.
