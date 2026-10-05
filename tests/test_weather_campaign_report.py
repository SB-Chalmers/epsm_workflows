"""Protect scientific denominators and final-report provenance without live runs."""

import json
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from examples import report_weather_imputation_campaign as R


def paired_rows():
    """Create unequal site-year sample sizes with known physical error contrasts."""
    rows = []
    for position, site in enumerate(("a", "b", "b", "b")):
        row = {
            "site_id": site,
            "year": 2023,
            "building_id": "profile",
            "scope": "meteorology",
            "sampling": "random_seasonal",
            "gap_hours": 12,
            "physical_signature": f"mask-{position}",
        }
        for mode in ("heating", "cooling"):
            for quantity, unit in (("annual", "kwh_m2"), ("peak", "w_m2")):
                metric = f"absolute_{quantity}_{mode}_error_{unit}"
                new, old = (1.0, 4.0) if site == "a" else (10.0, 5.0)
                row[metric + "_new"] = new
                row[metric + "_old"] = old
                row[metric + "_change"] = new - old
            metric = f"hourly_{mode}_load_mae_w_m2"
            row[metric + "_new"] = 1.0
            row[metric + "_old"] = 2.0
            row[metric + "_change"] = -1.0
            row[f"baseline_peak_{mode}_w_m2"] = 20.0
            for side in ("new", "old"):
                row[f"baseline_{mode}_peak_hidden_{side}"] = False
                row[f"same_{mode}_peak_hours_{side}"] = True
        rows.append(row)
    return pd.DataFrame(rows)


def test_paired_digest_equal_site_year_weights_differ_from_response_weights():
    """Unequal repeated masks cannot silently overweight one selected site-year."""
    result = R.paired_digest(paired_rows()).set_index("mode")
    row = result.loc["heating"]
    assert row.paired_building_responses == 4
    assert row.site_years == 2
    assert row.annual_new_mean_kwh_m2 == pytest.approx(7.75)
    assert row.annual_new_equal_site_year_mean_kwh_m2 == pytest.approx(5.5)
    assert row.annual_old_equal_site_year_mean_kwh_m2 == pytest.approx(4.5)
    assert row.annual_improved_n == 1
    assert row.annual_worsened_n == 3


def test_unexposed_and_zero_peaks_have_no_preservation_fraction():
    """Untouched positive peaks and zero loads cannot count as exposed successes."""
    pairs = paired_rows()
    pairs["baseline_peak_cooling_w_m2"] = 0.0
    pairs["baseline_cooling_peak_hidden_new"] = True
    result = R.paired_digest(pairs).set_index("mode")
    assert result.loc["heating", "peak_exposed_n"] == 0
    assert result.loc["heating", "peak_unexposed_n"] == 4
    assert result.loc["cooling", "peak_exposed_n"] == 0
    assert result.loc["cooling", "peak_unexposed_n"] == 0
    assert result.loc["cooling", "zero_or_numerical_reference_peak_n"] == 4
    for mode in ("heating", "cooling"):
        for side in ("new", "old"):
            assert pd.isna(
                result.loc[mode, f"peak_hours_preserved_{side}_exposed_fraction"]
            )


def test_exposed_peak_fraction_uses_only_positive_exposed_responses():
    """Peak rates must ignore unexposed ties and numerical reference maxima."""
    pairs = paired_rows()
    pairs["baseline_heating_peak_hidden_new"] = [True, True, False, True]
    pairs["baseline_peak_heating_w_m2"] = [20.0, 20.0, 20.0, 1e-7]
    pairs["same_heating_peak_hours_new"] = [True, False, True, True]
    pairs["same_heating_peak_hours_old"] = [False, False, True, True]
    row = R.paired_digest(pairs).set_index("mode").loc["heating"]
    assert row.peak_exposed_n == 2
    assert row.peak_unexposed_n == 1
    assert row.zero_or_numerical_reference_peak_n == 1
    assert row.peak_hours_preserved_new_exposed_fraction == 0.5
    assert row.peak_hours_preserved_old_exposed_fraction == 0.0


