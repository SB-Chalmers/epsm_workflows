"""Explicit EPC comparison boundaries and unfitted annual cross-building metrics.

Study selection, physical assumptions and regulatory normalization stay outside core.
These helpers do not certify EPC primary energy or turn allocated values into meters.
"""

import calendar
import math
import re
from datetime import date
from statistics import median


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
