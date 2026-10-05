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


def bind_event_figure(root):
    """Attach opaque event figures to the same completed analysis receipt."""
    folder = root / "analysis/event-figures"
    folder.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for extension in ("png", "svg"):
        path = folder / f"paired-weather-event-preservation.{extension}"
        path.write_bytes(f"opaque paired weather-event fixture {extension}".encode())
        outputs[path.name] = R.sha(path.read_bytes())
    write_json(
        folder / "manifest.json",
        {
            "status": "complete",
            "analysis_manifest_sha256": R.sha(
                (root / "analysis/manifest.json").read_bytes()
            ),
            "outputs": outputs,
        },
    )


def test_verified_optional_event_figure_is_embedded_and_bound(completed_campaign):
    """Optional completed event figures are linked and fingerprinted in synthesis."""
    bind_event_figure(completed_campaign)
    receipt = R.publish(completed_campaign)
    event_manifest = completed_campaign / "analysis/event-figures/manifest.json"
    assert receipt["event_plot_manifest_sha256"] == R.sha(event_manifest.read_bytes())
    page = (completed_campaign / "synthesis.html").read_text()
    for extension in ("png", "svg"):
        assert f"event-figures/paired-weather-event-preservation.{extension}" in page


@pytest.mark.parametrize("defect", ["provisional", "analysis_binding", "image_bytes"])
def test_unbound_optional_event_figure_rejects_final_report(completed_campaign, defect):
    """Optional event figures must be completed and tied to the final evidence."""
    root = completed_campaign
    bind_event_figure(root)
    path = root / "analysis/event-figures/manifest.json"
    manifest = json.loads(path.read_text())
    if defect == "provisional":
        manifest["status"] = "provisional"
    elif defect == "analysis_binding":
        manifest["analysis_manifest_sha256"] = "different-analysis"
    else:
        with (path.parent / "paired-weather-event-preservation.png").open(
            "ab"
        ) as stream:
            stream.write(b"changed")
    write_json(path, manifest)
    with pytest.raises(ValueError, match="event figure|Event figure"):
        R.publish(root)
    assert not (root / "synthesis.html").exists()
    assert not (root / "analysis/hierarchy-digest.csv").exists()


def bind_method_figures(root):
    """Bind both comparison scopes to the actual completed analysis bytes."""
    folder = root / "analysis/method-figures"
    folder.mkdir()
    outputs = {}
    for scope in ("meteorology", "solar"):
        for extension in ("png", "svg"):
            name = f"common-method-{scope}-errors.{extension}"
            path = folder / name
            path.write_bytes(f"method figure {scope} {extension}".encode())
            outputs[name] = R.sha(path.read_bytes())
    write_json(
        folder / "manifest.json",
        {
            "status": "complete",
            "analysis_manifest_sha256": R.sha(
                (root / "analysis/manifest.json").read_bytes()
            ),
            "outputs": outputs,
        },
    )


def test_final_report_embeds_verified_common_method_figures(completed_campaign):
    """All fallback scopes are visible and bound in the final report receipt."""
    bind_method_figures(completed_campaign)
    receipt = R.publish(completed_campaign)
    manifest = completed_campaign / "analysis/method-figures/manifest.json"
    assert receipt["method_plot_manifest_sha256"] == R.sha(manifest.read_bytes())
    page = (completed_campaign / "synthesis.html").read_text()
    for scope in ("meteorology", "solar"):
        for extension in ("png", "svg"):
            assert f"method-figures/common-method-{scope}-errors.{extension}" in page


@pytest.mark.parametrize("defect", ["provisional", "analysis_binding", "image_bytes"])
def test_unbound_method_figures_reject_final_report(completed_campaign, defect):
    """Partial or changed fallback plots cannot masquerade as final evidence."""
    root = completed_campaign
    bind_method_figures(root)
    path = root / "analysis/method-figures/manifest.json"
    manifest = json.loads(path.read_text())
    if defect == "provisional":
        manifest["status"] = "provisional"
    elif defect == "analysis_binding":
        manifest["analysis_manifest_sha256"] = "different-analysis"
    else:
        with (path.parent / "common-method-solar-errors.png").open("ab") as stream:
            stream.write(b"changed")
    write_json(path, manifest)
    with pytest.raises(ValueError, match="method figures|Method figure"):
        R.publish(root)
    assert not (root / "synthesis.html").exists()
    assert not (root / "analysis/hierarchy-digest.csv").exists()


