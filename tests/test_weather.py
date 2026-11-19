from datetime import date

from brolly.weather import summarise_climate


def test_summarise_climate_uses_window_around_date():
    daily = {
        "time": ["2024-11-18", "2024-11-25", "2024-06-01", "2023-11-20"],
        "temperature_2m_mean": [8.0, 10.0, 20.0, 6.0],
        "apparent_temperature_mean": [5.0, 7.0, 19.0, 3.0],
        "precipitation_sum": [3.0, 0.0, 0.0, 1.5],
    }
    c = summarise_climate(daily, date(2026, 11, 19))
    assert c["days"] == 3
    assert c["temp_mean"] == 8.0
    assert c["feels_like_mean"] == 5.0
    assert c["wet_day_pct"] == 0.67
    assert c["years"] == 2


def test_summarise_climate_wraps_new_year():
    daily = {"time": ["2024-12-30"], "temperature_2m_mean": [2.0]}
    assert summarise_climate(daily, date(2026, 1, 2))["days"] == 1
