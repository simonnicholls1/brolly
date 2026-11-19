"""HTTP API + static front end."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import verdict as v
from .db import VoteStore, open_store
from .weather import OpenMeteo, local_date

log = logging.getLogger("brolly")

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
SIMILAR_LOOKBACK_S = 365 * 24 * 60 * 60
MAX_VOTES_PER_IP_PER_HOUR = 10


class VoteIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    needed: bool
    client_id: str = Field(min_length=8, max_length=64)


def _round_loc(lat: float, lon: float) -> tuple[float, float]:
    # ~1km precision: enough to be local, not enough to pinpoint anyone.
    return round(lat, 2), round(lon, 2)


def create_app(
    store: VoteStore | None = None,
    weather=None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    store = store or open_store()
    weather = weather or OpenMeteo()
    salt = os.environ.get("IP_SALT", "brolly")

    app = FastAPI(title="Do I need a coat?")

    def ip_hash(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")
        return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:24]

    async def db(fn, *args, **kwargs):
        # Database calls block; keep them off the event loop.
        return await run_in_threadpool(fn, *args, **kwargs)

    async def safe(coro, what: str):
        try:
            return await coro
        except Exception as e:  # weather being down shouldn't break the answer
            log.warning("%s lookup failed: %s", what, e)
            return None

    @app.get("/api/verdict")
    async def get_verdict(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180)):
        now = clock()
        lat, lon = _round_loc(lat, lon)

        w = await safe(weather.current(lat, lon), "weather")
        climate = await safe(weather.climate(lat, lon, local_date(w, now)), "climate")

        live = await db(store.live_votes, lat, lon, v.LIVE_RADIUS_KM, since=now - v.VOTE_WINDOW_S)
        similar = (
            await db(
                store.similar_votes,
                lat, lon, v.SIMILAR_RADIUS_KM, w["feels_like"], v.SIMILAR_FEELS_TOLERANCE_C,
                v.is_wet(w), before=now - v.VOTE_WINDOW_S, after=now - SIMILAR_LOOKBACK_S,
            )
            if w
            else []
        )

        signals = [
            v.live_vote_signal(live, now),
            v.forecast_signal(w),
            v.similar_vote_signal(similar),
            v.climate_signal(climate),
        ]
        result = v.combine(signals)
        if result is None:
            raise HTTPException(503, "No weather or votes available for this spot right now.")

        recent = sorted(
            (
                {"minutes_ago": int((now - x["created_at"]) // 60), "needed": bool(x["needed"])}
                for x in live
            ),
            key=lambda x: x["minutes_ago"],
        )
        return {
            "answer": "yes" if result["need_coat"] else "no",
            "probability": round(result["p"], 3),
            "confidence": result["confidence"],
            "headline": v.headline(result, w),
            "weather": w,
            "climate": climate,
            "signals": result["breakdown"],
            "live": {
                "votes": recent,
                "window_minutes": v.VOTE_WINDOW_S // 60,
                "radius_km": v.LIVE_RADIUS_KM,
            },
            "location": {"lat": lat, "lon": lon},
            "generated_at": now,
        }

    @app.post("/api/vote")
    async def post_vote(body: VoteIn, request: Request):
        now = clock()
        lat, lon = _round_loc(body.lat, body.lon)
        iph = ip_hash(request)

        w = await safe(weather.current(lat, lon), "weather")
        snapshot = {
            "temp": w["temp"] if w else None,
            "feels_like": w["feels_like"] if w else None,
            "wet": int(v.is_wet(w)) if w else None,
            "weather_code": w["code"] if w else None,
        }

        # One live vote per person: voting again within 2h changes your answer.
        existing = await db(store.recent_vote_by_client, body.client_id, since=now - v.VOTE_WINDOW_S)
        if existing:
            await db(
                store.update_vote,
                existing["id"], needed=int(body.needed), lat=lat, lon=lon, created_at=now, **snapshot
            )
            return {"ok": True, "updated": True}

        if await db(store.count_by_ip, iph, since=now - 3600) >= MAX_VOTES_PER_IP_PER_HOUR:
            raise HTTPException(429, "Too many votes from your network. Try again later.")

        await db(
            store.add_vote,
            created_at=now, lat=lat, lon=lon, needed=int(body.needed),
            client_id=body.client_id, ip_hash=iph, **snapshot,
        )
        return {"ok": True, "updated": False}

    @app.get("/api/geocode")
    async def geocode(q: str = Query(min_length=2, max_length=80)):
        results = await safe(weather.geocode(q), "geocode")
        if results is None:
            raise HTTPException(502, "Place search is unavailable right now.")
        return {"results": results}

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(PUBLIC_DIR / "index.html")

    app.mount("/", StaticFiles(directory=PUBLIC_DIR), name="static")
    return app

