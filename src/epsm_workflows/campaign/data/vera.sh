#!/usr/bin/env bash
# Run a packed EPSM campaign on Vera: sbatch vera.sh /path/to/package [stage]
# Stages chain through Slurm dependencies: preflight -> pilot -> production -> finalize.
#SBATCH --job-name=epsm-campaign
#SBATCH --cpus-per-task=8
#SBATCH --time=04:00:00
#SBATCH --mail-type=END,FAIL

set -euo pipefail
: "${SLURM_JOB_ID:?Launch this script with sbatch on Vera}"
: "${TMPDIR:?Vera must supply node-local scratch}"
: "${EPSM_PYTHON:?Path to the campaign venv python}"
: "${EPSM_ENERGYPLUS:?Path to the EnergyPlus 24.2 executable}"
: "${EPSM_ACCOUNT:?Slurm account for the simulation arrays}"
root=$(readlink -f "${1:?Pass the unpacked package directory}")
stage=${2:-preflight}
script=$(readlink -f "${3:-$root/vera.sh}")
export EPSM_STORAGE_BASE="${EPSM_STORAGE_BASE:-$(dirname "$(dirname "$root")")}"
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export TERM=xterm
cd "$TMPDIR"
mkdir -p "$root/logs" "$root/locks"
exec 9>"$root/locks/stage-$stage.lock"
flock -n 9

cli() { "$EPSM_PYTHON" -m epsm_workflows.campaign "$@"; }
schedule_stage() {
    local next=$1 walltime=$2 dependency=$3
    local job
    job=$(sbatch --parsable --account="$EPSM_ACCOUNT" --dependency="$dependency" \
        --kill-on-invalid-dep=yes --job-name="epsm-$next" --time="$walltime" \
        --output="$root/logs/stage-$next-%j.log" --export=ALL \
        "$script" "$root" "$next" "$script")
    echo "$next stage $job scheduled after $dependency"
}
advance() {
    local phase=$1 next=$2
    cli submit "$root" --phase "$phase" --energyplus "$EPSM_ENERGYPLUS" \
        --account "$EPSM_ACCOUNT" --python "$EPSM_PYTHON" > "$TMPDIR/submission.json"
    local job
    job=$("$EPSM_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["job_id"])' \
        "$root/submissions/$phase.json")
    schedule_stage "$next" 02:00:00 "afterok:$job"
}

case "$stage" in
    preflight)
        cli preflight "$root" --energyplus "$EPSM_ENERGYPLUS" --workers "$SLURM_CPUS_PER_TASK"
        advance pilot production
        ;;
    production)
        test ! -e "$root/PAUSE"
        advance production finalize
        ;;
    finalize)
        test ! -e "$root/PAUSE"
        cli finalize "$root"
        ;;
    *) echo "Unknown campaign stage: $stage" >&2; exit 2 ;;
esac