def test_report_ties_use_area_normalized_numerical_tolerances():
    """Equality at the tolerance boundary remains a tie for each load metric."""
    pairs = paired_rows()
    for quantity, metric, tolerance in (
        ("annual", "absolute_annual_heating_error_kwh_m2", 1e-8),
        ("peak", "absolute_peak_heating_error_w_m2", 1e-6),
        ("hourly", "hourly_heating_load_mae_w_m2", 1e-6),
    ):
        pairs[metric + "_change"] = np.array([-2, -1, 1, 2]) * tolerance
    row = R.paired_digest(pairs).set_index("mode").loc["heating"]
    for quantity in ("annual", "peak", "hourly"):
        assert row[quantity + "_improved_n"] == 1
        assert row[quantity + "_worsened_n"] == 1
        assert row[quantity + "_tied_n"] == 2


def write_json(path, value):
    """Write fixture metadata with deterministic readable JSON bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


@pytest.fixture
def completed_campaign(tmp_path):
    """Provide complete captured evidence and opaque figures bound by receipts."""
    root = tmp_path / "campaign"
    analysis = root / "analysis"
    analysis.mkdir(parents=True)
    pairs = paired_rows()
    comparisons = []
    for row in pairs.to_dict("records"):
        for side, method in (("new", "automatic"), ("old", "automatic_legacy")):
            comparison = {
                key: value
                for key, value in row.items()
                if not key.endswith(("_new", "_old", "_change"))
            }
            comparison["method"] = method
            for key, value in row.items():
                if key.endswith("_" + side):
                    comparison[key[: -len(side) - 1]] = value
            comparisons.append(comparison)
    comparisons = pd.DataFrame(comparisons)
    availability = pd.DataFrame(
        [
            {
                "scope": "meteorology",
                "sampling": "random_seasonal",
                "gap_hours": 12,
                "method": "automatic",
                "planned_distinct_panels": 6,
                "truth_ready_panels": 6,
                "available_weather_panels": 4,
                "unavailable_weather_panels": 2,
                "unsupported_truth_panels": 0,
                "unsupported_method_panels": 0,
                "not_prepared_panels": 0,
                "panels_with_all_buildings_evaluated": 4,
                "available_fraction_of_truth_ready": 4 / 6,
            }
        ]
    )
    events = pd.DataFrame(
        [
            {
                "event_type": "heatwave",
                "scope": "meteorology",
                "sampling": "random_seasonal",
                "gap_hours": 12,
                "method": "automatic",
                "site_years": 2,
                "weather_outages": 4,
                "reference_exposed_events": 0,
                "reference_hours_hidden": 0,
                "lost_exposed_reference_events": 0,
                "new_candidate_event_count": 0,
                "exposed_events_any_overlap_fraction": None,
                "exposed_events_exact_bounds_fraction": None,
                "exposed_events_exact_bounds_and_peak_fraction": None,
            }
        ]
    )
    sources = pd.DataFrame(
        [{"site_id": site, "year": 2023, "status": "ready"} for site in ("a", "b")]
    )
    tables = {
        "hierarchy-pairs.csv": pairs,
        "deduplicated-comparisons.csv": comparisons,
        "method-availability.csv": availability,
        "common-method-comparisons.csv": comparisons,
        "event-exposure-summary.csv": events,
        "sources.csv": sources,
    }
    outputs = {}
    for name, data in tables.items():
        data.to_csv(analysis / name, index=False)
        outputs[name] = R.sha((analysis / name).read_bytes())
    matrix = {
        "sites": [{"site_id": site} for site in ("a", "b")],
        "years": [2023],
        "buildings": ["profile"],
        "meteorology_methods": ["automatic", "automatic_legacy"],
        "solar_methods": ["automatic", "automatic_legacy"],
    }
    write_json(root / "matrix.json", matrix)
    coverage = {
        "complete_full_matrix": True,
        "comparison_rows": 10,
        "location_years_completed_full_matrix": 2,
        "reference_building_location_years": 2,
        "planned_location_years": 2,
        "planned_reference_building_location_years": 2,
    }
    write_json(root / "coverage.json", coverage)
    source_path = root / "datasets/a/2023/sources/receipt.json"
    write_json(source_path, {"status": "ready", "station_id": "fixture-a"})
    manifest = {
        "status": "complete",
        "campaign_root": str(root.resolve()),
        "incomplete": [],
        "captured_building_rows": 10,
        "deduplicated_comparisons": len(comparisons),
        "hierarchy_pairs": len(pairs),
        "common_method_comparisons": len(comparisons),
        "matrix_sha256": R.sha((root / "matrix.json").read_bytes()),
        "inputs": {str(source_path): R.sha(source_path.read_bytes())},
        "outputs": outputs,
    }
    write_json(analysis / "manifest.json", manifest)
    for site, count in (("a", 3), ("b", 7)):
        write_json(
            root / "datasets" / site / "2023/audit.json",
            {
                "full_matrix_site_year_complete": True,
                "diagnostics_accepted": True,
                "warnings": 0,
                "severe": 0,
                "fatal": 0,
                "comparisons": count,
                "unique_simulations_accumulated": count,
            },
        )
    bind_figures(root)
    return root


def bind_figures(root):
    """Bind fixture figure bytes to the current captured analysis manifest."""
    folder = root / "analysis/figures"
    folder.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for stem in (
        "paired-annual-errors",
        "paired-peak-errors",
        "building-profile-changes",
        "exposed-peak-timing",
    ):
        for extension in ("png", "svg"):
            path = folder / f"{stem}.{extension}"
            path.write_bytes(f"opaque fixture {stem} {extension}".encode())
            outputs[path.name] = R.sha(path.read_bytes())
    path = folder / "figure-data.csv"
    path.write_text("paired_responses\n4\n")
    outputs[path.name] = R.sha(path.read_bytes())
    write_json(
        folder / "manifest.json",
        {
            "status": "complete",
            "analysis_manifest_sha256": R.sha(
                (root / "analysis/manifest.json").read_bytes()
            ),
            "paired_responses": 4,
            "site_years": 2,
            "outputs": outputs,
        },
    )


def test_verified_complete_campaign_publishes_digest_and_report(completed_campaign):
    """Clean completed evidence produces an exact receipt for readable outputs."""
    receipt = R.publish(completed_campaign)
    assert receipt["status"] == "complete"
    assert receipt["unique_annual_simulations"] == 10
    for name, expected in receipt["outputs"].items():
        assert R.sha((completed_campaign / name).read_bytes()) == expected
    digest = pd.read_csv(completed_campaign / "analysis/hierarchy-digest.csv")
    assert (
        digest.loc[digest["mode"].eq("heating"), "paired_building_responses"].item()
        == 4
    )


def test_final_availability_table_keeps_success_and_failure_counts(completed_campaign):
    """Actual analysis availability columns must remain visible in the synthesis."""
    R.publish(completed_campaign)
    page = (completed_campaign / "synthesis.html").read_text()
    for column in (
        "planned_distinct_panels",
        "available_weather_panels",
        "unavailable_weather_panels",
        "not_prepared_panels",
    ):
        assert column in page


@pytest.mark.parametrize(
    "mutation",
    [
        "analysis_provisional",
        "analysis_incomplete",
        "coverage_incomplete",
        "coverage_count",
        "matrix_bytes",
        "campaign_origin",
        "pairs_count",
        "comparison_count",
        "table_bytes",
        "captured_input_bytes",
        "audit_incomplete",
        "audit_warnings",
        "audit_count",
        "audit_comparison_count",
        "plot_provisional",
        "plot_manifest_binding",
        "plot_bytes",
    ],
)
def test_publication_rejects_unfinished_or_unbound_evidence(
    completed_campaign, mutation
):
    """Every rejected guard fails before either final scientific output is written."""
    root = completed_campaign
    analysis = root / "analysis"
    if mutation.startswith("analysis_") or mutation in {
        "campaign_origin",
        "pairs_count",
        "comparison_count",
    }:
        path = analysis / "manifest.json"
        value = json.loads(path.read_text())
        if mutation == "analysis_provisional":
            value["status"] = "provisional"
        elif mutation == "analysis_incomplete":
            value["incomplete"] = ["a/2023: checkpoint absent"]
        elif mutation == "campaign_origin":
            value["campaign_root"] = str(root.parent / "different-campaign")
        else:
            key = (
                "hierarchy_pairs"
                if mutation == "pairs_count"
                else "deduplicated_comparisons"
            )
            value[key] += 1
        write_json(path, value)
        bind_figures(root)
    elif mutation.startswith("coverage_"):
        path = root / "coverage.json"
        value = json.loads(path.read_text())
        if mutation == "coverage_incomplete":
            value["complete_full_matrix"] = False
        else:
            value["comparison_rows"] += 1
        write_json(path, value)
    elif mutation == "matrix_bytes":
        with (root / "matrix.json").open("a") as stream:
            stream.write("\n")
    elif mutation == "table_bytes":
        with (analysis / "hierarchy-pairs.csv").open("a") as stream:
            stream.write("\n")
    elif mutation == "captured_input_bytes":
        with (root / "datasets/a/2023/sources/receipt.json").open("a") as stream:
            stream.write("\n")
    elif mutation.startswith("audit_"):
        path = root / "datasets/a/2023/audit.json"
        value = json.loads(path.read_text())
        if mutation == "audit_incomplete":
            value["full_matrix_site_year_complete"] = False
        elif mutation == "audit_warnings":
            value["warnings"] = 1
        elif mutation == "audit_comparison_count":
            value["comparisons"] += 1
        else:
            path.unlink()
            value = None
        if value is not None:
            write_json(path, value)
    else:
        path = analysis / "figures/manifest.json"
        value = json.loads(path.read_text())
        if mutation == "plot_provisional":
            value["status"] = "provisional"
        elif mutation == "plot_manifest_binding":
            value["analysis_manifest_sha256"] = "incorrect"
        else:
            with (analysis / "figures/paired-annual-errors.png").open("ab") as stream:
                stream.write(b"changed")
        write_json(path, value)
    with pytest.raises(ValueError):
        R.publish(root)
    assert not (root / "synthesis.html").exists()
    assert not (analysis / "hierarchy-digest.csv").exists()


def test_primary_digest_does_not_mix_solar_into_meteorological_masks(
    completed_campaign,
):
    """A separately valid solar cohort cannot inflate the primary MET error digest."""
    root = completed_campaign
    analysis = root / "analysis"
    pairs = pd.read_csv(analysis / "hierarchy-pairs.csv")
    solar = deepcopy(pairs.iloc[[0]])
    solar["scope"] = "solar"
    for column in solar:
        if column.startswith(("absolute_", "hourly_")):
            solar[column] = 1000.0
    pd.concat([pairs, solar], ignore_index=True).to_csv(
        analysis / "hierarchy-pairs.csv", index=False
    )
    manifest = json.loads((analysis / "manifest.json").read_text())
    manifest["hierarchy_pairs"] += 1
    manifest["outputs"]["hierarchy-pairs.csv"] = R.sha(
        (analysis / "hierarchy-pairs.csv").read_bytes()
    )
    write_json(analysis / "manifest.json", manifest)
    bind_figures(root)
    R.publish(root)
    digest = pd.read_csv(analysis / "hierarchy-digest.csv").set_index("mode")
    assert digest.loc["heating", "paired_building_responses"] == 4
    assert digest.loc["heating", "annual_new_equal_site_year_mean_kwh_m2"] == 5.5
