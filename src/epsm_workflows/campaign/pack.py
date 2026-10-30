# SPDX-License-Identifier: MIT
"""Build a self-contained Vera package from a study file, then zip it."""
from __future__ import annotations

import json
import shutil
import time
from concurrent.futures import ProcessPoolExecutor
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path

from ..records import atomic_json, digest
from .geometry import load_footprints
from .store import create_plan, validate_stock_sampling
from .study import load_study
from .weather import catalog_digest, file_sha256, freeze_weather

PACKAGE_FORMAT = "epsm-campaign-package/1"


def authored_with():
    out = {}
    for name in ("epsm_workflows", "epsm_core", "epsm_data", "epsm", "epsm_db", "scipy", "numpy", "duckdb", "shapely"):
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


def _expand(payload):
    from . import cases

    study, design, footprints, catalog = payload
    polygon = cases.design_polygon(design, footprints)
    grouped = cases.weather_by_site(study, catalog)
    return list(cases.physical_cases(study, design, polygon, grouped, cohort_axes={"study_kind": study.kind}))


def study_cases(study, footprints, catalog, *, workers=1):
    """Every case of the study in plan order, expanded in parallel per design."""
    from . import cases

    if study.kind != "stock_diversity":
        yield from cases.cases_for(study, footprints, catalog)
        return
    from .sampling import stock_designs

    payloads = ((study, design, footprints, catalog) for design in stock_designs(study, footprints))
    if workers == 1:
        for payload in payloads:
            yield from _expand(payload)
        return
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for batch in pool.map(_expand, payloads, chunksize=4):
            yield from batch


def readme_text(study, receipt, manifest):
    package = f"{study.name}-{study.id[:12]}"
    return f"""# EPSM campaign package: {study.name}

Study `{study.kind}`, identity `{study.id}`. {receipt['runs']} runs in {receipt['groups']} groups;
{receipt['pilot_runs']} pilot runs; {len(receipt['weather_ids'])} frozen weather files.
Authored with {json.dumps({k: v for k, v in manifest['authored_with'].items() if v})}.

Everything Vera needs is in this folder: `plan.duckdb` (every case as a portable
Core Building plus its weather id), `weather/`, `study.json`, `vera.sh`. Nothing
here depends on the authoring machine; Vera records its own engine identities.

## One-time environment on Vera

```sh
module load Python/3.11.3-GCCcore-12.3.0   # epsm_rc wheels are cp311 Linux x86_64
python -m venv $EPSM_BASE/software/python-env
source $EPSM_BASE/software/python-env/bin/activate
gh release download {manifest['release_tag']} --repo SB-Chalmers/epsm --dir /tmp/epsm-wheels
pip install /tmp/epsm-wheels/epsm_core-*.whl /tmp/epsm-wheels/epsm_data-*.whl \\
            /tmp/epsm-wheels/epsm_workflows-*.whl /tmp/epsm-wheels/epsm_rc-*.whl "duckdb>=1.5,<2"
```

`epsm_rc` is optional: without it the RC columns stay NULL. EnergyPlus 24.2.0 must be
installed under `$EPSM_BASE/software`; `vera.sh` reads `EPSM_ENERGYPLUS`.

## Run

```sh
unzip {package}.zip -d $EPSM_BASE/campaigns/
cd $EPSM_BASE/campaigns/{package}
export EPSM_STORAGE_BASE=$EPSM_BASE EPSM_PYTHON=$EPSM_BASE/software/python-env/bin/python \\
       EPSM_ENERGYPLUS=$EPSM_BASE/software/EnergyPlus-24.2.0-e7ecb2d53b-Linux-Ubuntu22.04-x86_64/energyplus \\
       EPSM_ACCOUNT=<slurm-account>
sbatch vera.sh "$PWD"
```

`vera.sh` chains preflight (compile every case) -> pilot ({receipt['pilot_runs']} runs, 64 concurrent)
-> production (batched from pilot p95 runtime) -> finalize, through Slurm dependencies.
A strict EnergyPlus failure writes `PAUSE`, saves `diagnostics/*.tar.gz` and holds the array.

```sh
$EPSM_PYTHON -m epsm_workflows.campaign status .        # progress
$EPSM_PYTHON -m epsm_workflows.campaign pause .         # hold running arrays
sbatch vera.sh "$PWD" production                        # resume a stage after review
```

## Handoff

`finalize` writes `handoff/{study.name}.duckdb` with `README.md`, `.sha256`,
`.verification.json` and `.cleanup.json`, then removes shards, receipts, locks,
weather copies and the plan database. Copy the `handoff/` folder off the cluster.
Tables: `runs` (inputs and axes), `scalars`, `hourly_results` (EnergyPlus heating,
cooling, DHW and RC heating/cooling arrays), `weather`, `task_runtimes`; views
`training_rows` and `hourly`.
"""


def pack(study_path, destination, *, workers=1, release_tag="v0.5.1", archive=True):
    """Freeze a study into ``<destination>/<name>-<id12>/`` and zip it."""
    study = load_study(study_path)
    root = Path(destination).resolve() / f"{study.name}-{study.id[:12]}"
    if root.exists():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    atomic_json(root / "study.json", study.to_dict())
    catalog = freeze_weather(study, root)
    footprints = []
    if study.geometry.real_footprints:
        shutil.copyfile(study.geometry.real_footprints, root / "footprints.json")
        footprints = load_footprints(root / "footprints.json")
    manifest = {
        "format": PACKAGE_FORMAT,
        "study_id": study.id,
        "study_name": study.name,
        "kind": study.kind,
        "weather_digest": catalog_digest(catalog),
        "footprints_sha256": file_sha256(root / "footprints.json") if footprints else None,
        "authored_with": authored_with(),
        "release_tag": release_tag,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    receipt = create_plan(root, study, study_cases(study, footprints, catalog, workers=workers), catalog, manifest)
    if study.kind == "stock_diversity":
        receipt["sampling_validation"] = validate_stock_sampling(root)
        atomic_json(root / "plan.json", receipt)
    shutil.copyfile(files("epsm_workflows.campaign").joinpath("data/vera.sh"), root / "vera.sh")
    (root / "vera.sh").chmod(0o755)
    (root / "README.md").write_text(readme_text(study, receipt, manifest))
    atomic_json(root / "manifest.json", {**manifest, "plan": receipt, "manifest_digest": digest(manifest)})
    result = {"root": str(root), "study_id": study.id, **receipt}
    if archive:
        result["archive"] = shutil.make_archive(str(root), "zip", root.parent, root.name)
        result["archive_sha256"] = file_sha256(result["archive"])
    return result