def refresh_qualification_files(state):
    """Rebind isolated publisher evidence after intentional fixture mutations."""
    root = state["root"]
    analysis = root / "analysis"
    manifest = json.loads((analysis / "manifest.json").read_text())
    metadata = state["metadata"]
    references = state["annotated"].loc[
        state["annotated"].method.eq("reference")
        & state["annotated"].engine_outcome_qualified
    ]
    qualification_path = analysis / "diagnostics-qualification.json"
    write_json(qualification_path, metadata)
    state["excluded"].to_csv(analysis / "diagnostic-exclusions.csv", index=False)
    for name in (
        "diagnostics-qualification.json",
        "diagnostic-exclusions.csv",
        "deduplicated-comparisons.csv",
        "common-method-comparisons.csv",
        "hierarchy-pairs.csv",
    ):
        manifest["outputs"][name] = R.sha((analysis / name).read_bytes())
    manifest.update(
        {
            "diagnostics_qualification_sha256": manifest["outputs"][
                "diagnostics-qualification.json"
            ],
            "diagnostics_qualification_policy": metadata["policy"],
            "qualified_building_rows": metadata["qualified_building_rows"],
            "qualified_reference_building_rows": len(references),
            "qualified_reference_site_years": len(
                references[["site_id", "year"]].drop_duplicates()
            ),
            "excluded_building_rows": metadata["excluded_building_rows"],
            "deduplicated_comparisons": len(
                pd.read_csv(analysis / "deduplicated-comparisons.csv")
            ),
            "common_method_comparisons": len(
                pd.read_csv(analysis / "common-method-comparisons.csv")
            ),
            "hierarchy_pairs": len(pd.read_csv(analysis / "hierarchy-pairs.csv")),
        }
    )
    manifest["inputs"].update(metadata["inputs"])
    write_json(analysis / "manifest.json", manifest)
    bind_figures(root)


