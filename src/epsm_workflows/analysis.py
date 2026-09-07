"""Versioned training features, group-safe diagnostics, and actual-data QA."""

import json

import numpy as np
import pandas as pd

from epsm_core import BuildingSpec

FEATURE_VERSION = "building-demand"
FEATURES = (
    "footprint_area_m2",
    "perimeter_m",
    "conditioned_floor_area_m2",
    "conditioned_volume_m3",
    "floors",
    "floor_height_m",
    "orientation_sin",
    "orientation_cos",
    "wall_u",
    "top_u",
    "ground_u",
    "window_u",
    "shgc",
    "shading_factor",
    "thermal_bridge_uplift",
    "infiltration_ach",
    "natural_ach",
    "mechanical_l_s_m2",
    "sensible_recovery",
    "heating_setpoint_c",
    "occupants_per_m2",
    "equipment_w_m2",
    "dhw_kwh_m2_year",
    "latitude_deg",
    "longitude_deg",
    "roof",
    "topology",
    "shape",
    "north_window_m2",
    "east_window_m2",
    "south_window_m2",
    "west_window_m2",
)
TARGETS = (
    "space_heating_demand_eui_kwh_m2",
    "dhw_load_eui_kwh_m2",
    "ideal_thermal_load_eui_kwh_m2",
)


def frame(store):
    rows = []
    for rid, specj, metaj, reportj, scalarj, diagj in store.db.execute(
        "SELECT run_id,spec_json,metadata_json,report_json,scalar_json,diagnostics_json FROM runs WHERE status='complete' ORDER BY ordinal"
    ).fetchall():
        b = BuildingSpec.model_validate_json(specj)
        m = json.loads(metaj)
        r = json.loads(json.loads(reportj)["report_json"])
        g, a, e = b.geometry, b.airflow, b.envelope
        occupied = [p for p in g.parts if p.conditioned]
        base_area = sum(p.footprint.area_m2 for p in occupied)
        floor_area = sum(p.footprint.area_m2 * p.floors for p in occupied)
        volume = sum(p.footprint.area_m2 * p.storey_elevations_m[-1] for p in occupied)
        roof_topologies = {g.roof_for(p).topology for p in g.parts}
        row = dict(
            run_id=rid,
            **m,
            **json.loads(scalarj),
            **json.loads(diagj),
            footprint_area_m2=g.footprint_area_m2,
            perimeter_m=r["perimeter_m"],
            conditioned_floor_area_m2=g.conditioned_floor_area_m2,
            conditioned_volume_m3=g.conditioned_volume_m3,
            # Footprint-weighted storey count and floor-area-weighted storey height.
            floors=floor_area / base_area if base_area else 0,
            floor_height_m=volume / floor_area if floor_area else 0,
            orientation_sin=np.sin(np.radians(g.orientation_deg)),
            orientation_cos=np.cos(np.radians(g.orientation_deg)),
            wall_u=e.wall.u_value_w_m2k,
            top_u=e.top.u_value_w_m2k,
            ground_u=e.ground.u_value_w_m2k,
            window_u=b.facade.glazing.u_value_w_m2k,
            shgc=b.facade.glazing.solar_heat_gain_coefficient,
            shading_factor=b.facade.solar_shading_factor,
            thermal_bridge_uplift=e.thermal_bridge_uplift_fraction,
            infiltration_ach=a.envelope_infiltration_ach,
            natural_ach=a.natural_exchange_ach,
            mechanical_l_s_m2=a.mechanical_vent_l_s_m2,
            sensible_recovery=a.sensible_recovery_effectiveness,
            heating_setpoint_c=np.mean(b.use.heating_setpoint.values),
            occupants_per_m2=b.use.occupant_density_per_m2,
            equipment_w_m2=b.use.equipment_gain_w_m2,
            dhw_kwh_m2_year=b.use.dhw_kwh_m2_year,
            latitude_deg=b.climate.latitude_deg,
            longitude_deg=b.climate.longitude_deg,
            roof=next(iter(roof_topologies)) if len(roof_topologies) == 1 else "mixed",
            topology=a.topology,
            shape=m["requested"].get("shape", "rectangle"),
            renovation_condition=m["requested"].get("renovation_condition", "anchor"),
        )
        for cardinal in ("north", "east", "south", "west"):
            row[cardinal + "_window_m2"] = sum(
                f["window_area_m2"] for f in r["facades"] if f["cardinal"] == cardinal
            )
        rows.append(row)
    return pd.DataFrame(rows)


def training_data(store, target="space_heating_demand_eui_kwh_m2"):
    if target not in TARGETS:
        raise ValueError("Target is outside the approved target set")
    df = frame(store)
    # DHW demand is a direct target formula, so remove it from predictors for DHW/total models.
    features = tuple(
        f
        for f in FEATURES
        if not (target != "space_heating_demand_eui_kwh_m2" and f == "dhw_kwh_m2_year")
    )
    return dict(
        feature_version=FEATURE_VERSION,
        X=df.loc[:, features],
        y=df[target],
        groups=df.group_id,
        city=df.city,
        metadata=df[
            ["shape", "roof", "topology", "renovation_condition", "conditioned_floor_area_m2"]
        ],
    )


