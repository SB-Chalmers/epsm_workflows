"""Explicit EPC comparison boundaries and unfitted annual cross-building metrics.

Study selection, physical assumptions and regulatory normalization stay outside core.
These helpers do not certify EPC primary energy or turn allocated values into meters.
"""

import calendar
import math
import re
from collections import Counter
from datetime import date
from statistics import median

STATES = ("existing", "standard", "advanced")
MATCH_TOLERANCE = 0.20
RANGE_TOLERANCE = 0.15
MATCH_TARGET = 0.50
MINIMUM_COHORT = 10
# A cohort is materially worse when its shortfall rises by more than two buildings in twenty.
MATERIAL_SHORTFALL = 0.10


def reporting_regime(certificate_date):
    """Return the Swedish EPC reporting regime active on the approval date."""
    value = (
        date.fromisoformat(certificate_date)
        if isinstance(certificate_date, str)
        else certificate_date
    )
    if value < date(2019, 1, 1):
        return "specific_energy_pre_2019"
    if value < date(2020, 9, 1):
        return "primary_energy_2019_weights"
    return "primary_energy_2020_weights"


def number(values, field, *, required=True):
    """Parse Boverket decimal strings; missing values remain unknown."""
    value = values.get(field)
    if value is None or str(value).strip() == "":
        if required:
            raise ValueError(f"Missing {field}")
        return None
    try:
        result = float(str(value).strip().replace(",", "."))
    except ValueError as error:
        raise ValueError(f"Invalid {field}: {value!r}") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid nonnegative {field}: {value!r}")
    return result


def measurement_period(first, last):
    """Boverket YYMM fields, explicitly restricted to the 1999–2098 era."""

    def month(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}", value):
            raise ValueError("Expected Boverket YYMM measurement month")
        yy, mm = int(value[:2]), int(value[2:])
        return date(1999 if yy == 99 else 2000 + yy, mm, 1)

    start, end_month = month(first), month(last)
    end = end_month.replace(day=calendar.monthrange(end_month.year, end_month.month)[1])
    if (end.year - start.year) * 12 + end.month - start.month != 11:
        raise ValueError("Measurement period must contain twelve consecutive months")
    return start, end


def district_heat_observation(values, *, schema):
    """Read reported district heat; normal-use adjustment may already be applied.

    Missing alternative carriers mean 'no energy declared', not verified absence.
    The old DHW field is a subset of total district heat, never an addition.
    """
    if schema not in {"fore_v260", "efter_v260"}:
        raise ValueError("Unknown EPC export schema")
    if values.get("EgiBeraknatVarde") != "Nej":
        raise ValueError(
            "Calculated or unknown energy is not a measurement-based target"
        )
    start, end = measurement_period(
        values["EgiForstaArManad"], values["EgiSistaArManad"]
    )
    atemp = number(values, "EgenAtemp")
    if atemp <= 0:
        raise ValueError("Atemp must be positive")
    alternatives = [
        "Olja",
        "Gas",
        "Ved",
        "Flis",
        "OvrBiobransle",
        "ElVatten",
        "ElDirekt",
        "ElLuft",
        "PumpMark",
        "PumpFranluft",
        "PumpLuftLuft",
        "PumpLuftVatten",
    ]
    suffixes = [""] if schema == "fore_v260" else ["UPPV", "VV"]
    declared_fields = [
        "Egi" + fuel + suffix for fuel in alternatives for suffix in suffixes
    ]
    if schema == "efter_v260":
        declared_fields += ["EgiElVV"]
    if any((number(values, key, required=False) or 0) > 0 for key in declared_fields):
        raise ValueError("Mixed heat carriers require separate delivery accounting")
    if (number(values, "EgiSolvarme", required=False) or 0) > 0 or values.get(
        "EgiGruppSolvarme"
    ) == "Ja":
        raise ValueError("Solar thermal requires separate delivery accounting")
    if schema == "fore_v260":
        total = number(values, "EgiFjarrvarme")
        dhw = number(values, "EgiVVBered", required=False)
        kind = values.get("EgiGruppFjarrvarme")
        if kind not in {"Mätt", "Fördelat"}:
            raise ValueError("Unknown district-heat measurement basis")
        target_fields = ["EgiFjarrvarme"]
    else:
        heat = number(values, "EgiFjarrvarmeUPPV")
        dhw = number(values, "EgiFjarrvarmeVV")
        total = heat + dhw
        kind = "measurement_based; individual_meter_status_not_exported"
        target_fields = ["EgiFjarrvarmeUPPV", "EgiFjarrvarmeVV"]
    if total <= 0 or (dhw is not None and dhw > total):
        raise ValueError("Inconsistent district heat and DHW")
    approval = values.get("ExpertGodkannande", "")[:10]
    regime = reporting_regime(approval) if approval else None
    return dict(
        formular_id=str(values["FormularId"]),
        source_schema=schema,
        measurement_start=start.isoformat(),
        measurement_end=end.isoformat(),
        certificate_date=approval or None,
        reporting_regime=regime,
        atemp_m2=atemp,
        reported_district_heat_kwh=total,
        reported_dhw_subset_kwh=dhw,
        measurement_kind=kind,
        target_fields=target_fields,
        unit="kWh",
        energy_boundary="reported_delivered_district_heat_heat_and_dhw",
        normal_use_status="may_be_adjusted_before_entry",
        normal_year_status="before_automatic_energiindex_correction",
        undeclared_alternative_carriers=[
            k for k in declared_fields if number(values, k, required=False) is None
        ],
        reported_performance_kwh_m2=number(
            values, "EgiEnergiPrestanda", required=False
        ),
        reported_primary_2019_kwh_m2=number(
            values, "EgiPrimarenergital2019", required=False
        ),
        reported_primary_2020_kwh_m2=number(
            values, "EgiPrimarenergital2020", required=False
        ),
    )


