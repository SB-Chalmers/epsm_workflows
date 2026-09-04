"""Deterministic optimized LHS with constrained permutations and matched variants."""

import importlib.metadata
import math
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.stats.qmc import LatinHypercube

from epsm_core import (
    AirflowSpec,
    BuildingSpec,
    FacadeOverride,
    FacadeSpec,
    Footprint,
    GeometrySpec,
    GlazingSpec,
    ProvenanceSpec,
    ScheduleSpec,
    SourceRecord,
    UseSpec,
)
from epsm_core.compiler import COMPILER_VERSION
from epsm_core.weather import climate_from_epw, file_sha256

from .fabric import fabric, fabric_from_u

ANCHOR_PARAMS = [
    ("wall_mw", 0.025, 0.220),
    ("roof_mw", 0.030, 0.400),
    ("window_u", 1.5, 2.6),
    ("floor_eps", 0.010, 0.130),
    ("infiltration_ach", 0.15, 1),
    ("vent_recovery_eff", 0, 0.8),
    ("t_heating", 19.5, 21),
    ("num_floors", 2, 8),
    ("wwr_north", 0.06, 0.29),
    ("wwr_south", 0.08, 0.33),
    ("wwr_east", 0.07, 0.32),
    ("wwr_west", 0.08, 0.34),
]


@dataclass(frozen=True)
class Case:
    spec: BuildingSpec
    city: str
    group_id: str
    pair_id: str | None
    variant: str
    comparison_type: str
    requested: dict
    legacy_lhs_sample: int | None = None

    def metadata(self):
        return dict(
            city=self.city,
            group_id=self.group_id,
            pair_id=self.pair_id,
            variant=self.variant,
            comparison_type=self.comparison_type,
            requested=self.requested,
            legacy_lhs_sample=self.legacy_lhs_sample,
        )


def shape(family, area, aspect, cut_l, cut_w):
    outer = area / (1 - cut_l * cut_w) if family != "rectangle" else area
    length, width = math.sqrt(outer * aspect), math.sqrt(outer / aspect)
    if width < 8:
        raise ValueError("Outer width below 8 m")
    if family == "rectangle":
        return Footprint.rectangle(length, width)
    cl, cw = length * cut_l, width * cut_w
    if family == "L":
        if cut_l * cut_w > 0.3 or min(length - cl, width - cw) < 4:
            raise ValueError("Invalid L cut")
        return Footprint.l_shape(length, width, cl, cw)
    else:
        if (length - cl) / 2 < 4 or width - cw < 6:
            raise ValueError("Invalid U wings/base")
        return Footprint.u_shape(length, width, cl, cw)


def facade(footprint, wwrs, window_u, shade):
    # Axis values travel with local edges; orientation is applied only by the compiler.
    from epsm_core.geometry import cardinal, facade_edges

    overrides = tuple(
        FacadeOverride(facade_id=fid, wwr=wwrs[cardinal(a, b)])
        for fid, a, b in facade_edges(footprint.polygon, 0)
    )
    return FacadeSpec(
        default_wwr=sum(wwrs.values()) / 4,
        overrides=overrides,
        glazing=GlazingSpec(u_value_w_m2k=window_u, solar_heat_gain_coefficient=0.7),
        solar_shading_factor=shade,
    )


def _balanced(values, n, rng):
    out = np.resize(np.array(values, dtype=object), n)
    rng.shuffle(out)
    return out


def _wwrs(row, bounds):
    lo, hi = bounds["mean_wwr"]
    mean = lo + (hi - lo) * row[17]
    lo, hi = bounds["wwr_contrasts"]
    axis, fb, lr = lo + (hi - lo) * row[18:21]
    return dict(
        north=mean + axis + fb, south=mean + axis - fb, east=mean - axis + lr, west=mean - axis - lr
    )


