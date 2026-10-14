"""Verify paired anomaly evidence without engines or repeated image exports."""

import json

import pandas as pd
import pytest

from examples import plot_weather_campaign_events as E


@pytest.fixture
def event_rows():
    """Describe two weather outages with shared references and different retention."""
    rows = []
    for position, (sampling, hours) in enumerate(
        (("random_seasonal", 12), ("weather_extremes", 168))
    ):
        for event, _ in E.EVENTS:
            for method in ("automatic", "automatic_legacy"):
                exposed = 2 if position == 0 and event == "heatwave" else 0
                overlap = exposed if method == "automatic" else min(exposed, 1)
                exact = min(overlap, 1)
                rows.append(
                    {
                        "site_id": f"site-{position}",
                        "year": 2021 + position,
                        "scope": "meteorology",
                        "sampling": sampling,
                        "gap_hours": hours,
                        "physical_signature": f"physical-mask-{position}",
                        "method": method,
                        "event_type": event,
                        "reference_event_count": exposed + 1,
                        "reference_exposed_events": exposed,
                        "reference_unexposed_events": 1,
                        "reference_hours_hidden": exposed * 2,
                        "reference_event_hours": (exposed + 1) * 72,
                        "exposed_events_with_any_overlap": overlap,
                        "exposed_events_with_exact_bounds": exact,
                        "exposed_events_with_exact_bounds_and_peak": exact
                        if method == "automatic_legacy"
                        else 0,
                        "lost_exposed_reference_events": exposed - overlap,
                        "lost_unexposed_reference_events": 0,
                        "lost_reference_event_count": exposed - overlap,
                        "new_candidate_event_count": (1 if method == "automatic" else 2)
                        if position == 1 and event == "cold_spell"
                        else 0,
                    }
                )
    return pd.DataFrame(rows)


def test_pairs_count_weather_cases_without_solar_or_building_multiplication(event_rows):
    """Two physical outages remain two cases despite three anomaly types each."""
    solar = event_rows.copy()
    solar["scope"] = "solar"
    pairs, metadata = E.pair_events(pd.concat([event_rows, solar], ignore_index=True))
    assert len(pairs) == 2 * 3
    assert metadata["paired_weather_cases"] == 2
    assert metadata["paired_site_years"] == 2
    assert pairs.scope.eq("meteorology").all()
    assert "building_id" not in pairs


def test_retention_fractions_use_exposed_episodes_and_preserve_strict_tiers(event_rows):
    """Two episodes in one weather case supply the shared retention denominator."""
    pairs, _ = E.pair_events(event_rows)
    result = E.summarize(pairs)
    heat = result.loc[
        result.event_type.eq("heatwave")
        & result.sampling.eq("random_seasonal")
        & result.gap_hours.eq(12)
    ].set_index("method")
    assert heat.paired_weather_cases.eq(1).all()
    assert heat.reference_exposed_events.eq(2).all()
    assert heat.site_years_with_exposed_events.eq(1).all()
    assert heat.loc["automatic", "any_overlap_retained_fraction"] == 1.0
    assert heat.loc["automatic", "exact_bounds_retained_fraction"] == 0.5
    assert heat.loc["automatic", "exact_bounds_and_peak_retained_fraction"] == 0.0
    assert heat.loc["automatic_legacy", "any_overlap_retained_fraction"] == 0.5
    assert heat.loc["automatic_legacy", "lost_exposed_reference_events"] == 1


def test_zero_exposure_has_no_rate_but_keeps_new_episodes(event_rows):
    """Creating new cold spells is visible without an exposed reference spell."""
    pairs, _ = E.pair_events(event_rows)
    result = E.summarize(pairs)
    cold = result.loc[
        result.event_type.eq("cold_spell")
        & result.sampling.eq("weather_extremes")
        & result.gap_hours.eq(168)
    ].set_index("method")
    assert cold.reference_exposed_events.eq(0).all()
    assert cold.site_years.eq(1).all()
    assert cold.site_years_with_exposed_events.eq(0).all()
    assert cold.any_overlap_retained_fraction.isna().all()
    assert cold.exact_bounds_and_peak_retained_fraction.isna().all()
    assert cold.loc["automatic", "new_candidate_event_count"] == 1
    assert cold.loc["automatic_legacy", "new_candidate_event_count"] == 2


def test_unmatched_weather_case_is_excluded_and_reported(event_rows):
    """Method availability cannot change the shared comparison denominator silently."""
    extra = event_rows.loc[event_rows.method.eq("automatic")].copy()
    extra["physical_signature"] = extra.physical_signature + "-unmatched"
    pairs, metadata = E.pair_events(pd.concat([event_rows, extra], ignore_index=True))
    assert len(pairs) == 6
    assert metadata["paired_weather_cases"] == 2
    assert metadata["unmatched_weather_cases"] == {
        "automatic": 2,
        "automatic_legacy": 0,
    }


@pytest.mark.parametrize(
    "defect",
    ["duplicate", "reference", "tiers", "negative", "fractional", "missing_type"],
)
def test_invalid_weather_event_evidence_is_rejected(event_rows, defect):
    """Evidence identities and count algebra must be valid before comparisons."""
    altered = event_rows.copy()
    if defect == "duplicate":
        altered = pd.concat([altered, altered.iloc[[0]]], ignore_index=True)
    elif defect == "reference":
        altered.loc[0, "reference_hours_hidden"] += 1
    elif defect == "tiers":
        altered.loc[0, "exposed_events_with_exact_bounds_and_peak"] = 3
    elif defect == "negative":
        altered.loc[0, "new_candidate_event_count"] = -1
    elif defect == "fractional":
        altered["new_candidate_event_count"] = altered.new_candidate_event_count.astype(
            float
        )
        altered.loc[0, "new_candidate_event_count"] = 0.5
    else:
        altered = altered.iloc[1:]
    with pytest.raises(ValueError):
        E.pair_events(altered)


def captured_analysis(root, event_rows, status):
    """Create a tiny analysis with an exact captured anomaly-table fingerprint."""
    event_rows.to_csv(root / "event-exposure-comparisons.csv", index=False)
    manifest = {
        "status": status,
        "incomplete": [],
        "outputs": {
            "event-exposure-comparisons.csv": E.sha(
                (root / "event-exposure-comparisons.csv").read_bytes()
            )
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest))


def test_incomplete_analysis_is_not_rendered_as_final(tmp_path, event_rows):
    """Partial evidence requires explicit provisional mode before writing outputs."""
    captured_analysis(tmp_path, event_rows, "provisional")
    with pytest.raises(ValueError, match="explicit --provisional"):
        E.plot(tmp_path)
    assert not (tmp_path / "event-figures").exists()


def test_altered_event_table_is_rejected_before_figure_publication(
    tmp_path, event_rows
):
    """Apparently complete analysis cannot render bytes outside its exact receipt."""
    captured_analysis(tmp_path, event_rows, "complete")
    with (tmp_path / "event-exposure-comparisons.csv").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="differs from its analysis receipt"):
        E.plot(tmp_path)
    assert not (tmp_path / "event-figures").exists()
