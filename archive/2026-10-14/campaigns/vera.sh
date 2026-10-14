#!/usr/bin/env bash
# Upload the frozen release, then: sbatch campaigns/vera.sh /path/to/epsm-60000
#SBATCH --account=c3se2026-1-13
#SBATCH --job-name=epsm-campaign
#SBATCH --cpus-per-task=8
#SBATCH --time=00:30:00
#SBATCH --mail-user=sanjay.somanath@chalmers.se
#SBATCH --mail-type=END,FAIL

set -euo pipefail
: "${SLURM_JOB_ID:?Launch this script with sbatch on Vera}"
: "${TMPDIR:?Vera must supply node-local scratch}"
# Slurm copies the script, so retain its release path through an explicit argument.
script=$(readlink -f "${3:-$SLURM_SUBMIT_DIR/campaigns/vera.sh}")
test -f "$script"
workflow=$(dirname "$(dirname "$script")")
tree=$(dirname "$workflow")
base=$(readlink -f "${1:?Pass the dedicated EPSM storage directory}")
stage=${2:-start}
release=$(basename "$(dirname "$tree")")
campaign="$base/campaigns/epsm-surrogate-60000-$release"
notifier="$base/operations/progress-mail-$release.json"

module purge
module load Python/3.12.3-GCCcore-13.3.0
python_env="$base/software/python-env/bin/python"
energyplus="$base/software/EnergyPlus-24.2.0-e7ecb2d53b-Linux-Ubuntu22.04-x86_64/energyplus"
export PYTHONPATH="$workflow/src:$tree/epsm_core/src:$tree/smhi2epw/src"
export EPSM_STORAGE_BASE="$base"
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export TERM=xterm
cd "$TMPDIR"
mkdir -p "$base/operations"
exec 9>"$base/operations/$release-$stage.lock"
flock -n 9

cli() { "$python_env" -m epsm_workflows.cli "$@"; }
advance() {
    local phase=$1 next=$2 cap=$3
    cli submit "$campaign" --phase "$phase" --energyplus "$energyplus" \
        --account c3se2026-1-13 --concurrency "$cap" > "$TMPDIR/submission.json"
    local job
    job=$("$python_env" -c 'import json,sys; print(json.load(open(sys.argv[1]))["job_id"])' \
        "$campaign/submissions/$phase.json")
    sbatch --parsable --dependency="afterok:$job" --kill-on-invalid-dep=yes \
        --job-name="epsm-$next" --time=02:00:00 \
        --output="$base/operations/$release-$next-%j.log" \
        "$script" "$base" "$next" "$script"
    echo "$phase array $job submitted; next stage $next depends on its success"
}
schedule_stage() {
    local next=$1 walltime=$2
    local job
    job=$(sbatch --parsable --dependency="afterok:$SLURM_JOB_ID" \
        --kill-on-invalid-dep=yes --job-name="epsm-$next" --time="$walltime" \
        --output="$base/operations/$release-$next-%j.log" \
        "$script" "$base" "$next" "$script")
    echo "$next stage $job submitted; it depends on this stage succeeding"
}

case "$stage" in
    start)
        test ! -e "$campaign"  # Explicit new campaign; never overwrite an earlier one.
        cp -R "$workflow/tests" "$TMPDIR/tests"
        "$python_env" -m pytest -q "$TMPDIR/tests"
        cli prepare "$workflow/campaigns/vera_60000.json" "$campaign"
        cli plan "$campaign" --energyplus "$energyplus" --workers "$SLURM_CPUS_PER_TASK"
        schedule_stage preflight 04:00:00
        ;;
    preflight)
        cli preflight "$campaign" --energyplus "$energyplus" \
            --workers "$SLURM_CPUS_PER_TASK"
        "$python_env" "$workflow/campaigns/progress_mail.py" "$notifier" --install "$campaign"
        advance pilot production 64
        ;;
    production)
        test ! -e "$campaign/PAUSE"
        advance production finalize 128
        ;;
    finalize)
        test ! -e "$campaign/PAUSE"
        cli finalize "$campaign" "$base/handoff/epsm_surrogate_60000.duckdb"
        "$python_env" "$workflow/campaigns/progress_mail.py" "$notifier"
        ;;
    *) echo "Unknown campaign stage: $stage" >&2; exit 2 ;;
esac
