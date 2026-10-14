import argparse
import codecs
import csv
import json
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--epc-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = args.epc_root
args.output.mkdir(parents=True, exist_ok=True)
rows = []
for schema in ["fore_v260", "efter_v260"]:
    counts = Counter()
    for p in sorted((root / schema / "data").glob("*.txt")):
        with p.open("rb") as f:
            encoding = (
                "utf-16"
                if f.read(4).startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                else "latin-1"
            )
        with p.open(encoding=encoding, newline="") as f:
            for n, r in enumerate(csv.DictReader(f, delimiter="|"), 2):
                if r.get("IdLankod", "").zfill(2) + r.get("IdKommunkod", "").zfill(2) != "1480":
                    continue
                counts["municipality_rows"] += 1
                if r.get("EgenByggnadsKat") != "Flerbostadshus":
                    continue
                counts["mfh"] += 1
                try:
                    age = int(r["EgenNybyggAr"])
                except (ValueError, KeyError):
                    continue
                if not 1955 <= age <= 1975:
                    continue
                counts["era"] += 1
                # Retain values in source strings and keep every matching source occurrence.
                rows.append(
                    {
                        "schema": schema,
                        "source_file": str(p),
                        "source_row": n,
                        "encoding": encoding,
                        "values": r,
                    }
                )
    print(schema, dict(counts), flush=True)
(args.output / "candidate-raw.json").write_text(json.dumps(rows, ensure_ascii=False))
print("unique", len({(r["schema"], r["values"]["FormularId"]) for r in rows}))
