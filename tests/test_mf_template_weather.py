"""Weather revisions must not discard validated building preparation templates."""

import csv
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from epsm_core import (
    AirflowSpec,
    BuildingSpec,
    ClimateSpec,
    FacadeSpec,
    GeometrySpec,
    load_sveby_spec,
)
from epsm_data import resolve_building_archetype
from shapely.geometry import box

from epsm_workflows import mf_parent_dynamic as study
from epsm_workflows.campaign import atomic_json, file_sha256
from epsm_workflows.cohort_transfer.common import read


def weather_file(path, year=2023, *, omit_last=False, temperature=5.0):
    headers = [
        [
            "LOCATION",
            "Fixture",
            "Sweden",
            "SWE",
            "SMHI",
            "999999",
            "65",
            "21",
            "1",
            "40",
        ],
        ["DESIGN CONDITIONS", "0"],
        ["TYPICAL/EXTREME PERIODS", "0"],
        ["GROUND TEMPERATURES", "0"],
        ["HOLIDAYS/DAYLIGHT SAVINGS", "No", "0", "0", "0"],
        ["COMMENTS 1", "test"],
        ["COMMENTS 2", "test"],
        ["DATA PERIODS", "1", "1", "Data", "Sunday", f"1/1/{year}", f"12/31/{year}"],
    ]
    records = []
    date = datetime(year, 1, 1)  # noqa: DTZ001 - EPW labels use local standard time.
    while date.year == year:
        row = [0] * 35
        row[:6] = [year, date.month, date.day, date.hour + 1, 60, "test"]
        row[6:10] = [temperature, 0, 50, 101325]
        row[20:22] = [180, 3]
        records.append(row)
        date += timedelta(hours=1)
    if omit_last:
        records.pop()
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerows(headers + records)
    return {"path": str(path), "sha256": file_sha256(path), "year": year}


@pytest.fixture
def original_spec(tmp_path):
    old_weather = tmp_path / "old-weather.epw"
    old_weather.write_text("old weather bytes remain unchanged\n")
    physical = resolve_building_archetype(
        category="multi_family",
        construction_year=1965,
        climate_zone="SE.Zone3",
        ventilation="F",
    )
    return BuildingSpec(
        identifier="building-0",
        geometry=GeometrySpec.from_footprint(
            box(0, 0, 20, 10), storey_elevations_m=(0, 3, 6)
        ),
        envelope=physical["envelope"],
        facade=FacadeSpec(
            default_wwr=0.2, glazing=physical["glazing"], solar_shading_factor=1.0
        ),
        airflow=AirflowSpec(
            topology="F", mechanical_vent_l_s_m2=0.35, **physical["core_airflow_inputs"]
        ),
        use=load_sveby_spec().use_spec(calendar_year=2023),
        climate=ClimateSpec(
            epw_path=str(old_weather),
            weather_sha256=file_sha256(old_weather),
            latitude_deg=57,
            longitude_deg=12,
            elevation_m=0,
            time_zone_hours=1,
            calendar_year=2023,
        ),
        load_reference_area_m2=350,
    )


def test_weather_refresh_replaces_only_climate_and_preserves_original(
    original_spec, tmp_path
):
    weather = weather_file(tmp_path / "replacement.epw")
    before = original_spec.model_dump(mode="json")
    refreshed = study.refresh_template_climate(original_spec, weather, 2023)
    assert refreshed.climate.weather_sha256 == weather["sha256"]
    assert refreshed.climate.epw_path == weather["path"]
    assert refreshed.climate.calendar_year == 2023
    assert refreshed.climate.latitude_deg == 65
    assert refreshed.climate.longitude_deg == 21
    assert refreshed.climate.elevation_m == 40
    assert refreshed.model_dump(
        mode="json", exclude={"climate"}
    ) == original_spec.model_dump(mode="json", exclude={"climate"})
    assert refreshed.load_reference_area_m2 == 350
    assert refreshed.envelope == original_spec.envelope
    assert refreshed.airflow == original_spec.airflow
    assert refreshed.geometry == original_spec.geometry
    assert refreshed.facade.solar_shading_factor == 1.0
    assert original_spec.model_dump(mode="json") == before


def test_changed_file_sha_is_rejected_even_after_validated_cache_hit(
    original_spec, tmp_path
):
    weather = weather_file(tmp_path / "replacement.epw")
    study.refresh_template_climate(original_spec, weather, 2023)
    with (tmp_path / "replacement.epw").open("a") as stream:
        stream.write("altered bytes\n")
    with pytest.raises(ValueError, match="checksum"):
        study.refresh_template_climate(original_spec, weather, 2023)


@pytest.mark.parametrize("measurement_year", [True, 2023.5, "2023", float("nan")])
def test_invalid_measurement_year_is_rejected(
    original_spec, tmp_path, measurement_year
):
    with pytest.raises(ValueError, match="integer measurement year"):
        study.refresh_template_climate(
            original_spec, weather_file(tmp_path / "replacement.epw"), measurement_year
        )


def test_declared_and_embedded_source_year_must_match_measurement_year(
    original_spec, tmp_path
):
    weather = weather_file(tmp_path / "wrong-year.epw", year=2022)
    with pytest.raises(ValueError, match="Declared.*year"):
        study.refresh_template_climate(original_spec, weather, 2023)
    # Calendar-only validation accepts matching non-leap month/day/hour axes.
    # This workflow also requires the actual source-year labels to agree.
    with pytest.raises(ValueError, match="source years"):
        study.refresh_template_climate(original_spec, weather | {"year": 2023}, 2023)