@pytest.fixture
def screened_campaign(completed_campaign, monkeypatch):
    """Mock only Q's boundary; publisher still verifies bytes, rows and identities.

    Real Core qualification belongs to the helper's evidence tests. This fixture
    supplies eight clean comparison rows, two clean references, and two labels
    for one warned run. The caller must rebind every changed publication byte.
    """
    from examples import qualify_weather_campaign_results as Q

    root = completed_campaign
    analysis = root / "analysis"
    comparisons = pd.read_csv(analysis / "deduplicated-comparisons.csv")
    comparisons["case_id"] = [
        f"{row.physical_signature}-{row.method}"
        for row in comparisons.itertuples(index=False)
    ]
    comparisons["simulation_path"] = [
        str(
            root
            / "datasets"
            / row.site_id
            / str(row.year)
            / "simulations"
            / row.building_id
            / row.case_id
        )
        for row in comparisons.itertuples(index=False)
    ]
    comparisons["engine_run_qualified"] = True
    comparisons["engine_outcome_qualified"] = True
    references = pd.DataFrame(
        [
            {
                "site_id": site,
                "year": 2023,
                "building_id": "profile",
                "case_id": "reference",
                "method": "reference",
                "simulation_path": str(
                    root / f"datasets/{site}/2023/simulations/profile/reference"
                ),
                "engine_run_qualified": True,
                "engine_outcome_qualified": True,
            }
            for site in ("a", "b")
        ]
    )
    warnings = pd.DataFrame(
        [
            {
                "site_id": "a",
                "year": 2023,
                "building_id": "profile",
                "case_id": case,
                "method": "era5" if case == "warned" else "automatic",
                "simulation_path": str(
                    root / "datasets/a/2023/simulations/profile/warned"
                ),
                "engine_run_qualified": False,
                "engine_outcome_qualified": False,
                "engine_exclusion_reasons": '["engine_diagnostic_warning_or_error"]',
            }
            for case in ("warned", "warned_alias")
        ]
    )
    annotated = pd.concat([references, comparisons, warnings], ignore_index=True)
    for name in ("deduplicated-comparisons.csv", "common-method-comparisons.csv"):
        comparisons.to_csv(analysis / name, index=False)
    pairs = pd.read_csv(analysis / "hierarchy-pairs.csv")
    for side, method in (("new", "automatic"), ("old", "automatic_legacy")):
        pairs["case_id_" + side] = pairs.physical_signature + "-" + method
    pairs.to_csv(analysis / "hierarchy-pairs.csv", index=False)
    manifest = json.loads((analysis / "manifest.json").read_text())
    manifest["captured_building_rows"] = len(annotated)
    proof_path = root / "execution-environment/qualification-fixture.json"
    write_json(proof_path, {"fixture": "isolated publisher boundary", "warnings": 1})
    inputs = {str(proof_path): R.sha(proof_path.read_bytes())}
    for site, rows in annotated.groupby("site_id"):
        path = root / f"datasets/{site}/2023/summary.csv"
        rows.to_csv(path, index=False)
        inputs[str(path)] = R.sha(path.read_bytes())
    manifest["inputs"].update(inputs)
    write_json(analysis / "manifest.json", manifest)
    coverage_path = root / "coverage.json"
    coverage = json.loads(coverage_path.read_text())
    coverage["comparison_rows"] = len(annotated)
    write_json(coverage_path, coverage)
    for site, count in (("a", 5), ("b", 7)):
        path = root / f"datasets/{site}/2023/audit.json"
        audit = json.loads(path.read_text())
        audit.update(
            {
                "comparisons": count,
                "unique_simulations_accumulated": 4 if site == "a" else 7,
                "warnings": 2 if site == "a" else 0,
            }
        )
        write_json(path, audit)
    records = [
        {
            "canonical_simulation_path": path,
            "qualified": bool(rows.engine_run_qualified.iloc[0]),
            "raw_warning_count": 2 if path.endswith("/warned") else 0,
            "core_warning_count": 1 if path.endswith("/warned") else 0,
            "warning_exception_scopes": (
                ["annual_space_heating_demand"] if path.endswith("/warned") else []
            ),
        }
        for path, rows in annotated.groupby("simulation_path")
    ]
    metadata = {
        "policy": Q.POLICY,
        "evidence_mode": "strict_immutable_files",
        "qualifier_script_sha256": R.sha(b"isolated qualification fixture"),
        "raw_building_rows": 12,
        "qualified_building_rows": 10,
        "excluded_building_rows": 2,
        "unique_simulations": 11,
        "qualified_simulations": 10,
        "excluded_simulations": 1,
        "exclusion_reason_counts": {"engine_diagnostic_warning_or_error": 2},
        "records": records,
        "inputs": inputs,
    }
    state = {
        "root": root,
        "annotated": annotated,
        "excluded": annotated.loc[~annotated.engine_outcome_qualified].copy(),
        "metadata": metadata,
        "proof_path": proof_path,
    }

    def isolated_qualifier(actual_root, raw_rows, inputs=None):
        assert actual_root == root
        identity = ["site_id", "year", "building_id", "case_id"]
        assert set(
            map(tuple, raw_rows[identity].itertuples(index=False, name=None))
        ) == set(
            map(tuple, state["annotated"][identity].itertuples(index=False, name=None))
        )
        return (
            state["annotated"].copy(deep=True),
            state["excluded"].copy(deep=True),
            deepcopy(state["metadata"]),
        )

    monkeypatch.setattr(Q, "qualify_results", isolated_qualifier)
    refresh_qualification_files(state)
    return state