def _constrain_wwr(unit, rng, bounds):
    # Exchange values within columns; every original LHS stratum stays represented.
    def penalty(row):
        w = np.array(list(_wwrs(row, bounds).values()))
        return float(np.maximum(0.06 - w, 0).sum() + np.maximum(w - 0.34, 0).sum())

    scores = np.array([penalty(row) for row in unit])
    for _ in range(100000):
        bad = np.flatnonzero(scores > 1e-12)
        if not len(bad):
            return
        i = int(rng.choice(bad))
        j = int(rng.integers(len(unit)))
        col = int(rng.integers(17, 21))
        before = scores[i] + scores[j]
        unit[i, col], unit[j, col] = unit[j, col], unit[i, col]
        si, sj = penalty(unit[i]), penalty(unit[j])
        if si + sj <= before:
            scores[i], scores[j] = si, sj
        else:
            unit[i, col], unit[j, col] = unit[j, col], unit[i, col]
    raise ValueError("Cannot preserve LHS glazing strata under WWR constraints")


def _new(city, seed, city_index, bounds):
    def scale(key, unit, log=False):
        lo, hi = bounds[key]
        return lo * (hi / lo) ** unit if log else lo + (hi - lo) * unit

    rng = np.random.default_rng(seed + city_index)
    unit = LatinHypercube(d=25, seed=seed + city_index, optimization="random-cd").random(950)
    _constrain_wwr(unit, rng, bounds)
    comparisons = np.array(["shape"] * 380 + ["orientation"] * 285 + ["massing"] * 285)
    rng.shuffle(comparisons)
    floor_lo, floor_hi = bounds["floors"]
    floors = _balanced(range(floor_lo, floor_hi + 1), 950, rng).astype(int)
    roofs = _balanced(["compact", "cold_attic"], 950, rng)
    systems = _balanced(["S", "F", "FT", "FTX"], 950, rng)
    offsets = iter(_balanced([30, 60, 90], 285, rng))
    combos = [("rectangle", "L")] * 127 + [("rectangle", "U")] * 127 + [("L", "U")] * 126
    rng.shuffle(combos)
    combos = iter(combos)
    families = _balanced(["rectangle", "L", "U"], 950, rng)
    specs = []
    # Compute valid area intervals, then assign the sampled areas without changing their strata.
    for i, row in enumerate(unit):
        kind = comparisons[i]
        f = int(floors[i])
        fs = next(combos) if kind == "shape" else (families[i], families[i])
        alt = (f - 2 if f >= floor_lo + 2 else f + 2) if kind == "massing" else f
        aspect = scale("outer_aspect_ratio", row[2], True)
        cl = 0.2 + 0.35 * row[4]
        # L and U share a cut fraction restricted to their common interval for shape pairs.
        cw = (
            0.2 + 0.25 * row[5]
            if kind == "shape"
            else (
                0.2 + (min(0.55, 0.3 / cl) - 0.2) * row[5] if fs[0] == "L" else 0.15 + 0.30 * row[5]
            )
        )
        # Assign floor counts later when a sampled area has no feasible footprint for this pair.
        specs.append(
            dict(
                kind=kind,
                f=f,
                alt=alt,
                families=fs,
                aspect=aspect,
                cl=cl,
                cw=cw,
                offset=int(next(offsets)) if kind == "orientation" else 0,
            )
        )
    areas = scale("conditioned_floor_area_m2", unit[:, 0], True)

    def minimum_area(family, item):
        aspect, cl, cw = item["aspect"], item["cl"], item["cw"]
        outer = 64 * aspect
        if family == "U":
            outer = max(outer, 64 / ((1 - cl) ** 2 * aspect), 36 * aspect / (1 - cw) ** 2)
        if family == "L":
            outer = max(outer, 16 / ((1 - cl) ** 2 * aspect), 16 * aspect / (1 - cw) ** 2)
        return max(
            bounds["footprint_area_m2"][0], outer * (1 - cl * cw if family != "rectangle" else 1)
        )

    # Balanced floor labels can be permuted to satisfy geometry without changing any LHS column.
    for attempt in range(200):
        costs = np.full((950, 950), 1e9)
        for i, item in enumerate(specs):
            f = int(floors[i])
            alt = (f - 2 if f >= floor_lo + 2 else f + 2) if item["kind"] == "massing" else f
            item["f"], item["alt"] = f, alt
            low = max(minimum_area(fam, item) * fl for fam, fl in zip(item["families"], (f, alt)))
            high = bounds["footprint_area_m2"][1] * min(f, alt)
            feasible = (areas >= low) & (areas <= high)
            costs[i, feasible] = np.abs(i - np.arange(950)[feasible]) / 950
        ii, jj = linear_sum_assignment(costs)
        if not (costs[ii, jj] >= 1e9).any():
            break
        rng.shuffle(floors)
    else:
        raise ValueError("Geometry constraints cannot preserve LHS area strata; revise design")
    unit[:, 0] = unit[jj, 0]
    for d in range(25):
        assert len(set(np.floor(unit[:, d] * 950).astype(int))) == 950
    climate = climate_from_epw(city["epw"])
    cases = []
    for i, (row, item) in enumerate(zip(unit, specs)):
        atemp = scale("conditioned_floor_area_m2", row[0], True)
        height = scale("floor_height_m", row[1])
        orientation = scale("orientation_deg", row[3])
        scores = np.clip(row[6] + (-0.2 + 0.4 * row[7:11]), 0, 1)
        wall, top, ground, window = [
            scale(key, 1 - score)
            for key, score in zip(
                ("wall_u_nominal", "top_envelope_u_nominal", "ground_floor_u_nominal", "window_u"),
                scores,
            )
        ]
        envelope = fabric_from_u(
            wall, top, ground, roofs[i], scale("thermal_bridge_fraction", row[11])
        )
        system = systems[i]
        airflow = AirflowSpec(
            topology=system,
            envelope_infiltration_ach=scale("envelope_infiltration_ach", row[21]),
            natural_exchange_ach=scale("natural_exchange_ach_S", row[22]) if system == "S" else 0,
            mechanical_vent_l_s_m2=scale("mechanical_vent_l_s_m2", row[23]) if system != "S" else 0,
            sensible_recovery_effectiveness=scale("hrv_sensible_efficiency_FTX", row[24])
            if system == "FTX"
            else 0,
        )
        use = UseSpec(
            heating_setpoint=ScheduleSpec.constant(scale("heating_setpoint_c", row[13])),
            occupant_density_per_m2=scale("occupant_density_per_m2", row[14]),
            equipment_gain_w_m2=scale("equipment_gain_w_m2", row[15]),
            dhw_kwh_m2_year=scale("dhw_kwh_m2_year", row[16]),
        )
        pair = f"{city['slug']}_{i:04}"
        requested = dict(
            lhs_unit=row.tolist(),
            fabric_condition=float(row[6]),
            renovation_condition=("original", "partial", "deep")[min(2, int(row[6] * 3))],
            **item,
        )
        for v, fl, family in zip(("a", "b"), (item["f"], item["alt"]), item["families"]):
            fp = shape(family, atemp / fl, item["aspect"], item["cl"], item["cw"])
            geometry = GeometrySpec(
                footprints=(fp,),
                floors=fl,
                floor_height_m=height,
                orientation_deg=(orientation + (item["offset"] if v == "b" else 0)) % 360,
            )
            b = BuildingSpec(
                identifier=f"{pair}_{v}",
                geometry=geometry,
                climate=climate,
                envelope=envelope,
                airflow=airflow,
                use=use,
                facade=facade(
                    fp, _wwrs(row, bounds), window, scale("solar_shading_factor", row[12])
                ),
                provenance=ProvenanceSpec(
                    records=(
                        SourceRecord(
                            field_path="*",
                            status="assumed",
                            source="swedish_mfh_1961_1975_v2",
                            assumption_profile="campaign_v2",
                        ),
                    )
                ),
            )
            cases.append(
                Case(b, city["city"], pair, pair, v, item["kind"], {**requested, "shape": family})
            )
    return cases


