"""Render per-building native predictions and attempted-building accuracy coverage.

Reference: docs/cohort-transfer.md. Failed simulations remain in the empirical
coverage denominator. Plots never supply assignment features or fitting inputs.
"""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def render(campaign, output, *, baseline=False):
    """Plot a completed baseline check or completed retrospective transfer run."""
    campaign, output = Path(campaign), Path(output)
    if baseline:
        from epsm_workflows.cohort_transfer.preparation import TargetStore

        rows = json.loads((campaign / "features.json").read_text())
        observations = TargetStore(campaign).evaluation(
            rows, campaign / "baseline-frozen.json"
        )
        predictions = json.loads(
            (campaign / "baseline-predictions-before-targets.json").read_text()
        )
        data = [
            row | {"deployed_prediction": prediction["prediction"]}
            for row, prediction in zip(observations, predictions, strict=True)
        ]
        title = "Unfitted v1.4 baseline"
    else:
        data = json.loads((campaign / "predictions.json").read_text())
        title = "Retrospective property-CV transfer"
    valid = [r for r in data if r["deployed_prediction"] is not None]
    observed = np.array([r["target"] for r in valid])
    predicted = np.array([r["deployed_prediction"] for r in valid])
    ape = np.sort(np.abs(predicted / observed - 1) * 100)
    coverage = np.arange(1, len(valid) + 1) / len(data) * 100
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.3), constrained_layout=True)
    limit = max(observed.max(), predicted.max()) * 1.05
    axes[0].fill_between(
        [0, limit], [0, 0.8 * limit], [0, 1.2 * limit], color="#d9e8f4", label="±20%"
    )
    axes[0].plot([0, limit], [0, limit], color="#555", linewidth=1)
    axes[0].scatter(observed, predicted, s=18, alpha=0.7, color="#1c638f")
    axes[0].set(
        xlabel="Observed delivered space heat (kWh/m²/year)",
        ylabel="Core prediction (kWh/m²/year)",
        xlim=(0, limit),
        ylim=(0, limit),
    )
    axes[0].legend(frameon=False)
    axes[1].step(ape, coverage, where="post", color="#1c638f", linewidth=2)
    for threshold in (10, 20):
        actual = (ape < threshold).sum() / len(data) * 100
        axes[1].axvline(threshold, color="#888", linewidth=0.8, linestyle="--")
        axes[1].scatter([threshold], [actual], color="#b04724", zorder=4)
        axes[1].annotate(
            f"<{threshold}%: {actual:.1f}%",
            (threshold, actual),
            xytext=(8, 8),
            textcoords="offset points",
        )
    axes[1].set(
        xlabel="Absolute building error (%)",
        ylabel="Attempted buildings below error (%)",
        xlim=(0, max(60, min(180, ape.max() + 5))),
        ylim=(0, 100),
    )
    axes[1].grid(alpha=0.2)
    fig.suptitle(
        f"{title} · {len(data)} buildings · {len(data) - len(valid)} failed",
        fontsize=12,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180)
    plt.close(fig)
    return str(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    print(render(args.campaign, args.output, baseline=args.baseline))


if __name__ == "__main__":
    main()
