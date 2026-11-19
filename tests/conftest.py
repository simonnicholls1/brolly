import pytest
from fastapi.testclient import TestClient

from brolly.app import create_app
from brolly.db import VoteStore


class FakeWeather:
    def __init__(self):
        self.now = {
            "temp": 9.0, "feels_like": 6.0, "precip": 0.0, "snowfall": 0.0, "code": 3,
            "cloud": 90, "wind": 12, "gusts": 20, "is_day": True, "rain_chance": 10,
            "timezone": "Europe/London", "utc_offset_s": 0,
        }
        self.clim = {"temp_mean": 8.5, "feels_like_mean": 5.5, "wet_day_pct": 0.4, "years": 10, "days": 150}
        self.fail = False

    async def current(self, lat, lon):
        if self.fail:
            raise RuntimeError("down")
        return dict(self.now)

    async def climate(self, lat, lon, today):
        if self.fail:
            raise RuntimeError("down")
        return dict(self.clim)

    async def geocode(self, q):
        return [{"name": "London", "region": "England, United Kingdom", "lat": 51.5, "lon": -0.12}]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    store = VoteStore(":memory:")
    weather = FakeWeather()
    clock = Clock()
    client = TestClient(create_app(store=store, weather=weather, clock=clock))
    return client, store, weather, clock
