"""Measured leap years enter a campaign as 8760-hour calendars."""

import csv

import pytest

pytest.importorskip("epsm_core")

from epsm_workflows.campaign.weather import drop_leap_day, epw_rows, nonleap_stand_in


def leap_epw(calendar_epw, destination):
    """A 2016 EPW: the 2018 fixture with 29 February copied from the 28th."""
    with calendar_epw.open(newline="", errors="replace") as stream:
        rows = list(csv.reader(stream))
    headers, data = rows[:8], rows[8:]
    headers[4] = ["HOLIDAYS/DAYLIGHT SAVINGS", "Yes", "0", "0", "0"]
    headers[7] = ["DATA PERIODS", "1", "1", "Data", "Friday", "1/1/2016", "12/31/2016"]
    out = []
    for row in data:
        row[0] = "2016"
        out.append(row)
        if int(row[1]) == 2 and int(row[2]) == 28:
            out.append([*row[:2], "29", *row[3:]])
    with destination.open("w", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(headers + out)
    return destination


def test_nonleap_stand_in_keeps_the_first_weekday():
    assert nonleap_stand_in(2016) == 2021 and nonleap_stand_in(2020) == 2025
    assert nonleap_stand_in(2024) == 2029


def test_drop_leap_day_yields_a_core_valid_nonleap_calendar(tmp_path, calendar_epw):
    from epsm_core.weather import climate_from_epw, validate_epw_calendar

    source = leap_epw(calendar_epw, tmp_path / "x_2016.epw")
    with pytest.raises(ValueError, match="expected 8760"):
        list(epw_rows("x_2016", source, 2016))
    year = nonleap_stand_in(2016)
    drop_leap_day(source, tmp_path / "sweep.epw", year)
    rows = list(epw_rows("x_2016", tmp_path / "sweep.epw", year))
    assert len(rows) == 8760 and rows[0][2] == "2021-01-01 00:00:00"
    assert not any(r[2].startswith("2021-02-29") for r in rows)
    with (tmp_path / "sweep.epw").open(newline="") as stream:
        lines = list(csv.reader(stream))
    assert lines[4][1] == "No" and lines[7][5:] == ["1/1/2021", "12/31/2021"] and lines[8][0] == "2021"
    validate_epw_calendar(tmp_path / "sweep.epw", year)
    assert climate_from_epw(tmp_path / "sweep.epw", calendar_year=year).calendar_year == year
