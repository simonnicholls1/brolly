from brolly import verdict as v

CID = "test-client-123"


def vote(client, needed, cid=CID, lat=51.5, lon=-0.12, ip="1.1.1.1"):
    return client.post(
        "/api/vote",
        json={"lat": lat, "lon": lon, "needed": needed, "client_id": cid},
        headers={"x-forwarded-for": ip},
    )


def test_verdict_from_weather_only(env):
    client, *_ = env
    r = client.get("/api/verdict", params={"lat": 51.5, "lon": -0.12})
    assert r.status_code == 200
    d = r.json()
    assert d["answer"] == "yes"  # feels like 6°C
    keys = {s["key"]: s for s in d["signals"]}
    assert keys["live"]["share"] == 0
    assert keys["forecast"]["share"] > 0.5
    assert keys["climate"]["share"] > 0
    assert d["live"]["votes"] == []


def test_votes_shift_the_answer_then_expire(env):
    client, store, weather, clock = env
    weather.now.update(feels_like=13.0, temp=14.0)  # borderline day
    for i in range(4):
        assert vote(client, False, cid=f"client-{i:04d}").status_code == 200
    d = client.get("/api/verdict", params={"lat": 51.5, "lon": -0.12}).json()
    assert d["answer"] == "no"
    assert len(d["live"]["votes"]) == 4

    clock.t += v.VOTE_WINDOW_S + 60
    d = client.get("/api/verdict", params={"lat": 51.5, "lon": -0.12}).json()
    assert d["live"]["votes"] == []
    # Expired votes now count as "similar weather" history instead.
    similar = next(s for s in d["signals"] if s["key"] == "similar")
    assert similar["detail"]["count"] == 4


def test_far_away_votes_are_ignored(env):
    client, *_ = env
    vote(client, False, lat=40.7, lon=-74.0)  # New York
    d = client.get("/api/verdict", params={"lat": 51.5, "lon": -0.12}).json()
    assert d["live"]["votes"] == []


def test_revote_changes_answer_instead_of_stacking(env):
    client, store, _, clock = env
    assert vote(client, True).json()["updated"] is False
    clock.t += 60
    assert vote(client, False).json()["updated"] is True
    d = client.get("/api/verdict", params={"lat": 51.5, "lon": -0.12}).json()
    assert d["live"]["votes"] == [{"minutes_ago": 0, "needed": False}]


def test_ip_rate_limit(env):
    client, *_ = env
    for i in range(10):
        assert vote(client, True, cid=f"spam-client-{i}").status_code == 200
    assert vote(client, True, cid="spam-client-x").status_code == 429


def test_vote_stores_weather_snapshot(env):
    client, store, *_ = env
    vote(client, True)
    row = store.all_votes()[0]
    assert row["feels_like"] == 6.0 and row["wet"] == 0
    assert row["lat"] == 51.5 and row["lon"] == -0.12


def test_weather_down_still_answers_from_votes(env):
    client, _, weather, _ = env
    vote(client, True)
    weather.fail = True
    d = client.get("/api/verdict", params={"lat": 51.5, "lon": -0.12}).json()
    assert d["answer"] == "yes"
    assert d["weather"] is None


def test_weather_down_and_no_votes_is_503(env):
    client, _, weather, _ = env
    weather.fail = True
    assert client.get("/api/verdict", params={"lat": 51.5, "lon": -0.12}).status_code == 503


def test_validation(env):
    client, *_ = env
    assert client.get("/api/verdict", params={"lat": 200, "lon": 0}).status_code == 422
    assert client.post("/api/vote", json={"lat": 0, "lon": 0, "needed": True, "client_id": "x"}).status_code == 422


def test_static_and_geocode(env):
    client, *_ = env
    assert "Do I need a coat?" in client.get("/").text
    assert client.get("/app.js").status_code == 200
    assert client.get("/api/geocode", params={"q": "Lon"}).json()["results"][0]["name"] == "London"