def test_screened_warnings_publish_distinct_raw_run_and_outcome_counts(
    screened_campaign,
):
    """One warned run and its alias remain archived while inference is clean."""
    root = screened_campaign["root"]
    raw_paths = list((root / "datasets").glob("*/*/summary.csv"))
    raw_paths += list((root / "datasets").glob("*/*/audit.json"))
    before = {path: path.read_bytes() for path in raw_paths}
    receipt = R.publish(root)
    assert receipt["unique_annual_simulations"] == 11
    assert receipt["diagnostics_qualification_sha256"] == R.sha(
        (root / "analysis/diagnostics-qualification.json").read_bytes()
    )
    page = (root / "synthesis.html").read_text()
    for text in (
        "raw catalogue contains 12 building responses",
        "10 qualify for building-outcome analysis",
        "2 are excluded",
        "1 canonical simulations have unqualified diagnostics",
        "Building outcomes retained for inference have zero EnergyPlus warnings",
        "Physically valid weather and anomaly comparisons remain available",
        "analysis/diagnostic-exclusions.csv",
        "analysis/diagnostics-qualification.json",
    ):
        assert text in page
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize(
    "defect",
    [
        "qualification_bytes",
        "manifest_qualification_sha",
        "policy",
        "evidence_mode",
        "qualifier_script_sha256",
        "raw_building_rows",
        "qualified_building_rows",
        "excluded_building_rows",
        "unique_simulations",
        "qualified_simulations",
        "excluded_simulations",
        "exclusion_reason_counts",
        "record_identity",
        "warning_exception_scope",
        "duplicate_record",
        "evidence_sha",
        "extra_evidence",
        "native_evidence_changed",
        "manifest_qualified_count",
        "manifest_excluded_count",
    ],
)
def test_report_rechecks_qualification_instead_of_trusting_saved_flags(
    screened_campaign, defect
):
    """Even coherently rebound tables must agree with independent qualification."""
    root = screened_campaign["root"]
    analysis = root / "analysis"
    path = analysis / "diagnostics-qualification.json"
    saved = json.loads(path.read_text())
    if defect == "qualification_bytes":
        with path.open("ab") as stream:
            stream.write(b"\n")
    elif defect.startswith("manifest_"):
        manifest_path = analysis / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if defect == "manifest_qualification_sha":
            manifest["diagnostics_qualification_sha256"] = "different-receipt"
        else:
            key = (
                "qualified_building_rows"
                if defect == "manifest_qualified_count"
                else "excluded_building_rows"
            )
            manifest[key] += 1
        write_json(manifest_path, manifest)
    elif defect == "native_evidence_changed":
        with screened_campaign["proof_path"].open("ab") as stream:
            stream.write(b"\nchanged immutable diagnostics")
    else:
        if defect in ("policy", "evidence_mode", "qualifier_script_sha256"):
            saved[defect] = "unbound-policy-or-code"
        elif defect.endswith(("rows", "simulations")):
            saved[defect] += 1
        elif defect == "exclusion_reason_counts":
            saved[defect] = {"reference_engine_unqualified": 2}
        elif defect == "record_identity":
            saved["records"][0]["canonical_simulation_path"] += "/different-run"
        elif defect == "warning_exception_scope":
            warned = next(
                record for record in saved["records"] if not record["qualified"]
            )
            warned["warning_exception_scopes"] = ["annual_cooling_demand"]
        elif defect == "duplicate_record":
            saved["records"].append(deepcopy(saved["records"][0]))
        elif defect == "evidence_sha":
            saved["inputs"][str(screened_campaign["proof_path"])] = "changed"
        else:
            saved["inputs"][str(root / "fabricated-evidence.json")] = "invented"
        write_json(path, saved)
        manifest_path = analysis / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["outputs"][path.name] = R.sha(path.read_bytes())
        manifest["diagnostics_qualification_sha256"] = R.sha(path.read_bytes())
        write_json(manifest_path, manifest)
    with pytest.raises(ValueError):
        R.publish(root)
    assert not (root / "synthesis.html").exists()
    assert not (analysis / "hierarchy-digest.csv").exists()


