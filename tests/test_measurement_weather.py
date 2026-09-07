import pytest
from epsm_workflows.weather import measurement_year


def test_weather_measurement_gate():
    assert measurement_year("2024-01-01", "2024-12-31") == 2024
    with pytest.raises(ValueError, match="Cross-year"):
        measurement_year("2023-06-01", "2024-05-31")
    with pytest.raises(ValueError, match="complete"):
        measurement_year("2024-02-01", "2024-12-31")
