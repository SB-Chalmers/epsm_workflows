"""Plan paired, reproducible outages without changing production weather recovery.

The input must contain original accepted observations, including their native
missing cells, rather than a previously completed reference. UTC timestamps are
hour endings; the study calendar uses fixed UTC+1 and interval-start labels.
Random panels contain separate seasonal blocks. Weather extremes are separate
stress panels, never selected from a building's simulated demand. Repeated
panels share a site/year and are not independent climate replications.

Solar input completeness does not make modeled DNI/direct-horizontal partitions
independent observations: their provenance remains the source receipt's concern.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta, timezone

import numpy as np
import pandas as pd

MET_COLUMNS = (
    "dry_bulb",
    "relative_humidity",
    "wind_speed",
    "wind_direction",
    "pressure",
)
SOLAR_COLUMNS = ("ghi", "dni", "dirh")
SEASONS = ("DJF", "MAM", "JJA", "SON")
DEFAULT_METHODS = ("temporal", "donor_raw", "donor_assessed", "era5", "automatic")
_METHODS = (*DEFAULT_METHODS, "automatic_legacy", "erbs")
FIXED_LST = timezone(timedelta(hours=1))
_SCOPES = {
    "meteorology": MET_COLUMNS,
    "solar": SOLAR_COLUMNS,
    "all": MET_COLUMNS + SOLAR_COLUMNS,
    **{column: (column,) for column in MET_COLUMNS + SOLAR_COLUMNS},
}


@dataclass(frozen=True)
class OutageBlock:
    """Describe one block by inclusive selected hour-ending UTC timestamps."""

    season: str
    start_utc: pd.Timestamp
    end_utc: pd.Timestamp
    hours: int
    anchor_name: str | None = None
    anchor_utc: pd.Timestamp | None = None

    def to_record(self):
        """Return a JSON-safe description of one withheld block."""
        return {
            "season": self.season,
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "hours": self.hours,
            "anchor_name": self.anchor_name,
            "anchor_utc": self.anchor_utc.isoformat()
            if self.anchor_utc is not None
            else None,
        }


@dataclass(frozen=True)
class OutagePanel:
    """Pair methods and buildings through the same withheld original evidence."""

    site_id: str
    year: int
    scope: str
    sampling: str
    seed: int | None
    duration_hours: int
    columns: tuple[str, ...]
    mask: pd.Series
    blocks: tuple[OutageBlock, ...]
    evidence_id: str
    status: str
    reason: str | None
    original_cells_sha256: str | None
    anchor_name: str | None = None
    anchor_utc: pd.Timestamp | None = None

    def to_record(self):
        """Serialize evidence and unsupported decisions without nonfinite JSON values."""
        return {
            "site_id": self.site_id,
            "year": self.year,
            "scope": self.scope,
            "sampling": self.sampling,
            "seed": self.seed,
            "duration_hours": self.duration_hours,
            "columns": list(self.columns),
            "status": self.status,
            "reason": self.reason,
            "evidence_id": self.evidence_id,
            "original_cells_sha256": self.original_cells_sha256,
            "masked_hours": int(self.mask.sum()),
            "masked_original_cells": int(self.mask.sum()) * len(self.columns),
            "masked_utc": [
                stamp.isoformat()
                for stamp in self.mask.index[self.mask].tz_convert("UTC")
            ],
            "blocks": [block.to_record() for block in self.blocks],
            "anchor_name": self.anchor_name,
            "anchor_utc": self.anchor_utc.isoformat()
            if self.anchor_utc is not None
            else None,
            "solar_truth_note": (
                "Original solar inputs may include modeled direct-component partitions."
                if set(self.columns) & set(SOLAR_COLUMNS)
                else None
            ),
        }

    def withheld(self, originals):
        """Remove every selected truth cell before fitting or donor assessment.

        Pass this original frame to fitting. Completed reference values must not
        enter training; algorithms retain responsibility for their held-out folds
        and adjacent-hour exclusions. Unsupported panels cannot be consumed.
        """
        if self.status != "ready":
            raise ValueError(f"Unsupported outage panel: {self.reason}")
        if not originals.index.equals(self.mask.index):
            raise ValueError("Originals and outage mask must have identical timestamps")
        if (
            _truth_hash(originals, self.mask, self.columns)
            != self.original_cells_sha256
        ):
            raise ValueError(
                "Withheld original evidence changed after panel generation"
            )
        frame = originals.copy(deep=True)
        frame.loc[self.mask, list(self.columns)] = np.nan
        return frame


def _digest(value):
    """Hash canonical JSON without platform-dependent Python hash randomization."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _truth_hash(originals, mask, columns):
    """Fingerprint selected original numbers in a fixed byte order."""
    values = originals.loc[mask, list(columns)].apply(pd.to_numeric, errors="coerce")
    digest = hashlib.sha256()
    digest.update(
        _digest(
            {
                "columns": columns,
                "utc": [s.isoformat() for s in values.index.tz_convert("UTC")],
            }
        ).encode()
    )
    digest.update(values.to_numpy(dtype="<f8").tobytes())
    return digest.hexdigest()