@pytest.mark.parametrize(
    "catalogue", ["diagnostic-exclusions", "comparisons", "common", "pairs"]
)
def test_excluded_alias_cannot_leak_into_any_published_building_cohort(
    screened_campaign, catalogue
):
    """Membership uses raw outcome eligibility, including paired component IDs."""
    root = screened_campaign["root"]
    analysis = root / "analysis"
    if catalogue == "diagnostic-exclusions":
        path = analysis / "diagnostic-exclusions.csv"
        rows = pd.read_csv(path)
        rows.loc[0, "case_id"] = "mask-0-automatic"
    elif catalogue == "pairs":
        path = analysis / "hierarchy-pairs.csv"
        rows = pd.read_csv(path)
        rows.loc[0, "case_id_new"] = "warned_alias"
    else:
        name = (
            "deduplicated-comparisons.csv"
            if catalogue == "comparisons"
            else "common-method-comparisons.csv"
        )
        path = analysis / name
        rows = pd.read_csv(path)
        leaked = rows.iloc[[0]].copy()
        leaked["case_id"] = "warned_alias"
        leaked["engine_outcome_qualified"] = True
        rows = pd.concat([rows, leaked], ignore_index=True)
    rows.to_csv(path, index=False)
    manifest_path = analysis / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"][path.name] = R.sha(path.read_bytes())
    if catalogue == "comparisons":
        manifest["deduplicated_comparisons"] = len(rows)
    elif catalogue == "common":
        manifest["common_method_comparisons"] = len(rows)
    write_json(manifest_path, manifest)
    with pytest.raises(ValueError):
        R.publish(root)
    assert not (root / "synthesis.html").exists()
    assert not (analysis / "hierarchy-digest.csv").exists()


def test_report_explains_baseline_cascade_without_miscounting_clean_runs(
    screened_campaign,
):
    """A warned baseline excludes clean dependent outcomes but not their archives."""
    state = screened_campaign
    root = state["root"]
    analysis = root / "analysis"
    baseline = state["annotated"].site_id.eq("a") & state["annotated"].method.eq(
        "reference"
    )
    state["annotated"].loc[baseline, "engine_run_qualified"] = False
    state["annotated"].loc[
        state["annotated"].site_id.eq("a"), "engine_outcome_qualified"
    ] = False
    state["excluded"] = (
        state["annotated"].loc[~state["annotated"].engine_outcome_qualified].copy()
    )
    metadata = state["metadata"]
    metadata.update(
        {
            "qualified_building_rows": 7,
            "excluded_building_rows": 5,
            "qualified_simulations": 9,
            "excluded_simulations": 2,
            "exclusion_reason_counts": {
                "engine_diagnostic_warning_or_error": 3,
                "reference_engine_unqualified": 4,
            },
        }
    )
    for record in metadata["records"]:
        if record["canonical_simulation_path"].endswith(
            "/a/2023/simulations/profile/reference"
        ):
            record["qualified"] = False
            record["raw_warning_count"] = 2
            record["core_warning_count"] = 1
            record["warning_exception_scopes"] = ["annual_space_heating_demand"]
    audit_path = root / "datasets/a/2023/audit.json"
    audit = json.loads(audit_path.read_text())
    audit["warnings"] = 4
    write_json(audit_path, audit)
    for name in (
        "deduplicated-comparisons.csv",
        "common-method-comparisons.csv",
        "hierarchy-pairs.csv",
    ):
        path = analysis / name
        rows = pd.read_csv(path)
        rows.loc[rows.site_id.eq("b")].to_csv(path, index=False)
    refresh_qualification_files(state)
    R.publish(root)
    page = (root / "synthesis.html").read_text()
    assert "7 qualify for building-outcome analysis" in page
    assert "5 are excluded" in page
    assert "2 canonical simulations have unqualified diagnostics" in page
    assert "A flagged reference also excludes related comparisons" in page
    assert "2/108 raw evaluated reference building combinations" in page
    assert (
        "1 warning-free reference building combinations across 1 location-years" in page
    )


@pytest.mark.parametrize(
    "field", ["qualified_reference_building_rows", "qualified_reference_site_years"]
)
def test_report_rejects_forged_qualified_reference_counts(screened_campaign, field):
    """Retained reference coverage must match independently qualified immutable rows."""
    root = screened_campaign["root"]
    path = root / "analysis/manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] += 1
    write_json(path, manifest)
    bind_figures(root)
    with pytest.raises(
        ValueError, match="Qualified reference building and site-year counts disagree"
    ):
        R.publish(root)
    assert not (root / "synthesis.html").exists()