def district_heat_estimate(
    space_heat_kwh,
    dhw_kwh,
    atemp_m2,
    *,
    space_efficiency,
    dhw_efficiency,
    airing_kwh_m2,
    net_distribution_loss_kwh_m2,
):
    """Explicit simplified building-boundary estimate; efficiencies exclude recoveries.

    Net losses must exclude heat already recovered in the simulated conditioned space.
    Callers must state the evidence/assumption behind every delivery parameter.
    """
    values = [
        space_heat_kwh,
        dhw_kwh,
        atemp_m2,
        space_efficiency,
        dhw_efficiency,
        airing_kwh_m2,
        net_distribution_loss_kwh_m2,
    ]
    if not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError("Delivery inputs must be finite and nonnegative")
    if atemp_m2 <= 0 or not 0 < space_efficiency <= 1 or not 0 < dhw_efficiency <= 1:
        raise ValueError("Positive area and efficiencies in (0, 1] required")
    heating = (space_heat_kwh + airing_kwh_m2 * atemp_m2) / space_efficiency
    dhw = dhw_kwh / dhw_efficiency
    losses = net_distribution_loss_kwh_m2 * atemp_m2
    return dict(
        space_heating_delivery_kwh=heating,
        dhw_delivery_kwh=dhw,
        net_distribution_loss_kwh=losses,
        delivered_district_heat_kwh=heating + dhw + losses,
    )


def comparison_terms(building_type, *, municipality_code=None, apply=()):
    """Sourced annual terms, in kWh/m2 Atemp, for the terms a study chose to ``apply``.

    ``apply`` names ``window_airing`` and ``hot_water_circulation``. Which terms suit
    which building type is the study's accepted result, so nothing applies by default.
    """
    from epsm_data import select_epc_comparison_terms

    keys = {
        "window_airing": "window_airing_kwh_m2_year",
        "hot_water_circulation": "hot_water_circulation_kwh_m2_year",
    }
    if set(apply) - set(keys):
        raise ValueError(f"Unknown comparison terms: {sorted(set(apply) - set(keys))}")
    values = select_epc_comparison_terms(
        building_type, municipality_code=municipality_code
    ).values
    terms = {name: values[keys[name]] for name in apply}
    undefined = [name for name, value in terms.items() if value is None]
    if undefined:
        raise ValueError(f"{building_type} has no sourced value for {undefined}")
    return terms


