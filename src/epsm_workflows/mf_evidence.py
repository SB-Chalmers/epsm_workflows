"""Apply quantitative MF building evidence without inferring renovation performance.

Sources: docs/mf-revision.md; EPSM Data component resolver. EPC completed-work
checkboxes identify work, not a U-value, material depth or operating flow.
"""

from __future__ import annotations

import math


def insulation_candidates(construction):
    """Conservative candidates for reviewed EPSM MF stacks, not material discovery.

    Low conductivity alone also selects timber and lightweight structural layers.
    The reviewed MF insulating layers are low-density and below 0.1 W/(m K).
    Unrecognised stacks require assembly review rather than automatic scaling.
    """
    return [
        i
        for i, layer in enumerate(construction.layers)
        if layer.material.conductivity_w_mk <= 0.1
        and layer.material.density_kg_m3 <= 200
    ]


def _number(value, name, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError(f"Invalid {name}")
    return float(value)


def reviewed_evidence(row):
    """Validate independent component/operation evidence dated before the period.

    Evidence completed during a measurement year is not a full-year assignment.
    Component state sources are still accepted by the existing resolver API;
    this stricter interface is for new quantitative observations.
    """
    evidence = row.get("mf_evidence", {})
    if not isinstance(evidence, dict):
        raise ValueError("mf_evidence must be an object")
    allowed = {"window", "wall", "roof", "operating_flow"}
    if set(evidence) - allowed:
        raise ValueError(f"Unsupported MF evidence: {set(evidence) - allowed}")
    result = {}
    for role, item in evidence.items():
        if not isinstance(item, dict) or not str(item.get("source", "")).strip():
            raise ValueError(f"Independent source required for {role}")
        if item.get("classification") not in {"measured", "source_derived"}:
            raise ValueError("Quantitative evidence must be measured or source_derived")
        start, end = item.get("valid_from_year"), item.get("valid_through_year")
        year = row["measurement_year"]
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end)):
            raise ValueError("Evidence requires integer validity years")
        if not start < year <= end:
            raise ValueError("Evidence must cover the entire measurement year")
        if role == "window":
            if item.get("definition") != "whole_window_including_frame":
                raise ValueError("Whole-window definition including frame required")
            _number(item.get("u_value_w_m2k"), "window U")
            for key in ("solar_heat_gain_coefficient", "visible_transmittance"):
                if (
                    key in item
                    and not 0 <= _number(item[key], key, positive=False) <= 1
                ):
                    raise ValueError(f"Invalid {key}")
        elif role == "operating_flow":
            if (
                item.get("area_basis") != "Atemp"
                or item.get("quantity") != "operating_outdoor_air"
            ):
                raise ValueError(
                    "Operating flow requires Atemp and outdoor-air definition"
                )
            _number(item.get("l_s_m2"), "operating flow", positive=False)
            if row.get("ventilation") == "S":
                raise ValueError("Natural ventilation requires its own exchange model")
        else:
            if item.get("boundary") != "nominal_assembly_including_surface_films":
                raise ValueError("Opaque U requires a nominal assembly boundary")
            _number(item.get("u_value_w_m2k"), f"{role} U")
        result[role] = dict(item)
    return result


def apply_quantitative_evidence(spec, row):
    """Return a new spec and audit; preserve unrelated geometry and component inputs.

    Opaque U observations constrain equivalent insulation thickness at unchanged
    material properties. They do not establish the building's exact layer stack.
    """
    from epsm_core import GlazingSpec

    evidence = reviewed_evidence(row)
    audit = []
    changed = spec
    for role, item in evidence.items():
        if role == "window":
            before = changed.facade.glazing.model_dump(mode="json")
            values = before | {k: v for k, v in item.items() if k in before}
            changed = changed.updated(
                facade=changed.facade.updated(glazing=GlazingSpec(**values))
            )
            after = changed.facade.glazing.model_dump(mode="json")
        elif role == "operating_flow":
            before = changed.airflow.mechanical_vent_l_s_m2
            after = item["l_s_m2"]
            changed = changed.updated(
                airflow=changed.airflow.updated(mechanical_vent_l_s_m2=after)
            )
        else:
            field = "wall" if role == "wall" else "top"
            construction = getattr(changed.envelope, field)
            before = construction.u_value_w_m2k
            layers = list(construction.layers)
            choices = insulation_candidates(construction)
            if not choices:
                raise ValueError(
                    "No insulation layer for evidence-constrained opaque U"
                )
            index = max(
                choices,
                key=lambda i: layers[i].thickness_m
                / layers[i].material.conductivity_w_mk,
            )
            old = layers[index]
            other_r = 1 / before - old.thickness_m / old.material.conductivity_w_mk
            depth = (
                1 / item["u_value_w_m2k"] - other_r
            ) * old.material.conductivity_w_mk
            if depth <= 0:
                raise ValueError(
                    "Observed U incompatible with retained non-insulation layers"
                )
            layers[index] = old.updated(thickness_m=depth)
            replacement = construction.updated(layers=tuple(layers))
            changed = changed.updated(
                envelope=changed.envelope.updated(**{field: replacement})
            )
            after = replacement.u_value_w_m2k
        audit.append(
            {
                "role": role,
                "before": before,
                "after": after,
                "evidence": item,
                "exact_layer_stack_observed": False
                if role in {"wall", "roof"}
                else None,
            }
        )
    return changed, audit


def evidence_locks(row):
    controls = {
        "window": ("window_u_multiplier", "shgc_multiplier"),
        "wall": ("wall_resistance",),
        "roof": ("roof_resistance",),
        "operating_flow": ("flow_multiplier",),
    }
    return {name for role in reviewed_evidence(row) for name in controls[role]}
