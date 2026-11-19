"""SQLite storage for votes. One file, no server needed."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

from .verdict import haversine_km

SCHEMA = """
CREATE TABLE IF NOT EXISTS votes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL    NOT NULL,          -- unix seconds
    lat         REAL    NOT NULL,          -- rounded to ~1km for privacy
    lon         REAL    NOT NULL,
    needed      INTEGER NOT NULL,          -- 1 = needed a coat
    client_id   TEXT    NOT NULL,
    ip_hash     TEXT,
    temp        REAL,                      -- weather snapshot at vote time,
    feels_like  REAL,                      -- used later as "historical" data
    wet         INTEGER,
    weather_code INTEGER
);
CREATE INDEX IF NOT EXISTS votes_time ON votes (created_at);
CREATE INDEX IF NOT EXISTS votes_place ON votes (lat, lon);
CREATE INDEX IF NOT EXISTS votes_client ON votes (client_id, created_at);
"""


class VoteStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _box(lat: float, lon: float, km: float) -> tuple[float, float, float, float]:
        dlat = km / 111.0
        dlon = km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
        return lat - dlat, lat + dlat, lon - dlon, lon + dlon

    def _nearby(self, sql_extra: str, params: list, lat: float, lon: float, km: float) -> list[dict]:
        a, b, c, d = self._box(lat, lon, km)
        rows = self._conn.execute(
            f"SELECT * FROM votes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? {sql_extra}",
            [a, b, c, d, *params],
        ).fetchall()
        return [dict(r) for r in rows if haversine_km(lat, lon, r["lat"], r["lon"]) <= km]

    def live_votes(self, lat: float, lon: float, km: float, since: float) -> list[dict]:
        return self._nearby("AND created_at >= ?", [since], lat, lon, km)

    def similar_votes(
        self, lat: float, lon: float, km: float, feels_like: float, tol: float, wet: bool,
        before: float, after: float,
    ) -> list[dict]:
        return self._nearby(
            "AND created_at < ? AND created_at >= ? AND feels_like BETWEEN ? AND ? AND wet = ?",
            [before, after, feels_like - tol, feels_like + tol, int(wet)],
            lat, lon, km,
        )

    def recent_vote_by_client(self, client_id: str, since: float) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM votes WHERE client_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            [client_id, since],
        ).fetchone()
        return dict(row) if row else None

    def count_by_ip(self, ip_hash: str, since: float) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM votes WHERE ip_hash = ? AND created_at >= ?", [ip_hash, since]
        ).fetchone()[0]

    def add_vote(self, **v) -> int:
        cur = self._conn.execute(
            """INSERT INTO votes (created_at, lat, lon, needed, client_id, ip_hash, temp, feels_like, wet, weather_code)
               VALUES (:created_at, :lat, :lon, :needed, :client_id, :ip_hash, :temp, :feels_like, :wet, :weather_code)""",
            v,
        )
        return cur.lastrowid

    def update_vote(self, vote_id: int, **v) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in v)
        self._conn.execute(f"UPDATE votes SET {sets} WHERE id = :id", {**v, "id": vote_id})