def delivered_heating_estimate(
    space_heat_kwh,
    atemp_m2,
    *,
    carrier,
    period_days=365,
    efficiency=None,
    seasonal_cop=None,
    terms_kwh_m2_year=None,
):
    """Modelled heating in the same terms as an EPC's measured heating, for any carrier.

    ``space_heat_kwh`` is simulated heat over the measured period. Annual per-m2 terms
    are scaled to ``period_days`` and added before conversion: they are heat the
    building needs, so the system's efficiency or COP applies to them too.
    """
    terms_kwh_m2_year = dict(terms_kwh_m2_year or {})
    values = [space_heat_kwh, atemp_m2, period_days, *terms_kwh_m2_year.values()]
    if not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError("Delivery inputs must be finite and nonnegative")
    if atemp_m2 <= 0 or period_days <= 0:
        raise ValueError("Positive area and period required")
    if carrier == "heat_pump":
        if efficiency is not None or seasonal_cop is None or not seasonal_cop > 0:
            raise ValueError("A heat pump requires a positive seasonal_cop only")
        divisor = seasonal_cop
    elif carrier == "fuel":
        if seasonal_cop is not None or efficiency is None or not 0 < efficiency <= 1:
            raise ValueError("Fuel requires an efficiency in (0, 1] only")
        divisor = efficiency
    elif carrier in {"district_heat", "electric_direct"}:
        if efficiency is not None or seasonal_cop is not None:
            raise ValueError(f"{carrier} delivers heat directly")
        divisor = 1
    else:
        raise ValueError(f"Unknown heating carrier: {carrier!r}")
    terms = {
        name: value * atemp_m2 * period_days / 365
        for name, value in terms_kwh_m2_year.items()
    }
    heat = space_heat_kwh + sum(terms.values())
    return dict(
        heat_kwh=heat,
        term_heat_kwh=terms,
        delivered_heating_kwh=heat / divisor,
        divisor=divisor,
        carrier=carrier,
    )


def accuracy(observed, predicted):
    """Annual cross-building errors, predicted minus observed; no fitted DOF.

    This CVRMSE is across buildings, not an ASHRAE monthly/hourly calibration test.
    No default pass/fail or national generalization is inferred.
    """
    observed, predicted = list(observed), list(predicted)
    if not observed or len(observed) != len(predicted):
        raise ValueError("Equal nonempty paired observations and predictions required")
    if not all(math.isfinite(v) and v > 0 for v in observed):
        raise ValueError("Observed values must be finite and positive")
    if not all(math.isfinite(v) and v >= 0 for v in predicted):
        raise ValueError("Predictions must be finite and nonnegative")
    n = len(observed)
    errors = [p - o for o, p in zip(observed, predicted)]
    ape = [abs(e / o) for e, o in zip(errors, observed)]
    mean = sum(observed) / n
    mse = sum(e * e for e in errors) / n
    denominator = sum((o - mean) ** 2 for o in observed)
    return dict(
        n=n,
        mean_observed=sum(observed) / n,
        mean_predicted=sum(predicted) / n,
        mae=sum(abs(e) for e in errors) / n,
        rmse=math.sqrt(mse),
        bias_percent=100 * sum(errors) / sum(observed),
        cvrmse_percent=100 * math.sqrt(mse) / mean,
        mape_percent=100 * sum(ape) / n,
        median_ape_percent=100 * median(ape),
        within_20_percent_count=sum(a <= 0.20 for a in ape),
        r2=1 - n * mse / denominator if denominator > 0 else None,
        fitted_parameters=0,
        error_sign="predicted_minus_reported",
        sampling_unit="annual_building_record",
    )


def classify_building(ratios):
    """Place one building's modelled/measured ratios against the catalogue range.

    ``ratios`` holds ``existing``, ``standard`` and ``advanced``, or ``single`` for an
    era with one state, which spans no range. ``position`` says where the model sits:
    ``above_range`` means the measurement is above Existing, ``below_range`` below Advanced.
    """
    if set(ratios) not in ({"single"}, set(STATES)):
        raise ValueError("Ratios must hold every renovation state, or only single")
    if not all(math.isfinite(r) and r > 0 for r in ratios.values()):
        raise ValueError("Ratios must be finite and positive")
    single = "single" in ratios
    low, high = (
        (ratios["single"], ratios["single"])
        if single
        else sorted((ratios["advanced"], ratios["existing"]))
    )
    matched = {s: r for s, r in ratios.items() if abs(r - 1) <= MATCH_TOLERANCE}
    return dict(
        ratios=dict(ratios),
        single_state=single,
        position="inside" if low <= 1 <= high else "above_range" if high < 1 else "below_range",
        covered=None if single else low <= 1 <= high,
        selected_state=min(matched, key=lambda s: abs(matched[s] - 1)) if matched else None,
    )


