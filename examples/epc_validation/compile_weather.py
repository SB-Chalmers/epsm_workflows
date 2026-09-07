import argparse
import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from epsm_workflows.weather import compile_measurement_weather

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--years", type=int, nargs="+", required=True)
parser.add_argument("--latitude", type=float, required=True)
parser.add_argument("--longitude", type=float, required=True)
args = parser.parse_args()
logging.basicConfig(level=logging.INFO)


def one(year):
    d = args.output / "weather" / str(year)
    d.mkdir(parents=True, exist_ok=True)
    try:
        climate, report = compile_measurement_weather(
            f"{year}-01-01", f"{year}-12-31", d, latitude=args.latitude, longitude=args.longitude
        )
        print("WEATHER COMPLETE", year, climate.epw_path, flush=True)
        return True
    except Exception as e:
        (d / "failure.json").write_text(
            json.dumps({"error": str(e), "traceback": traceback.format_exc()})
        )
        print("WEATHER FAILED", year, str(e), flush=True)
        return False


with ThreadPoolExecutor(max_workers=2) as pool:
    completed = list(pool.map(one, args.years))
if not all(completed):
    raise SystemExit("Some weather files failed; see failure.json")