def generate(config):
    if config["n_total"] != 10000 or config["n_per_city"] != 2000 or len(config["cities"]) != 5:
        raise ValueError("Campaign must have five cities and exactly 10000 rows")
    cases = []
    unit = LatinHypercube(d=12, seed=42).random(100)
    for si, row in enumerate(unit):
        raw = {
            name: float(lo + row[i] * (hi - lo)) for i, (name, lo, hi) in enumerate(ANCHOR_PARAMS)
        }
        raw["num_floors"] = int(round(raw["num_floors"]))
        for city in config["cities"]:
            fp = Footprint.rectangle(50, 25)
            b = BuildingSpec(
                identifier=f"anchor_{si:04}_{city['slug']}",
                geometry=GeometrySpec(
                    footprints=(fp,), floors=raw["num_floors"], floor_height_m=2.7
                ),
                climate=climate_from_epw(city["epw"]),
                envelope=fabric(raw["wall_mw"], raw["roof_mw"], raw["floor_eps"]),
                facade=facade(
                    fp,
                    {c: raw["wwr_" + c] for c in ("north", "south", "east", "west")},
                    raw["window_u"],
                    0.5,
                ),
                airflow=AirflowSpec(
                    topology="FTX",
                    envelope_infiltration_ach=raw["infiltration_ach"],
                    mechanical_vent_l_s_m2=0.35,
                    sensible_recovery_effectiveness=raw["vent_recovery_eff"],
                ),
                use=UseSpec(
                    heating_setpoint=ScheduleSpec.constant(raw["t_heating"]),
                    occupant_density_per_m2=0.017,
                    equipment_gain_w_m2=1.68,
                    dhw_kwh_m2_year=21,
                ),
                provenance=ProvenanceSpec(
                    records=(
                        SourceRecord(
                            field_path="*",
                            status="assumed",
                            source="seed42_original_lhs",
                            assumption_profile="corrected_anchor_v2",
                        ),
                    )
                ),
            )
            cases.append(
                Case(b, city["city"], f"anchor_{si:04}", None, "anchor", "legacy_anchor", raw, si)
            )
    for i, city in enumerate(config["cities"]):
        cases.extend(_new(city, config["seed"], i, config["bounds"]))
    validate_pairs(cases)
    # The first planned 200 exercise new geometry and physics across all cities.
    pilot = []
    for city in config["cities"]:
        local = [c for c in cases if c.city == city["city"]]
        pilot.extend([c for c in local if c.legacy_lhs_sample is not None][:2])
        pilot.extend([c for c in local if c.pair_id is not None][:38])
    ids = {c.spec.identifier for c in pilot}
    return pilot + [c for c in cases if c.spec.identifier not in ids]


