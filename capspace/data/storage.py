"""DuckDB-backed cache for upstream responses.

Both upstreams are rate-limited and neither is fast: `nba_api` throttles
aggressively and ESPN's fantasy endpoints are slow enough that an uncached
`rank` run is unpleasant. Game logs and league state change at most a few
times a day, so a 12-hour TTL costs nothing in accuracy.

The cache stores whole DataFrames, one table per key. It guarantees that
values survive the round trip, not that dtypes do: DuckDB widens `date`
columns to `datetime64[us]`, so callers normalize on parse. See
`test_date_columns_come_back_as_datetime64`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import warnings
from collections.abc import Callable
from pathlib import Path

import duckdb
import pandas as pd

DEFAULT_TTL_HOURS: float = 12.0

Clock = Callable[[], dt.datetime]


class StaleCacheWarning(UserWarning):
    """Raised as a warning when expired rows are served because a fetch failed."""


def _table_name(key: str) -> str:
    """A stable, SQL-safe table name for an arbitrary cache key."""
    return "payload_" + hashlib.sha1(key.encode()).hexdigest()[:16]


class Cache:
    """A persistent frame cache with a TTL and a stale-on-failure fallback.

    The clock is injected so TTL behavior is testable without sleeping.
    """

    def __init__(self, path: Path | str, now: Clock = dt.datetime.now) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self.path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache_entries ("
            "  key TEXT PRIMARY KEY,"
            "  table_name TEXT NOT NULL,"
            "  fetched_at TIMESTAMP NOT NULL"
            ")"
        )

    def close(self) -> None:
        self._conn.close()

    def fetched_at(self, key: str) -> dt.datetime | None:
        """When `key` was last written, or None if it was never written."""
        # fetchall, not fetchone: DuckDB holds the result open until it is
        # exhausted, and an open result blocks the next register() on this
        # connection.
        rows = self._conn.execute(
            "SELECT fetched_at FROM cache_entries WHERE key = ?", [key]
        ).fetchall()
        return rows[0][0] if rows else None

    def get_or_fetch(
        self,
        key: str,
        fetch: Callable[[], pd.DataFrame],
        ttl_hours: float = DEFAULT_TTL_HOURS,
    ) -> pd.DataFrame:
        """Return cached rows for `key` if fresh, otherwise call `fetch`.

        If `fetch` raises and expired rows exist, those rows are returned with
        a `StaleCacheWarning` — a dead upstream must not read as "no data",
        which downstream would score as a roster of zeros. If `fetch` raises
        with nothing cached, the error propagates.
        """
        written = self.fetched_at(key)
        age_hours = None if written is None else (self._now() - written).total_seconds() / 3600.0
        if age_hours is not None and age_hours < ttl_hours:
            return self._read(key)

        try:
            frame = fetch()
        except Exception:
            if written is None or age_hours is None:
                raise
            warnings.warn(
                f"upstream fetch for {key!r} failed; serving rows cached at {written} "
                f"(stale by {age_hours:.1f}h)",
                StaleCacheWarning,
                stacklevel=2,
            )
            return self._read(key)

        self._write(key, frame)
        return frame

    def _read(self, key: str) -> pd.DataFrame:
        return self._conn.execute(f'SELECT * FROM "{_table_name(key)}"').df()

    def _write(self, key: str, frame: pd.DataFrame) -> None:
        table = _table_name(key)
        self._conn.register("_incoming", frame)
        try:
            self._conn.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM _incoming')
            self._conn.execute(
                "INSERT OR REPLACE INTO cache_entries (key, table_name, fetched_at) "
                "VALUES (?, ?, ?)",
                [key, table, self._now()],
            )
        finally:
            self._conn.unregister("_incoming")
