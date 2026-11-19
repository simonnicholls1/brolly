"""Copy votes from a SQLite file into Postgres.

    python -m brolly.migrate /data/brolly.db "$DATABASE_URL"

Safe to re-run: refuses to copy into a database that already has votes.
"""

import sys

from .db import COLUMNS, PostgresVoteStore, SqliteVoteStore


def main(sqlite_path: str, pg_url: str) -> None:
    src = SqliteVoteStore(sqlite_path)
    dst = PostgresVoteStore(pg_url)
    if dst.all_votes():
        sys.exit("Target already has votes; not copying (to avoid duplicates).")
    rows = src.all_votes()
    for r in rows:
        dst.add_vote(**{c: r[c] for c in COLUMNS})
    print(f"Copied {len(rows)} votes.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
