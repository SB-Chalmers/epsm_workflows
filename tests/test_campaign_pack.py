"""A study file becomes one self-contained, zipped Vera package."""

import json
import zipfile

import pytest

pytest.importorskip("scipy")
pytest.importorskip("duckdb")
pytest.importorskip("epsm_core")
import duckdb
from shapely.geometry import Polygon, mapping

from epsm_workflows.campaign.__main__ import main
from epsm_workflows.campaign.geometry import (
    FOOTPRINT_FORMAT,
    freeze_footprints,
    load_footprints,
)
from epsm_workflows.campaign.pack import pack
from epsm_workflows.campaign.study import load_study


def write_study(tmp_path, epw, *, footprints=None):
    study = {
        "kind": "stock_diversity", "name": "tiny", "seed": 7,
        "cities": [{"slug": "malmo", "city": "Malmö", "tmy": epw.name}],
        "contexts": [{"name": "urban_lowrise_dense", "preset": "urban_lowrise_dense"}],
        "geometry": {"families": ["rectangle", "L"], "real_footprints": footprints, "real_fraction": 0.5 if footprints else 0,
                     "anchor_footprints": 2},
        "stock": {"diversity_designs": 4, "anchor_designs": 4},
    }
    path = tmp_path / "tiny.json"
    path.write_text(json.dumps(study))
    (tmp_path / epw.name).write_bytes(epw.read_bytes())
    return path


def test_freeze_footprints_from_records(tmp_path):
    records = [
        {"format": "epsm-record-1", "crs": "EPSG:3006", "record": {"building_id": "b2", "dataset_id": "d",
         "geometry": mapping(Polygon([(100, 100), (130, 100), (130, 120), (100, 120)])), "facts": {"storeys": 3}}},
        {"building_id": "b1", "geometry": mapping(Polygon([(0, 0), (24, 0), (24, 12), (0, 12)])), "facts": {}},
        {"building_id": "tiny", "geometry": mapping(Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])), "facts": {}},
    ]
    report = freeze_footprints(records, tmp_path / "fp.json")
    assert report["accepted"] == 2 and report["rejected"][0]["building_id"] == "tiny"
    frozen = load_footprints(tmp_path / "fp.json")
    assert [f["footprint_id"] for f in frozen] == ["b1", "b2"]
    assert frozen[1]["storeys"] == 3 and frozen[1]["polygon"].bounds[:2] == (0, 0) and frozen[1]["area_m2"] == 600
    assert json.loads((tmp_path / "fp.json").read_text())["format"] == FOOTPRINT_FORMAT


def test_pack_builds_plan_weather_readme_and_archive(tmp_path, calendar_epw, core_solves_u_values):
    records = [
        {"building_id": f"r{i}", "geometry": mapping(Polygon([(0, 0), (30 + i, 0), (30 + i, 20), (0, 20)])), "facts": {"storeys": 3}}
        for i in range(2)
    ]
    freeze_footprints(records, tmp_path / "fp.json")
    study_path = write_study(tmp_path, calendar_epw, footprints="fp.json")
    study = load_study(study_path)
    assert study.geometry.real_footprints == str(tmp_path / "fp.json")
    result = pack(study_path, tmp_path / "out", workers=1)
    root = tmp_path / "out" / f"tiny-{study.id[:12]}"
    assert result["runs"] == 8 and result["groups"] == 8 and result["pilot_runs"] == 4
    assert result["buildings"] == 8 and result["sampling_validation"]["strata"] == 6
    assert result["input_columns"]["ground_method"] == "VARCHAR"
    names = zipfile.ZipFile(result["archive"]).namelist()
    assert {f"{root.name}/{n}" for n in ("plan.duckdb", "plan.json", "study.json", "manifest.json", "README.md",
                                            "vera.sh", "weather.json", "footprints.json", "weather/malmo/tmy.epw")} <= set(names)
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["study_id"] == study.id and manifest["plan"]["runs"] == 8
    readme = (root / "README.md").read_text()
    assert "sbatch vera.sh" in readme and study.id in readme
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    sources = dict(plan.execute("SELECT geometry_source,count(*) FROM runs GROUP BY 1").fetchall())
    assert sources == {"real": 4, "synthetic": 4}
    assert plan.execute("SELECT count(*) FROM weather").fetchone()[0] == 8760
    assert json.loads(plan.execute("SELECT value FROM manifest WHERE key='study'").fetchone()[0])["name"] == "tiny"
    plan.close()
    with pytest.raises(FileExistsError):
        pack(study_path, tmp_path / "out")


def test_cli_validate_reports_identity(tmp_path, calendar_epw, capsys):
    study_path = write_study(tmp_path, calendar_epw)
    assert main(["validate", str(study_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "stock_diversity" and report["cities"] == ["malmo"] and len(report["id"]) == 64
