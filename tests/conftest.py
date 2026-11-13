"""Shared fixtures for the campaign tests; engines are never required."""

import csv
from pathlib import Path

import pytest


def _tmyx_source():
    """A bundled TMYx EPW from the Core checkout, a sibling checkout or EPSM_TEST_EPW."""
    import os

    folders = []
    if os.environ.get("EPSM_TEST_EPW"):
        return Path(os.environ["EPSM_TEST_EPW"])
    try:
        import epsm_core

        folders.append(Path(epsm_core.__file__).resolve().parents[2] / "data" / "weather")
    except ImportError:
        pass
    folders.append(Path(__file__).resolve().parents[2] / "epsm_core" / "data" / "weather")
    for folder in folders:
        candidates = sorted(folder.glob("*Karlstad*TMYx*.epw")) if folder.exists() else []
        if candidates:
            return candidates[0]
    return None


@pytest.fixture(scope="session")
def calendar_epw(tmp_path_factory):
    """A complete single-year (2018) EPW derived from a bundled TMYx file."""
    source = _tmyx_source()
    if source is None:
        pytest.skip("epsm_core weather data is not available")
    destination = tmp_path_factory.mktemp("weather") / "test-2018.epw"
    with source.open(newline="", errors="replace") as stream:
        rows = list(csv.reader(stream))
    header, data = rows[:8], rows[8:]
    if len(data) != 8760:
        pytest.skip("TMYx source does not have 8760 rows")
    for row in data:
        row[0] = "2018"
    with destination.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerows(header)
        writer.writerows(data)
    return destination


@pytest.fixture(scope="session")
def core_solves_u_values():
    """Skip tests that need Building.assign_element_u_value when Core lacks it."""
    try:
        from epsm_core import Building
    except ImportError:
        pytest.skip("epsm_core is not installed")
    if not hasattr(Building, "assign_element_u_value"):
        pytest.skip("Core without assign_element_u_value")
    return True
