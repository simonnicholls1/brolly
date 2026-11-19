from brolly import verdict as v


def test_cold_probability_shape():
    assert v.cold_probability(14) == 0.5
    assert v.cold_probability(5) > 0.9
    assert v.cold_probability(24) < 0.05


def test_rain_forces_coat_even_when_mild():
    s = v.forecast_signal({"feels_like": 19, "precip": 1.2, "code": 63, "wind": 5})
    assert s.p >= 0.88
    assert "Raining now" in s.detail["reasons"]


def test_warm_dry_day_says_no():
    s = v.forecast_signal({"feels_like": 24, "precip": 0, "code": 0, "wind": 5, "rain_chance": 5})
    assert s.p < 0.1


def test_live_votes_decay_and_expire():
    now = 10_000.0
    votes = [
        {"created_at": now - 60, "needed": 1},  # fresh yes
        {"created_at": now - 6000, "needed": 0},  # old no, mostly decayed
        {"created_at": now - v.VOTE_WINDOW_S - 1, "needed": 0},  # expired
    ]
    s = v.live_vote_signal(votes, now)
    assert s.detail["count"] == 2
    assert s.p > 0.75
    assert 0 < s.weight < 2 * v.W_PER_LIVE_VOTE


def test_no_votes_falls_back_to_weather():
    r = v.combine([
        v.live_vote_signal([], 0),
        v.forecast_signal({"feels_like": 5, "code": 3}),
        v.similar_vote_signal([]),
        v.climate_signal(None),
    ])
    assert r["need_coat"] is True
    shares = {b["key"]: b["share"] for b in r["breakdown"]}
    assert shares["forecast"] == 1.0
    assert shares["live"] == 0


def test_enough_fresh_votes_can_overrule_forecast():
    now = 1000.0
    votes = [{"created_at": now, "needed": 0} for _ in range(5)]
    r = v.combine([
        v.live_vote_signal(votes, now),
        v.forecast_signal({"feels_like": 11, "code": 2}),
    ])
    assert r["need_coat"] is False


def test_similar_votes_are_smoothed_and_capped():
    s = v.similar_vote_signal([{"needed": 1}])
    assert s.p == 2 / 3
    s = v.similar_vote_signal([{"needed": 1}] * 100)
    assert s.weight == v.W_MAX_SIMILAR


def test_combine_none_when_nothing_usable():
    assert v.combine([v.forecast_signal(None), v.climate_signal(None)]) is None


def test_headlines():
    yes = {"need_coat": True, "confidence": "definitely"}
    no = {"need_coat": False, "confidence": "definitely"}
    assert "waterproof" in v.headline(yes, {"precip": 2, "code": 63})
    assert "brolly" in v.headline(no, {"precip": 0, "code": 0, "rain_chance": 80})
    assert v.headline(no, {"precip": 0, "code": 0}) == "Leave it at home."


def test_haversine():
    assert abs(v.haversine_km(51.5, -0.12, 48.85, 2.35) - 343) < 5