def baseline(store):
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import ExtraTreesRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import GroupShuffleSplit
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import OneHotEncoder

    data = training_data(store)
    X, y = data["X"], data["y"]

    def evaluate(train, test):
        categorical = list(X.select_dtypes(include="object").columns)
        numeric = [c for c in X.columns if c not in categorical]
        model = make_pipeline(
            ColumnTransformer(
                [
                    ("cat", OneHotEncoder(handle_unknown="ignore"), categorical),
                    ("num", SimpleImputer(), numeric),
                ]
            ),
            ExtraTreesRegressor(n_estimators=120, min_samples_leaf=3, random_state=42, n_jobs=4),
        )
        model.fit(X.iloc[train], y.iloc[train])
        pred = model.predict(X.iloc[test])

        def metrics(actual, pred):
            return dict(
                n=len(actual),
                mae=float(mean_absolute_error(actual, pred)),
                rmse=float(np.sqrt(mean_squared_error(actual, pred))),
                r2=float(r2_score(actual, pred)) if len(actual) > 1 else None,
            )

        overall = metrics(y.iloc[test], pred)
        details = {}
        metadata = data["metadata"].iloc[test].copy()
        metadata["size"] = pd.cut(
            metadata.conditioned_floor_area_m2,
            [0, 2000, 8000, float("inf")],
            labels=["small", "medium", "large"],
        )
        for col in ("shape", "roof", "topology", "renovation_condition", "size"):
            details[col] = {}
            for key in metadata[col].unique():
                mask = (metadata[col] == key).to_numpy()
                if mask.any():
                    details[col][str(key)] = metrics(y.iloc[test].to_numpy()[mask], pred[mask])
        return dict(overall=overall, breakdowns=details)

    train, test = next(
        GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42).split(X, y, data["groups"])
    )
    results = dict(
        feature_version=FEATURE_VERSION, group_holdout=evaluate(train, test), leave_one_city_out={}
    )
    for city in data["city"].unique():
        test = np.flatnonzero((data["city"] == city).to_numpy())
        # Cross-city anchor groups are removed from training for geographic transfer too.
        held_groups = set(data["groups"].iloc[test])
        train = np.flatnonzero((~data["groups"].isin(held_groups)).to_numpy())
        results["leave_one_city_out"][city] = evaluate(train, test)
    return results


def qa(store, legacy_csv=None):
    df = frame(store)
    report = dict(storage=store.validate(), completed=len(df))
    if df.empty:
        return report
    report["counts"] = {
        c: {str(k): int(v) for k, v in df[c].value_counts().items()}
        for c in ("city", "shape", "roof", "topology", "renovation_condition", "comparison_type")
    }
    report["realized_bounds"] = {
        c: dict(min=float(df[c].min()), max=float(df[c].max()))
        for c in FEATURES
        if pd.api.types.is_numeric_dtype(df[c])
    }
    report["targets"] = {c: df[c].describe().to_dict() for c in TARGETS}
    report["warnings"] = int(df.warning_count.sum())
    report["severe"] = int(df.severe_count.sum())
    report["categorical_balance_by_city"] = {
        str(city): {
            column: {str(k): int(v) for k, v in local[column].value_counts().items()}
            for column in ("roof", "topology", "shape", "comparison_type")
        }
        for city, local in df[df.comparison_type != "legacy_anchor"].groupby("city")
    }
    pair_errors = []
    complete_pairs = 0
    for pair, local in df[df.pair_id.notna()].groupby("pair_id"):
        if len(local) != 2:
            continue
        complete_pairs += 1
        for column in (
            "conditioned_floor_area_m2",
            "conditioned_volume_m3",
            "wall_u",
            "top_u",
            "ground_u",
            "infiltration_ach",
            "natural_ach",
            "mechanical_l_s_m2",
            "sensible_recovery",
        ):
            if not np.isclose(local[column].iloc[0], local[column].iloc[1], rtol=1e-9):
                pair_errors.append(f"{pair}:{column}")
    report["pair_invariants"] = dict(complete_pairs=complete_pairs, errors=pair_errors)
    if legacy_csv:
        old = pd.read_csv(legacy_csv)
        anchors = df[df.legacy_lhs_sample.notna()]
        joined = anchors.merge(
            old,
            on=None,
            left_on=["legacy_lhs_sample", "city"],
            right_on=["lhs_sample", "loc_city"],
            suffixes=("_v2", "_v1"),
        )
        # Zone demand and ideal supply-air demand have different ventilation accounting; label this explicitly.
        report["anchor_deltas"] = dict(
            n=len(joined),
            comparison="v1 zone heating versus v2 ideal supply heating; includes accounting and physics corrections",
            mean_heating_delta_kwh=float(
                (joined.space_heating_demand_kwh - joined.heating_kwh).mean()
            ),
        )
    return report
