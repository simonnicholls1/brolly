"""Vote storage.

Two backends with the same interface:
- Postgres (production): set DATABASE_URL=postgres://...
- SQLite (local dev / tests): DATABASE_PATH, default data/brolly.db
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
from pathlib import Path

from .verdict import haversine_km

log = logging.getLogger("brolly.db")

COLUMNS = (
    "created_at", "lat", "lon", "needed", "client_id", "ip_hash",
    "temp", "feels_like", "wet", "weather_code",
)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS votes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   REAL    NOT NULL,
    lat          REAL    NOT NULL,
    lon          REAL    NOT NULL,
    needed       INTEGER NOT NULL,
    client_id    TEXT    NOT NULL,
    ip_hash      TEXT,
    temp         REAL,
    feels_like   REAL,
    wet          INTEGER,
    weather_code INTEGER
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS votes (
    id           BIGSERIAL PRIMARY KEY,
    created_at   DOUBLE PRECISION NOT NULL,  -- unix seconds
    lat          DOUBLE PRECISION NOT NULL,  -- rounded to ~1km for privacy
    lon          DOUBLE PRECISION NOT NULL,
    needed       SMALLINT NOT NULL,          -- 1 = needed a coat
    client_id    TEXT NOT NULL,
    ip_hash      TEXT,
    temp         DOUBLE PRECISION,           -- weather snapshot at vote time,
    feels_like   DOUBLE PRECISION,           -- used later as "historical" data
    wet          SMALLINT,
    weather_code INTEGER
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS votes_time ON votes (created_at);
CREATE INDEX IF NOT EXISTS votes_place ON votes (lat, lon);
CREATE INDEX IF NOT EXISTS votes_client ON votes (client_id, created_at);
"""


class VoteStore:
    """Shared query logic. Subclasses provide _query/_execute/_insert."""

    def _query(self, sql: str, params: list) -> list[dict]:
        raise NotImplementedError

    def _execute(self, sql: str, params: list) -> None:
        raise NotImplementedError

    def _insert(self, sql: str, params: list) -> int:
        raise NotImplementedError

    def close(self) -> None:
        pass

    @staticmethod
    def _box(lat: float, lon: float, km: float) -> tuple[float, float, float, float]:
        dlat = km / 111.0
        dlon = km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
        return lat - dlat, lat + dlat, lon - dlon, lon + dlon

    def _nearby(self, extra: str, params: list, lat: float, lon: float, km: float) -> list[dict]:
        a, b, c, d = self._box(lat, lon, km)
        rows = self._query(
            f"SELECT * FROM votes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? {extra}",
            [a, b, c, d, *params],
        )
        return [r for r in rows if haversine_km(lat, lon, r["lat"], r["lon"]) <= km]

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
        rows = self._query(
            "SELECT * FROM votes WHERE client_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            [client_id, since],
        )
        return rows[0] if rows else None

    def count_by_ip(self, ip_hash: str, since: float) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM votes WHERE ip_hash = ? AND created_at >= ?", [ip_hash, since]
        )
        return int(rows[0]["n"])

    def add_vote(self, **v) -> int:
        cols = ", ".join(COLUMNS)
        marks = ", ".join("?" for _ in COLUMNS)
        return self._insert(f"INSERT INTO votes ({cols}) VALUES ({marks})", [v[c] for c in COLUMNS])

    def update_vote(self, vote_id: int, **v) -> None:
        unknown = set(v) - set(COLUMNS)
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")
        sets = ", ".join(f"{k} = ?" for k in v)
        self._execute(f"UPDATE votes SET {sets} WHERE id = ?", [*v.values(), vote_id])

    def delete_older_than(self, cutoff: float) -> int:
        n = self._query("SELECT COUNT(*) AS n FROM votes WHERE created_at < ?", [cutoff])[0]["n"]
        if n:
            self._execute("DELETE FROM votes WHERE created_at < ?", [cutoff])
        return int(n)

    def all_votes(self) -> list[dict]:
        return self._query("SELECT * FROM votes ORDER BY id", [])


class SqliteVoteStore(VoteStore):
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SQLITE_SCHEMA + INDEXES)

    def _query(self, sql, params):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _execute(self, sql, params):
        with self._lock:
            self._conn.execute(sql, params)

    def _insert(self, sql, params):
        with self._lock:
            return self._conn.execute(sql, params).lastrowid

    def close(self):
        self._conn.close()


class PostgresVoteStore(VoteStore):
    def __init__(self, url: str, min_size: int = 0, max_size: int = 5):
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # min_size=0 so an idle app holds no connections (lets serverless
        # Postgres like Neon scale to zero); check= revives dropped ones.
        self._pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            # prepare_threshold=None: no server-side prepared statements, so
            # Neon's pooled (-pooler, PgBouncer) URL works as well as the direct one.
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
            check=ConnectionPool.check_connection,
            open=True,
        )
        with self._pool.connection() as conn:
            conn.execute(POSTGRES_SCHEMA)
            for stmt in filter(str.strip, INDEXES.split(";")):
                conn.execute(stmt)

    @staticmethod
    def _pg(sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s")

    def _query(self, sql, params):
        with self._pool.connection() as conn:
            return conn.execute(self._pg(sql), params).fetchall()

    def _execute(self, sql, params):
        with self._pool.connection() as conn:
            conn.execute(self._pg(sql), params)

    def _insert(self, sql, params):
        with self._pool.connection() as conn:
            return conn.execute(self._pg(sql) + " RETURNING id", params).fetchone()["id"]

    def close(self):
        self._pool.close()


def open_store(url: str | None = None, path: str | None = None) -> VoteStore:
    url = url if url is not None else os.environ.get("DATABASE_URL", "")
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresVoteStore(url)
    if os.environ.get("FLY_APP_NAME"):
        log.warning("DATABASE_URL not set on Fly: falling back to SQLite, votes may not persist")
    return SqliteVoteStore(path or os.environ.get("DATABASE_PATH", "data/brolly.db"))
