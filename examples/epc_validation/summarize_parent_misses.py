"""Describe completed held-out misses without revising the fitted archetype.

References: docs/cohort-transfer.md, property-separated parent-first protocol.
Groups use the frozen observed descriptors. They are descriptive diagnostics,
not validated sub-archetypes or evidence of causal mechanisms. Failed simulations
remain in every relevant coverage denominator. No targets enter assignment.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def summarize(campaign: Path, destination: Path) -> dict:
    """Retain heterogeneous errors, domain fallbacks and fit-boundary warnings."""
    rows = json.loads((campaign / "predictions.json").read_text())
    descriptors = {
        r["building_id"]: r
        for r in json.loads((campaign / "features.json").read_text())
    }
    groups = {}
    for field in (
        "heated_basement",
        "roof_form",
        "building_position",
        "renovation_timing",
        "completed_window_replacement",
        "designed_flow_l_s_m2",
        "assignment",
    ):
        values = sorted(
            {str(r.get(field, descriptors[r["building_id"]].get(field))) for r in rows}
        )
        groups[field] = {}
        for value in values:
            subset = [
                r
                for r in rows
                if str(r.get(field, descriptors[r["building_id"]].get(field))) == value
            ]
            valid = [r for r in subset if r["deployed_prediction"] is not None]
            groups[field][value] = {
                "buildings": len(subset),
                "properties": len({r["property_key"] for r in subset}),
                "failed": len(subset) - len(valid),
                "below20": sum(
                    abs(r["deployed_prediction"] / r["target"] - 1) < 0.2 for r in valid
                ),
                "below10": sum(
                    abs(r["deployed_prediction"] / r["target"] - 1) < 0.1 for r in valid
                ),
                "mean_signed_error_percent": sum(
                    100 * (r["deployed_prediction"] / r["target"] - 1) for r in valid
                )
                / len(valid)
                if valid
                else None,
                "validated_sub_archetype": False,
            }
    boundaries = []
    for file in sorted((campaign / "folds").glob("*/outer*/frozen.json")):
        frozen = json.loads(file.read_text())
        for label, fits in frozen["fits"].items():
            register = json.loads(
                (file.parent / f"cluster{label}/sensitivity.json").read_text()
            )["register"]
            for method in ("single", "shared"):
                parameters = fits[method]["parameters"]
                hits = {
                    name: value
                    for name, value in parameters.items()
                    if any(abs(value - end) < 1e-9 for end in register[name]["bounds"])
                }
                boundaries.append(
                    {
                        "fold": file.parent.name,
                        "cluster": label,
                        "method": method,
                        "parameters": parameters,
                        "bound_hits": hits,
                    }
                )
    result = {
        "campaign": str(campaign.resolve()),
        "descriptive_only": True,
        "outer_residuals_used_to_refit": False,
        "groups": groups,
        "fold_fit_bounds": boundaries,
        "selected_method_counts": dict(Counter(r["selected_method"] for r in rows)),
        "failure_buildings": [
            r["building_id"] for r in rows if r["deployed_prediction"] is None
        ],
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "miss-diagnostics.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n"
    )
    lines = [
        "# Held-out miss diagnostics",
        "",
        "Descriptive strata of the completed fixed-data experiment. These comparisons do not establish transferable sub-archetypes or causal explanations.",
        "",
        "| Descriptor | Value | Buildings | Properties | Below 20% | Below 10% | Failed |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for field, strata in groups.items():
        for value, score in strata.items():
            lines.append(
                f"| {field} | {value} | {score['buildings']} | {score['properties']} | {score['below20']} | {score['below10']} | {score['failed']} |"
            )
    lines.extend(
        [
            "",
            "## Fit boundaries",
            "",
            "A bound hit identifies a constrained effective assumption. It does not justify widening the bound after viewing held-out errors or changing the construction matrix.",
            "",
        ]
    )
    for fit in boundaries:
        lines.append(
            f"- {fit['fold']}, {fit['method']}: {fit['parameters']}; boundary hits: {fit['bound_hits']}."
        )
    (destination / "miss-diagnostics.md").write_text("\n".join(lines) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    summarize(args.campaign, args.destination)
