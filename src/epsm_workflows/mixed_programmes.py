"""Resolve fixed EPC heated-use shares into explicit native EPSM programme mixtures.

Boverket heated-use shares define programme area, while Lantmäteriet GIS purposes
lead building-type assignment; see archetype_membership.py for primary references.
This adapter never uses measured energy, estimates absent shares, or supplies an
unavailable programme from a neighbouring use. It does not assign spatial zones.
"""

from __future__ import annotations

import math

from .archetype_membership import SHARES, numeric


def program_blend_from_epc(row, programmes, *, source):
    """Build a ProgramBlend from existing shares and source-backed native profiles.

    ``programmes`` maps programme names (``residential``, ``office``, ``school``,
    etc.) to ``(UseSpec, AirflowSpec, source_string)`` tuples. Every positive EPC
    share requires an exact profile. Call ``resolve(calendar_year=...)`` to check
    calendar/system compatibility, or use separate ZoneUseSpec assignments for
    known spatial uses. The source string must document the effective-zone and
    reference-area assumptions. Link conflicts require review before application.
    """
    from epsm_core import ProgramBlend, ProgramComponent

    if row.get("semantic_compatibility") == "incompatible":
        raise ValueError(
            "Review incompatible EPC/GIS link before programme application"
        )
    count = numeric(row.get("certificate_to_footprint_count"))
    if count is not None and count != 1:
        raise ValueError("Programme shares must identify one footprint")
    garage = numeric(row.get("garage_percent"))
    if garage is not None and garage > 0:
        raise ValueError("Heated garage requires a separate documented programme")
    shares = {}
    for field, raw in SHARES.items():
        value = row.get(field, row.get(raw))
        if isinstance(value, float) and math.isnan(value):
            continue  # Parquet null; positive reported shares must still total 100%.
        share = numeric(value)
        if value is not None and str(value).strip() and share is None:
            raise ValueError(f"Invalid programme share: {field}")
        if share is not None and not 0 <= share <= 100:
            raise ValueError(f"Programme share outside [0,100]: {field}")
        if share and share > 0:
            shares[field.removesuffix("_percent")] = share
    if not math.isclose(sum(shares.values()), 100, rel_tol=0, abs_tol=1e-7):
        raise ValueError(
            "Reported heated-programme shares must sum to 100%; no normalisation"
        )
    missing = set(shares) - set(programmes)
    if missing:
        raise ValueError(f"No sourced programme for positive shares: {sorted(missing)}")
    components = []
    for name, share in shares.items():
        use, airflow, profile_source = programmes[name]
        components.append(
            ProgramComponent(
                name=name,
                area_fraction=share / 100,
                use=use,
                airflow=airflow,
                source=profile_source,
            )
        )
    return ProgramBlend(components=tuple(components), source=source)