def score_cohorts(buildings):
    """Score type x era cohorts against the three targets of the measured-data contract.

    ``buildings`` yields mappings with ``category``, ``era`` and ``ratios``. The result is
    keyed by (category, era). ``shortfall`` sums how far each target is missed. A
    single-state era has no bottom target, and its top target is the share measured
    above that state. A cohort below ``MINIMUM_COHORT`` is scored but flagged.
    """
    groups = {}
    for building in buildings:
        key = building["category"], building["era"]
        groups.setdefault(key, []).append(classify_building(building["ratios"]))
    cohorts = {}
    for key, rows in sorted(groups.items()):
        n = len(rows)
        single = {row["single_state"] for row in rows}
        if len(single) != 1:
            raise ValueError(f"Cohort {key} mixes single-state and three-state buildings")
        single = single.pop()
        above = sum(row["position"] == "above_range" for row in rows) / n
        below = None if single else sum(row["position"] == "below_range" for row in rows) / n
        match = sum(row["selected_state"] is not None for row in rows) / n
        shortfall = max(0, MATCH_TARGET - match) + max(0, above - RANGE_TOLERANCE)
        if not single:
            shortfall += max(0, below - RANGE_TOLERANCE)
        cohorts[key] = dict(
            n=n,
            single_state=single,
            enough_buildings=n >= MINIMUM_COHORT,
            above_existing=above,
            below_advanced=below,
            match=match,
            coverage=None if single else sum(row["covered"] for row in rows) / n,
            existing_median_ratio=None
            if single
            else median(row["ratios"]["existing"] for row in rows),
            selected_states=dict(Counter(row["selected_state"] for row in rows)),
            shortfall=shortfall,
            meets_targets=shortfall == 0,
        )
    return cohorts


def summarise_cohorts(cohorts, category=None):
    """Combine scored cohorts, weighted by building, for one type or all of them."""
    chosen = [c for (name, _), c in cohorts.items() if category in (None, name)]
    if not chosen:
        raise ValueError(f"No cohorts for {category!r}")
    ranged = [c for c in chosen if not c["single_state"]]
    n = sum(c["n"] for c in chosen)
    weighted = lambda field, rows: sum(c[field] * c["n"] for c in rows) / sum(c["n"] for c in rows)
    return dict(
        n=n,
        above_existing=weighted("above_existing", chosen),
        below_advanced=weighted("below_advanced", ranged) if ranged else None,
        match=weighted("match", chosen),
        shortfall=sum(c["shortfall"] for c in chosen),
        cohorts_meeting_targets=sum(c["meets_targets"] for c in chosen),
    )


def compare_cohorts(baseline, changed):
    """Test a change on one sample, per building type, by the acceptance rule.

    Both scorings must cover the same cohorts. Whether a value stays in its sourced range,
    and applies per type or era trend, is for the study to state.
    """
    if set(baseline) != set(changed):
        raise ValueError("Compare the same cohorts")
    result = {}
    for category in sorted({name for name, _ in changed}):
        before, after = summarise_cohorts(baseline, category), summarise_cohorts(changed, category)
        worse = [
            era
            for (name, era), cohort in changed.items()
            if name == category
            and cohort["shortfall"] - baseline[name, era]["shortfall"] > MATERIAL_SHORTFALL + 1e-9
        ]
        result[category] = dict(
            baseline=before,
            changed=after,
            materially_worse=worse,
            accepted=after["shortfall"] < before["shortfall"] - 1e-9
            and after["above_existing"] <= before["above_existing"]
            and after["match"] >= before["match"]
            and not worse,
        )
    return result


def accepted_on_both(calibration, holdout):
    """The building types a change passes on a calibration and a disjoint hold-out sample."""
    return sorted(
        category
        for category in set(calibration) & set(holdout)
        if calibration[category]["accepted"] and holdout[category]["accepted"]
    )