def test_incomplete_calendar_is_not_a_valid_replacement(original_spec, tmp_path):
    weather = weather_file(tmp_path / "incomplete.epw", omit_last=True)
    with pytest.raises(ValueError, match="calendar"):
        study.refresh_template_climate(original_spec, weather, 2023)


def test_checksum_rechecked_after_climate_header_read(
    original_spec, tmp_path, monkeypatch
):
    from epsm_core import weather as weather_module

    weather = weather_file(tmp_path / "replacement.epw")
    real_read = weather_module.climate_from_epw

    def changed_climate(*args, **kwargs):
        return real_read(*args, **kwargs).updated(weather_sha256="0" * 64)

    monkeypatch.setattr(weather_module, "climate_from_epw", changed_climate)
    with pytest.raises(ValueError, match="while reading climate"):
        study.refresh_template_climate(original_spec, weather, 2023)


def test_prepare_retains_matching_template_when_weather_is_revised(
    original_spec, tmp_path, monkeypatch
):
    old, selection, output = (
        tmp_path / name for name in ("old", "selection", "prepared")
    )
    engine = tmp_path / "engine"
    engine.write_text("engine fixture\n")
    source_file = tmp_path / "source.csv"
    source_file.write_text("source fixture\n")
    source = {
        "building_id": "building-0",
        "property_key": "property-0",
        "era": "SE.02",
        "ventilation": "F",
        "construction_year": 1965,
        "measurement_year": 2023,
        "catalogue_climate_zone": "SE.Zone3",
        "municipality": "1480",
        "use_group": "multi_family",
        "heating_family": "district_heat",
        "atemp_m2": 350.0,
        "storeys": 2,
        "status": "prepared",
        "source_file": str(source_file),
        "source_line": 2,
        "source_row_sha256": "0" * 64,
        "formular_id": 1,
        "spec_path": "specs/building-0/baseline.json",
    }
    weather = weather_file(tmp_path / "replacement.epw")
    new_manifest = tmp_path / "new-weather.json"
    atomic_json(new_manifest, {"1480:2023": weather})
    scope_evidence = tmp_path / "scope-evidence.json"
    atomic_json(
        scope_evidence,
        {
            "schema_version": 1,
            "records": {
                source["building_id"]: {
                    **{
                        name: source[name]
                        for name in (
                            "building_id",
                            "formular_id",
                            "property_key",
                            "source_row_sha256",
                        )
                    },
                    "release_subject_count": 1,
                    "archive_subject_count": 1,
                }
            },
        },
    )
    atomic_json(selection / "cohort.json", [source])
    atomic_json(
        old / "config.json",
        {
            "engine": str(engine),
            "weather_manifest": {
                "1480:2023": {
                    "path": original_spec.climate.epw_path,
                    "sha256": original_spec.climate.weather_sha256,
                }
            },
        },
    )
    atomic_json(old / "features.json", [source])
    atomic_json(old / "manifest.json", {"runtime": {}})
    atomic_json(
        old / "engine.json",
        {"engine_hash": file_sha256(engine), "schema_hash": "0" * 64},
    )
    atomic_json(old / source["spec_path"], original_spec.model_dump(mode="json"))
    receipt_path = old / "runs" / "case" / "receipt.json"
    atomic_json(
        receipt_path,
        {
            "building_id": "building-0",
            "scenario": "baseline",
            "status": "accepted",
            "prediction_kwh_m2": 100.0,
            "physical_hash": original_spec.physical_hash(),
            "spec_path": source["spec_path"],
        },
    )
    preserved = {
        path: file_sha256(path) for path in (old / source["spec_path"], receipt_path)
    }

    def forbidden_rebuild(*args, **kwargs):
        pytest.fail("Matching physical template must not be reconstructed")

    monkeypatch.setattr(study, "build_baseline", forbidden_rebuild)
    monkeypatch.setattr(study, "physical_descriptors", lambda row, spec: {})
    monkeypatch.setattr(
        study, "_targets", lambda rows: {row["building_id"]: 100.0 for row in rows}
    )
    monkeypatch.setattr(study, "source_identity", lambda: {"test": "runtime"})
    monkeypatch.setattr(
        "epsm_core.EnergyPlusRunner",
        lambda *args, **kwargs: SimpleNamespace(
            compiler=SimpleNamespace(schema_hash="0" * 64)
        ),
    )
    summary = study.prepare(
        selection, old, output, weather=new_manifest, scope_evidence=scope_evidence
    )
    assert summary["prepared"] == 1
    assert summary["current_native_receipts_imported"] == 0
    prepared = BuildingSpec.model_validate(read(output / source["spec_path"]))
    assert prepared.climate.weather_sha256 == weather["sha256"]
    assert prepared.climate.calendar_year == 2023
    assert prepared.facade.solar_shading_factor == 0.5
    assert prepared.geometry == original_spec.geometry
    assert prepared.envelope == original_spec.envelope
    assert prepared.airflow == original_spec.airflow
    assert prepared.load_reference_area_m2 == original_spec.load_reference_area_m2
    assert prepared.use.model_dump(exclude={"equipment_gain_w_m2"}) == (
        original_spec.use.model_dump(exclude={"equipment_gain_w_m2"})
    )
    assert prepared.use.equipment_gain_w_m2 * sum(
        prepared.use.equipment_fraction.hourly(2023)
    ) / 1000 == pytest.approx(21.0)
    assert read(output / "features.json")[0]["construction_year"] == 1965
    assert (
        read(output / "preparation-outcomes.json")[0]["template_weather_refresh"][
            "building_physical_inputs_preserved"
        ]
        is True
    )
    assert preserved == {path: file_sha256(path) for path in preserved}
    study.verify(output)