def validate_pairs(cases):
    from collections import Counter, defaultdict

    if len(cases) != 10000 or set(Counter(c.city for c in cases).values()) != {2000}:
        raise ValueError("Incorrect campaign allocation")
    pairs = defaultdict(list)
    for c in cases:
        if c.pair_id:
            pairs[c.pair_id].append(c)
    for pair, rows in pairs.items():
        if len(rows) != 2:
            raise ValueError(f"Incomplete pair {pair}")
        a, b = [r.spec for r in rows]
        if a.envelope != b.envelope or a.airflow != b.airflow or a.use != b.use:
            raise ValueError(f"Pair physics differs: {pair}")
        if not math.isclose(
            a.geometry.conditioned_floor_area_m2, b.geometry.conditioned_floor_area_m2, rel_tol=1e-9
        ):
            raise ValueError(f"Pair area differs: {pair}")
        if not math.isclose(
            a.geometry.conditioned_volume_m3, b.geometry.conditioned_volume_m3, rel_tol=1e-9
        ):
            raise ValueError(f"Pair volume differs: {pair}")
        if rows[0].comparison_type == "massing" and abs(a.geometry.floors - b.geometry.floors) < 2:
            raise ValueError(f"Massing floor count: {pair}")


def manifest(config, compiler):
    def commit(path):
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        return r.stdout.strip() or "uncommitted"

    import epsm_core

    core = Path(epsm_core.__file__).parent
    workflow = Path(__file__).parent
    sources = {
        root.name + "/" + str(p.relative_to(root)): file_sha256(p)
        for root in (core, workflow)
        for p in sorted(root.rglob("*"))
        if p.suffix in (".py", ".json")
    }
    return dict(
        schema_version=2,
        config=config,
        source_hashes=sources,
        compiler_version=COMPILER_VERSION,
        core_git_commit=commit(core),
        workflows_git_commit=commit(workflow),
        python=platform.python_version(),
        packages={
            p: importlib.metadata.version(p)
            for p in (
                "epsm_core",
                "epsm_workflows",
                "numpy",
                "scipy",
                "pydantic",
                "shapely",
                "jsonschema",
                "duckdb",
                "pyarrow",
                "pandas",
                "scikit-learn",
            )
        },
        energyplus_version=compiler.version,
        engine_sha256=compiler.engine_hash,
        schema_sha256=compiler.schema_hash,
        weather={c["city"]: file_sha256(c["epw"]) for c in config["cities"]},
    )
