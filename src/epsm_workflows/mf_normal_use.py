"""Fixed household-gain reference for the MF physical comparison.

BEN Table 2:2 and chapter 3 section 5 use 30 kWh/m² Atemp of household
electricity and a 70% thermally available share. The reference below changes
only that annual useful gain; Sveby residential 2.0 supplies its timing proxy.
It does not establish that an individual certificate was normalised for this
quantity, nor change any measured heating target.
"""

from __future__ import annotations

import math
from functools import lru_cache

SVEBY_HOUSEHOLD_BASIS = "sveby_residential_2_0"
BEN_HOUSEHOLD_BASIS = "ben_2016_household_annual_with_sveby2_timing"
BEN_HOUSEHOLD_ELECTRICITY_KWH_M2_YEAR = 30.0
HOUSEHOLD_USEFUL_HEAT_FRACTION = 0.7
BEN_HOUSEHOLD_USEFUL_GAIN_KWH_M2_YEAR = 21.0
SVEBY_HOUSEHOLD_USEFUL_GAIN_KWH_M2_YEAR = 14.7
BEN_SOURCE = (
    "https://www.boverket.se/resources/constitutiontextstore/ben/PDF/"
    "konsoliderad_ben_bfs_2016_12.pdf"
)
SVEBY_SOURCE = (
    "https://www.sveby.org/wp-content/uploads/2024/11/"
    "Sveby-brukarindata-bostadshus-2.0-241111.pdf"
)


def household_reference_provenance():
    """Return JSON-safe source and limitation declarations for export."""
    return {
        "normal_use_basis": BEN_HOUSEHOLD_BASIS,
        "source": BEN_SOURCE,
        "source_locator": "Table 2:2; chapter 3 section 5 (BFS 2017:6)",
        "household_electricity_kwh_m2_year": BEN_HOUSEHOLD_ELECTRICITY_KWH_M2_YEAR,
        "thermally_available_fraction": HOUSEHOLD_USEFUL_HEAT_FRACTION,
        "useful_household_gain_kwh_m2_year": BEN_HOUSEHOLD_USEFUL_GAIN_KWH_M2_YEAR,
        "gain_field_basis": "Useful zone heat; do not apply another 70% factor",
        "timing_proxy": "Unchanged Sveby residential 2.0 monthly or legacy daily schedule",
        "timing_source": SVEBY_SOURCE,
        "timing_source_locator": "Table 9.1; bundled version 2.0, 2024-11-11",
        "current_catalogue_basis": SVEBY_HOUSEHOLD_BASIS,
        "current_catalogue_household_electricity_kwh_m2_year": 21.0,
        "current_catalogue_useful_gain_kwh_m2_year": SVEBY_HOUSEHOLD_USEFUL_GAIN_KWH_M2_YEAR,
        "legacy_daily_rounding": "Catalogue 1.68 W/m² is restored exactly on inverse conversion",
        "calendar_energy_conservation": "BEN useful gain integrates to 21 in common and leap years",
        "area_basis": "Existing load_reference_area_m2 / Atemp is preserved",
        "spatial_allocation": "Existing Core dwelling/common/basement load allocation is preserved",
        "individual_target_normalization_verified": False,
        "measured_target_changed": False,
        "selection_basis": "Fixed sourced normal-use reference before fitting; no heating-target input",
    }


@lru_cache(maxsize=256)
def _sveby_use(calendar_year):
    from epsm_core import load_sveby_spec

    return load_sveby_spec().use_spec(calendar_year=calendar_year)


def _reference_powers(spec, source_basis):
    if source_basis not in (SVEBY_HOUSEHOLD_BASIS, BEN_HOUSEHOLD_BASIS):
        raise ValueError(f"Unknown household-gain reference basis: {source_basis}")
    use = spec.use
    if use.occupancy_category != "residential":
        raise ValueError("Household-gain reference requires residential use")
    if use.equipment_heat_fraction != 1 or use.equipment_heat_fraction_schedule is not None:
        raise ValueError("Household-gain field must already represent useful heat")
    schedule = use.equipment_fraction
    year = spec.climate.calendar_year
    # Annual schedules conserve the separately declared 14.7 kWh/m² total.
    # The old daily mapping is rounded to 1.68 W/m²; retain its exact inverse.
    original = _sveby_use(year if schedule.annual_values is not None else None)
    if schedule != original.equipment_fraction:
        raise ValueError("Household timing must match the declared Sveby residential 2.0 proxy")
    hours = math.fsum(schedule.hourly(year))
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("Household timing requires a positive annual integral")
    sveby_power = original.equipment_gain_w_m2
    if schedule.annual_values is not None:
        useful = sveby_power * hours / 1000
        if not math.isclose(useful, SVEBY_HOUSEHOLD_USEFUL_GAIN_KWH_M2_YEAR, rel_tol=1e-10):
            raise ValueError("Bundled Sveby household useful gain changed; review the fixed reference")
    ben_power = BEN_HOUSEHOLD_USEFUL_GAIN_KWH_M2_YEAR * 1000 / hours
    expected = sveby_power if source_basis == SVEBY_HOUSEHOLD_BASIS else ben_power
    if not math.isclose(use.equipment_gain_w_m2, expected, rel_tol=1e-10, abs_tol=1e-12):
        raise ValueError(f"Household gain contradicts declared reference basis: {source_basis}")
    return sveby_power, ben_power


def ben_household_reference(spec, *, source_basis=SVEBY_HOUSEHOLD_BASIS):
    """Apply the fixed BEN useful annual gain while retaining all other inputs.

    A BEN input must explicitly declare ``source_basis=BEN_HOUSEHOLD_BASIS``.
    Both paths validate the starting scalar and exact source timing, preventing
    implicit double scaling. No observed energy enters this operation.
    """
    _, ben_power = _reference_powers(spec, source_basis)
    if source_basis == BEN_HOUSEHOLD_BASIS:
        return spec
    return spec.updated(use=spec.use.updated(equipment_gain_w_m2=ben_power))


def current_sveby_reference(spec, *, source_basis=BEN_HOUSEHOLD_BASIS):
    """Restore the authoritative current catalogue gain for a current baseline.

    Inverse conversion uses the original bundled scalar instead of a rounded
    ratio, preserving the original physical hash. Explicit already-Sveby input
    is accepted only if its scalar and source timing validate.
    """
    sveby_power, _ = _reference_powers(spec, source_basis)
    if source_basis == SVEBY_HOUSEHOLD_BASIS:
        return spec
    return spec.updated(use=spec.use.updated(equipment_gain_w_m2=sveby_power))
