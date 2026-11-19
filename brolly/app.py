"""HTTP API + static front end."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import verdict as v
from .db import VoteStore, open_store
from .weather import OpenMeteo, local_date

log = logging.getLogger("brolly")

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
SIMILAR_LOOKBACK_S = 365 * 24 * 60 * 60
RETENTION_S = SIMILAR_LOOKBACK_S  # votes older than this are never used, so delete them
PRUNE_EVERY_S = 60 * 60
MAX_VOTES_PER_IP_PER_HOUR = 10
# Per-IP request limits (requests, window seconds) for endpoints that call Open-Meteo.
RATE_LIMITS = {"/api/verdict": (60, 60), "/api/geocode": (20, 60), "/api/vote": (30, 60)}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(self), camera=(), microphone=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class RateLimiter:
    """In-memory sliding window. Fine for a single machine."""

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def allow(self, key: str, bucket: str, limit: int, window: float) -> bool:
        now = self.clock()
        q = self.hits[(bucket, key)]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self.hits) > 50_000:  # crude memory cap
            self.hits.clear()
        return True


def client_ip(request: Request) -> str:
    # Fly's edge sets Fly-Client-IP and clients can't override it. X-Forwarded-For
    # is NOT trusted: its first entry is whatever the client sent.
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "unknown")


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

    # No public /docs, /redoc or /openapi.json.
    app = FastAPI(title="Do I need a coat?", docs_url=None, redoc_url=None, openapi_url=None)
    limiter = RateLimiter(clock)
    last_prune = [0.0]

    canonical = os.environ.get("CANONICAL_HOST", "").strip().lower()

    @app.middleware("http")
    async def canonical_host(request: Request, call_next):
        # Send www.<domain> and *.fly.dev visitors to the one real domain.
        # Other hosts (localhost, internal checks) are left alone.
        host = request.headers.get("host", "").split(":")[0].lower()
        if (
            canonical
            and host != canonical
            and (host == f"www.{canonical}" or host.endswith(".fly.dev"))
            and request.url.path != "/healthz"
        ):
            target = f"https://{canonical}{request.url.path}"
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(target, status_code=301)
        return await call_next(request)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        rule = RATE_LIMITS.get(request.url.path)
        if rule and not limiter.allow(client_ip(request), request.url.path, *rule):
            return JSONResponse({"detail": "Slow down a bit and try again in a minute."}, 429)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, val in SECURITY_HEADERS.items():
            response.headers.setdefault(k, val)
        return response

    def ip_hash(request: Request) -> str:
        return hashlib.sha256(f"{salt}:{client_ip(request)}".encode()).hexdigest()[:24]

    async def maybe_prune(now: float):
        if now - last_prune[0] < PRUNE_EVERY_S:
            return
        last_prune[0] = now
        try:
            n = await db(store.delete_older_than, now - RETENTION_S)
            if n:
                log.info("pruned %d votes older than retention", n)
        except Exception as e:
            log.warning("prune failed: %s", e)

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
        await maybe_prune(now)

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

