import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from capspace.data.storage import Cache, StaleCacheWarning


class FakeClock:
    """A hand-advanced clock, so TTL behavior is tested without sleeping."""

    def __init__(self, start: dt.datetime) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, hours: float) -> None:
        self.now += dt.timedelta(hours=hours)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(dt.datetime(2026, 10, 29, 12, 0, 0))


@pytest.fixture
def cache(tmp_path: Path, clock: FakeClock) -> Cache:
    return Cache(tmp_path / "test.duckdb", now=clock)


def counting_fetch(
    frame: pd.DataFrame,
) -> tuple[Callable[[], pd.DataFrame], list[int]]:
    """A fetch callable plus the list recording how often it ran."""
    calls: list[int] = []

    def fetch() -> pd.DataFrame:
        calls.append(1)
        return frame

    return fetch, calls


def boom() -> pd.DataFrame:
    raise ConnectionError("upstream down")


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame({"a": [1, 2], "b": [1.5, 2.5]})


class TestFreshness:
    def test_first_call_fetches(self, cache: Cache, frame: pd.DataFrame):
        fetch, calls = counting_fetch(frame)
        got = cache.get_or_fetch("k", fetch)
        assert len(calls) == 1
        pd.testing.assert_frame_equal(got, frame)

    def test_second_call_within_ttl_serves_cache(self, cache: Cache, frame: pd.DataFrame):
        fetch, calls = counting_fetch(frame)
        cache.get_or_fetch("k", fetch)
        got = cache.get_or_fetch("k", fetch)
        assert len(calls) == 1
        pd.testing.assert_frame_equal(got, frame)

    def test_still_fresh_just_under_twelve_hours(self, cache, clock, frame):
        fetch, calls = counting_fetch(frame)
        cache.get_or_fetch("k", fetch)
        clock.advance(11.9)
        cache.get_or_fetch("k", fetch)
        assert len(calls) == 1

    def test_refetches_once_ttl_expires(self, cache, clock, frame):
        fetch, calls = counting_fetch(frame)
        cache.get_or_fetch("k", fetch)
        clock.advance(13)
        cache.get_or_fetch("k", fetch)
        assert len(calls) == 2

    def test_ttl_is_overridable_per_call(self, cache, clock, frame):
        fetch, calls = counting_fetch(frame)
        cache.get_or_fetch("k", fetch, ttl_hours=1.0)
        clock.advance(2)
        cache.get_or_fetch("k", fetch, ttl_hours=1.0)
        assert len(calls) == 2

    def test_distinct_keys_do_not_collide(self, cache, frame):
        cache.get_or_fetch("one", lambda: frame)
        got = cache.get_or_fetch("two", lambda: pd.DataFrame({"a": [9], "b": [9.0]}))
        assert got["a"].tolist() == [9]
        assert cache.get_or_fetch("one", boom)["a"].tolist() == [1, 2]


class TestStaleFallback:
    def test_serves_stale_rows_with_a_warning_when_fetch_fails(self, cache, clock, frame):
        """Review Focus 5: a dead upstream must not read as 'no data'."""
        cache.get_or_fetch("k", lambda: frame)
        clock.advance(13)
        with pytest.warns(StaleCacheWarning, match="k"):
            got = cache.get_or_fetch("k", boom)
        pd.testing.assert_frame_equal(got, frame)

    def test_propagates_the_error_when_no_stale_rows_exist(self, cache):
        with pytest.raises(ConnectionError):
            cache.get_or_fetch("k", boom)

    def test_a_fresh_hit_never_warns(self, cache, frame):
        cache.get_or_fetch("k", lambda: frame)
        cache.get_or_fetch("k", boom)  # filterwarnings=error would fail this on a warn


class TestPersistence:
    def test_cache_survives_reopening_the_file(self, tmp_path, clock, frame):
        path = tmp_path / "test.duckdb"
        Cache(path, now=clock).get_or_fetch("k", lambda: frame)
        got = Cache(path, now=clock).get_or_fetch("k", boom)
        pd.testing.assert_frame_equal(got, frame)

    def test_date_columns_come_back_as_datetime64(self, cache):
        """Pinned, not accidental: callers must normalize dates on parse."""
        frame = pd.DataFrame({"d": [dt.date(2026, 1, 1)]})
        cache.get_or_fetch("k", lambda: frame)
        got = cache.get_or_fetch("k", boom)
        assert str(got["d"].dtype).startswith("datetime64")
        assert got["d"].dt.date.tolist() == [dt.date(2026, 1, 1)]

    def test_empty_frames_round_trip(self, cache):
        empty = pd.DataFrame({"a": pd.Series(dtype="int64")})
        cache.get_or_fetch("k", lambda: empty)
        assert len(cache.get_or_fetch("k", boom)) == 0