def _positive_integer(value, name, *, zero=False):
    """Reject ambiguous or fractional count configuration before selecting evidence."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, np.integer))
        or value < (0 if zero else 1)
    ):
        raise ValueError(
            f"{name} must be {'nonnegative' if zero else 'positive'} integers"
        )
    return int(value)


def _unique(values, name):
    """Prevent duplicate configurations from masquerading as repeated independent evidence."""
    values = tuple(values)
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def _calendar(originals, year):
    """Validate the complete study year and derive fixed local seasons."""
    index = originals.index
    if (
        not isinstance(index, pd.DatetimeIndex)
        or index.tz is None
        or index.hasnans
        or not len(index)
    ):
        raise ValueError(
            "Originals require a nonempty timezone-aware hourly DatetimeIndex"
        )
    utc = index.tz_convert("UTC")
    if not utc.equals(utc.floor("h")) or np.any(
        (utc[1:] - utc[:-1]) != pd.Timedelta(hours=1)
    ):
        raise ValueError(
            "Originals require ordered, unique, consecutive hourly timestamps"
        )
    labels = utc.tz_convert(FIXED_LST) - pd.Timedelta(hours=1)
    annual = labels.year == year
    expected = pd.date_range(
        pd.Timestamp(year=year, month=1, day=1, tz=FIXED_LST) + pd.Timedelta(hours=1),
        pd.Timestamp(year=year + 1, month=1, day=1, tz=FIXED_LST),
        freq="h",
    ).tz_convert("UTC")
    if not utc[annual].equals(expected):
        raise ValueError(
            "Originals must cover the complete fixed UTC+1 local study year"
        )
    months = labels.month.to_numpy()
    seasons = np.asarray(SEASONS, dtype=object)[(months % 12) // 3]
    return utc, annual, seasons


def _starts(complete, allowed, hours):
    """Find wholly observed windows with observed neighbors at both endpoints."""
    if hours > len(complete) - 2:
        return np.empty(0, dtype=int)
    starts = np.arange(1, len(complete) - hours)
    invalid = np.r_[0, np.cumsum(~(complete & allowed))]
    valid = invalid[starts + hours] == invalid[starts]
    valid &= complete[starts - 1] & complete[starts + hours]
    return starts[valid]


def _separated(starts, mask, hours):
    """Keep windows separated from previous blocks by an unmasked hour."""
    masked = np.r_[0, np.cumsum(mask)]
    return starts[masked[starts + hours + 1] == masked[starts - 1]]


def _sample_blocks(starts, hours, count, rng):
    """Randomly choose feasible ordered blocks without greedy packing failures."""
    selected = []
    for remaining in range(count, 0, -1):
        if not len(starts):
            return None
        successors = np.searchsorted(starts, starts + hours + 1)
        capacity = np.zeros(len(starts) + 1, dtype=int)
        for i in range(len(starts) - 1, -1, -1):
            capacity[i] = 1 + capacity[successors[i]]
        feasible = starts[capacity[:-1] >= remaining]
        if not len(feasible):
            return None
        start = int(rng.choice(feasible))
        selected.append(start)
        starts = starts[starts >= start + hours + 1]
    return selected


def generate_panels(
    originals,
    site_id,
    year,
    scopes=("meteorology",),
    durations=(3, 12, 48, 168),
    seeds=(11, 29, 47),
    blocks_per_season=1,
    anchors=None,
):
    """Generate paired seasonal repetitions and separately anchored weather stress cases.

    Each random panel contains ``blocks_per_season`` blocks in each of DJF,
    MAM, JJA and SON, with one observed hour separating blocks. Each extreme
    anchor produces its own panel and its selected window must contain that
    exact hour. Extreme windows may span seasonal boundaries. Missing original
    evidence yields an explicit unsupported panel,
    never a shifted event elsewhere. Buffered source hours may supply endpoint
    context but are never withheld outside the requested fixed local year.

    Scopes accept meteorology, solar, all, or a single input column. Seeds and
    weather-derived aware anchor timestamps are independent of buildings and
    recovery methods. Their configurations must not contain duplicates.
    """
    if not isinstance(site_id, str) or not site_id.strip():
        raise ValueError("site_id must be a nonempty stable location identifier")
    year = _positive_integer(year, "year")
    if year >= 9999:
        raise ValueError("year must leave room for the next calendar boundary")
    scopes = _unique(scopes, "scopes")
    if any(scope not in _SCOPES for scope in scopes):
        raise ValueError(f"Unknown outage scope; choose from {tuple(_SCOPES)}")
    durations = _unique(
        (_positive_integer(x, "durations") for x in durations), "durations"
    )
    seeds = _unique((_positive_integer(x, "seeds", zero=True) for x in seeds), "seeds")
    blocks_per_season = _positive_integer(blocks_per_season, "blocks_per_season")
    if anchors is not None and not isinstance(anchors, Mapping):
        raise ValueError("anchors must map weather event names to aware timestamps")
    anchor_items = []
    for name, value in (anchors or {}).items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Weather anchors require nonempty stable event names")
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("Weather anchor timestamps must be timezone-aware")
        anchor_items.append((name, stamp.tz_convert("UTC")))
    utc, annual, seasons = _calendar(originals, year)
    panels = []
    for scope in scopes:
        columns = _SCOPES[scope]
        missing = set(columns) - set(originals.columns)
        if missing:
            complete = np.zeros(len(originals), dtype=bool)
        else:
            values = originals[list(columns)].apply(pd.to_numeric, errors="coerce")
            complete = np.isfinite(values.to_numpy(dtype=float)).all(axis=1)
        for hours in durations:
            for sampling, seed, anchor_name, anchor in (
                *[("random_seasonal", seed, None, None) for seed in seeds],
                *[
                    ("weather_extremes", None, name, stamp)
                    for name, stamp in sorted(anchor_items)
                ],
            ):
                config = {
                    "site_id": site_id,
                    "year": year,
                    "scope": scope,
                    "sampling": sampling,
                    "seed": seed,
                    "duration_hours": hours,
                    "blocks_per_season": blocks_per_season
                    if sampling == "random_seasonal"
                    else None,
                    "anchor_name": anchor_name,
                    "anchor_utc": anchor.isoformat() if anchor is not None else None,
                }
                selected = np.zeros(len(originals), dtype=bool)
                blocks = []
                reason = (
                    f"Missing original columns: {', '.join(sorted(missing))}"
                    if missing
                    else None
                )
                if reason is None and sampling == "random_seasonal":
                    rng = np.random.default_rng(int(_digest(config)[:16], 16))
                    for season in SEASONS:
                        starts = _separated(
                            _starts(complete, annual & (seasons == season), hours),
                            selected,
                            hours,
                        )
                        chosen = _sample_blocks(starts, hours, blocks_per_season, rng)
                        if chosen is None:
                            reason = f"No {blocks_per_season} separated originally complete {hours}-hour blocks in {season}"
                            break
                        for start in chosen:
                            selected[start : start + hours] = True
                            blocks.append(
                                OutageBlock(
                                    season, utc[start], utc[start + hours - 1], hours
                                )
                            )
                elif reason is None:
                    position = utc.get_indexer([anchor])[0]
                    if position < 0 or not annual[position]:
                        reason = f"Weather anchor {anchor_name} is absent from the requested local year"
                    else:
                        starts = _starts(complete, annual, hours)
                        starts = starts[
                            (starts <= position) & (starts + hours > position)
                        ]
                        if not len(starts):
                            reason = f"No originally complete {hours}-hour window containing weather anchor {anchor_name}"
                        else:
                            preferred = position - hours // 2
                            start = int(
                                min(
                                    starts,
                                    key=lambda s: (abs(int(s) - preferred), int(s)),
                                )
                            )
                            selected[start : start + hours] = True
                            blocks.append(
                                OutageBlock(
                                    str(seasons[position]),
                                    utc[start],
                                    utc[start + hours - 1],
                                    hours,
                                    anchor_name,
                                    anchor,
                                )
                            )
                if reason is not None:
                    selected[:] = False
                    blocks.clear()
                mask = pd.Series(
                    selected, index=originals.index, name="withheld", dtype=bool
                )
                truth_hash = (
                    _truth_hash(originals, mask, columns) if reason is None else None
                )
                blocks.sort(key=lambda block: block.start_utc)
                identity = config | {
                    "blocks": [b.to_record() for b in blocks],
                    "original_cells_sha256": truth_hash,
                    "reason": reason,
                }
                panels.append(
                    OutagePanel(
                        site_id,
                        year,
                        scope,
                        sampling,
                        seed,
                        hours,
                        columns,
                        mask,
                        tuple(blocks),
                        "outage-" + _digest(identity)[:24],
                        "ready" if reason is None else "unsupported",
                        reason,
                        truth_hash,
                        anchor_name,
                        anchor,
                    )
                )
    return panels


def method_support(panel, method):
    """Expose strategy eligibility before scheduling a paired weather comparison."""
    if method not in _METHODS:
        raise ValueError(f"Unknown study recovery method: {method}")
    if panel.status != "ready":
        return False, panel.reason
    if method == "temporal" and panel.duration_hours > 48:
        return False, "Bounded temporal recovery supports outages through 48 hours"
    if method in ("donor_raw", "donor_assessed") and set(panel.columns) & set(
        SOLAR_COLUMNS
    ):
        return False, "Meteorological donors cannot reconstruct solar inputs"
    if method == "erbs":
        return (
            False,
            "Erbs retains GHI and needs a separately defined direct-component mask",
        )
    return True, None


def estimate_counts(panels, methods=DEFAULT_METHODS, building_variants=1):
    """Count weather comparisons and paired simulations without unsupported methods.

    References are counted once per unique site/year/building, including a site
    whose outage panels are unsupported. Duplicate evidence IDs are rejected.
    Counts are planning estimates: they do
    not imply donor acceptance, successful downloads, or independent samples.
    ``building_variants`` accepts a positive count or unique variant names.
    """
    panels = tuple(panels)
    _unique((panel.evidence_id for panel in panels), "panel evidence identifiers")
    methods = _unique(methods, "methods")
    if any(method not in _METHODS for method in methods):
        raise ValueError("Unknown study recovery method in campaign plan")
    if isinstance(building_variants, Iterable) and not isinstance(
        building_variants, (str, bytes)
    ):
        variants = _unique(building_variants, "building_variants")
        buildings = _positive_integer(len(variants), "building_variants")
    else:
        buildings = _positive_integer(building_variants, "building_variants")
    ready = sum(panel.status == "ready" for panel in panels)
    cases = sum(
        method_support(panel, method)[0] for panel in panels for method in methods
    )
    references = len({(panel.site_id, panel.year) for panel in panels}) * buildings
    return {
        "panels_total": len(panels),
        "panels_ready": ready,
        "panels_unsupported": len(panels) - ready,
        "weather_cases": cases,
        "method_combinations_unsupported": len(panels) * len(methods) - cases,
        "building_variants": buildings,
        "reference_simulations": references,
        "scenario_simulations": cases * buildings,
        "total_simulations": references + cases * buildings,
    }
