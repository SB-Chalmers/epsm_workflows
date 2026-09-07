"""Save source reconciliation and full certificate history for the selected buildings."""

import argparse
import json
from pathlib import Path

import duckdb
from epsm_workflows.validation import number

from epsm_core.weather import file_sha256

p = argparse.ArgumentParser()
p.add_argument("--study", type=Path, required=True)
p.add_argument("--publication", type=Path, required=True)
a = p.parse_args()
cohort = json.loads((a.study / "cohort.json").read_text())
audits = []
sources = set()
with duckdb.connect() as db:
    links = (
        db.execute(
            "select * from read_parquet(?) where building_id in (select unnest(?))",
            [
                str(a.publication / "catalogue/accepted_links.parquet"),
                [r["building_id"] for r in cohort],
            ],
        )
        .fetchdf()
        .to_dict("records")
    )
for r in cohort:
    d = a.study / "cases" / r["formular_id"]
    s = json.loads((d / "source.json").read_text())
    v = s["values"]
    sources.update(x["file"] for x in s["source_rows"])
    observation = json.loads((d / "observation.json").read_text())
    heat = observation["reported_district_heat_kwh"]
    expected = heat + sum(
        number(v, k, required=False) or 0 for k in ["EgiFastighet", "EgiKomfort", "EgiFjarrkyla"]
    )
    field = "EgiSumma3" if r["schema"] == "fore_v260" else "EgiSumma2"
    declared = number(v, field)
    audit = dict(
        formular_id=r["formular_id"],
        sum_field=field,
        declared_kwh=declared,
        reconstructed_kwh=expected,
        residual_kwh=expected - declared,
        reconciled=abs(expected - declared) <= 1,
        history=[x for x in links if x["building_id"] == r["building_id"]],
    )
    audits.append(audit)
    (d / "accounting-audit.json").write_text(json.dumps(audit, indent=2, default=str))
manifest = dict(
    source_files=[dict(path=f, sha256=file_sha256(f)) for f in sorted(sources)],
    publication_catalogue=dict(
        path=str(a.publication / "catalogue/accepted_links.parquet"),
        sha256=file_sha256(a.publication / "catalogue/accepted_links.parquet"),
    ),
    records=audits,
    all_declared_totals_reconcile=all(r["reconciled"] for r in audits),
)
(a.study / "source-audit.json").write_text(json.dumps(manifest, indent=2, default=str))
print("All totals reconcile:", manifest["all_declared_totals_reconcile"])
