"""Pure scoring logic: turns votes + weather into "do I need a coat?".

Every signal produces a probability (0..1) that a coat is needed and a weight
saying how much we trust it. The final answer is the weighted mean.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

VOTE_WINDOW_S = 2 * 60 * 60  # live votes expire after 2 hours
LIVE_RADIUS_KM = 10
SIMILAR_RADIUS_KM = 100
SIMILAR_FEELS_TOLERANCE_C = 2.5

# How much each signal counts.
W_FORECAST = 3.0  # the weather right now
W_CLIMATE = 1.0  # what this date is usually like here
W_PER_SIMILAR_VOTE = 0.25  # past votes cast in similar weather...
W_MAX_SIMILAR = 4.0  # ...capped
W_PER_LIVE_VOTE = 1.5  # a brand-new local vote; decays linearly to 0 over 2h

WET_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def cold_probability(feels_like: float) -> float:
    """Chance you'd want a coat purely from how cold it feels.

    50/50 at 14°C feels-like, ~90% at 8°C, ~10% at 20°C.
    """
    return 1 / (1 + math.exp(-(14 - feels_like) / 2.7))


def is_snowy(w: dict) -> bool:
    return w.get("code") in SNOW_CODES or (w.get("snowfall") or 0) > 0


def is_wet(w: dict) -> bool:
    return (
        (w.get("precip") or 0) >= 0.1
        or w.get("code") in WET_CODES
        or is_snowy(w)
        or (w.get("rain_chance") or 0) >= 50
    )


@dataclass
class Signal:
    key: str
    label: str
    p: float | None
    weight: float
    detail: dict = field(default_factory=dict)
    share: float = 0.0


def forecast_signal(w: dict | None) -> Signal:
    if not w:
        return Signal("forecast", "Weather right now", None, 0, {"reasons": ["Weather unavailable"]})
    reasons = [f"Feels like {round(w['feels_like'])}°C"]
    p = cold_probability(w["feels_like"])

    if is_snowy(w):
        p = max(p, 0.97)
        reasons.append("Snow about")
    elif (w.get("precip") or 0) >= 0.1 or w.get("code") in WET_CODES:
        p = max(p, 0.88)
        reasons.append("Raining now")
    elif (w.get("rain_chance") or 0) >= 50:
        p = max(p, 0.6 + (w["rain_chance"] - 50) / 200)
        reasons.append(f"{round(w['rain_chance'])}% chance of rain in the next few hours")

    if (w.get("wind") or 0) >= 30 or (w.get("gusts") or 0) >= 50:
        p = _clamp(p + 0.08)
        reasons.append("Windy")

    return Signal("forecast", "Weather right now", _clamp(p), W_FORECAST, {"reasons": reasons})


def climate_signal(c: dict | None) -> Signal:
    if not c:
        return Signal("climate", "Usual for this date", None, 0)
    feels = c.get("feels_like_mean")
    if feels is None:
        feels = c["temp_mean"]
    p = _clamp(cold_probability(feels) + (c.get("wet_day_pct") or 0) * 0.2)
    return Signal("climate", "Usual for this date", p, W_CLIMATE, c)


def live_vote_signal(votes: list[dict], now: float) -> Signal:
    """Nearby votes from the last two hours. Newer votes count for more."""
    yes_w = no_w = 0.0
    yes = no = 0
    for v in votes:
        age = now - v["created_at"]
        if age < 0 or age > VOTE_WINDOW_S:
            continue
        w = 1 - age / VOTE_WINDOW_S
        if v["needed"]:
            yes_w += w
            yes += 1
        else:
            no_w += w
            no += 1
    total = yes_w + no_w
    return Signal(
        "live",
        "People nearby, last 2 hours",
        yes_w / total if total > 0 else None,
        total * W_PER_LIVE_VOTE,
        {"yes": yes, "no": no, "count": yes + no, "freshness": round(total, 3)},
    )


def similar_vote_signal(votes: list[dict]) -> Signal:
    """Earlier votes cast in similar conditions (feels-like and wet/dry)."""
    count = len(votes)
    yes = sum(1 for v in votes if v["needed"])
    return Signal(
        "similar",
        "Past votes in similar weather",
        # Laplace smoothing so a single vote doesn't mean 100%.
        (yes + 1) / (count + 2) if count else None,
        min(count * W_PER_SIMILAR_VOTE, W_MAX_SIMILAR),
        {"yes": yes, "no": count - yes, "count": count},
    )


def confidence_label(p: float) -> str:
    d = abs(p - 0.5)
    if d >= 0.35:
        return "definitely"
    if d >= 0.15:
        return "probably"
    return "borderline"


def combine(signals: list[Signal]) -> dict | None:
    usable = [s for s in signals if s.p is not None and s.weight > 0]
    total = sum(s.weight for s in usable)
    if total == 0:
        return None
    p = sum(s.p * s.weight for s in usable) / total
    for s in signals:
        s.share = s.weight / total if s in usable else 0.0
    return {
        "p": p,
        "need_coat": p >= 0.5,
        "confidence": confidence_label(p),
        "breakdown": [asdict(s) for s in signals],
    }


def headline(result: dict | None, weather: dict | None) -> str:
    if not result:
        return ""
    wet = bool(weather) and is_wet(weather)
    if result["need_coat"]:
        if weather and is_snowy(weather):
            return "Wrap up, it’s snowy out."
        if wet:
            return "Grab something waterproof."
        if result["confidence"] == "borderline":
            return "Only just. A light jacket will do."
        return "Wrap up, it’s chilly."
    if wet:
        return "Warm enough, but take a brolly."
    if result["confidence"] == "borderline":
        return "Probably not, but bring a layer just in case."
    return "Leave it at home."


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
