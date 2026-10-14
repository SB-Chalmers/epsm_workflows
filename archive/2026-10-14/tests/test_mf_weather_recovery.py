"""Weather identity, missingness and restart invariants without network calls."""

from types import SimpleNamespace

import pytest

from epsm_workflows.campaign import file_sha256
from epsm_workflows.mf_weather_recovery import (
    _seed_cache,
    classify_weather_failure,
    recover_weather,
    weather_jobs,
)


def row(identifier, year=2023, municipality="1480"):
    return {"building_id": identifier, "measurement_year": year, "municipality": municipality,
            "centroid_e": 320000, "centroid_n": 6400000}


def test_jobs_share_geographic_weather_and_ignore_heating():
    rows = [row("a") | {"target": 1}, row("b") | {"target": 999}]
    jobs = weather_jobs(rows)
    assert len(jobs) == 1 and jobs[0]["selected_buildings"] == 2
    assert jobs == weather_jobs([r | {"target": 42} for r in rows])
    assert len(weather_jobs(rows + [row("c", year=2024)])) == 2


def test_seeded_cache_replacements_preserve_prior_payloads(tmp_path):
    original, new = tmp_path / "original", tmp_path / "new"
    original.mkdir()
    payload = original / "abc.json"
    payload.write_text('{"original":true}')
    assert _seed_cache(new, [original]) == 1
    replacement = new / "replacement.tmp"
    replacement.write_text('{"new":true}')
    replacement.replace(new / "abc.json")
    assert payload.read_text() == '{"original":true}'


def test_restart_preserves_weather_and_failures_without_recompilation(tmp_path, monkeypatch):
    calls = []

    def compile_weather(start, end, folder, **kwargs):
        calls.append(start)
        if start.startswith("2024"):
            raise ValueError("Strict source coverage unavailable")
        folder.mkdir(parents=True)
        weather = folder / "weather-2023.epw"
        weather.write_text("declared fake complete calendar")
        return SimpleNamespace(epw_path=str(weather), weather_sha256=file_sha256(weather)), {
            "config": {}, "result": {},
        }

    monkeypatch.setattr("epsm_workflows.mf_weather_recovery.compile_measurement_weather", compile_weather)
    monkeypatch.setattr("epsm_workflows.mf_weather_recovery.validate_epw_calendar", lambda path, year: {"year": year})
    selection = [row("a"), row("b", year=2024)]
    first = recover_weather(selection, {}, tmp_path / "study", progress=False)
    assert first["summary"]["completed"] == 1 and first["summary"]["failed"] == 1
    second = recover_weather(selection, {}, tmp_path / "study", progress=False)
    assert len(calls) == 2 and second["summary"]["completed"] == 1
    assert second["status"][1]["reason"].endswith("Strict source coverage unavailable")
    with pytest.raises(ValueError, match="selection changed"):
        recover_weather(selection + [row("c")], {}, tmp_path / "study", progress=False)


def test_existing_weather_requires_checksum_and_calendar_and_keeps_query_point(tmp_path, monkeypatch):
    path = tmp_path / "existing.epw"
    path.write_text("prior weather")
    prior = {"1480:2023": {
        "key": "1480:2023", "status": "completed", "path": str(path),
        "sha256": file_sha256(path), "latitude": 57, "longitude": 12,
    }}
    monkeypatch.setattr("epsm_workflows.mf_weather_recovery.validate_epw_calendar", lambda path, year: {"year": year})
    result = recover_weather([row("a")], prior, tmp_path / "study", progress=False)
    assert result["summary"]["reused"] == 1
    assert result["manifest"]["1480:2023"]["latitude"] == 57
    path.write_text("changed weather")
    result = recover_weather([row("a")], prior, tmp_path / "other", progress=False)
    assert result["summary"]["failed"] == 1
    assert "checksum changed" in result["status"][0]["reason"]


def test_offline_source_unavailability_is_distinct_from_weather_quality_failure():
    blocked = classify_weather_failure({"status": "failed", "reason": "DataGapError: Offline weather cache miss: https://public.example/weather"})
    assert blocked["external_authorization_required"]
    assert blocked["failure_classification"] == "offline_cache_unavailable_requires_external_authorization"
    quality = classify_weather_failure({"status": "failed", "reason": "Invalid dry bulb temperature"})
    assert not quality["external_authorization_required"]
