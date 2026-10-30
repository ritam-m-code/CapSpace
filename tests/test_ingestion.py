import datetime as dt

import pandas as pd
import pytest

from capspace.data.ingestion import (
    fetch_game_logs,
    fetch_schedule,
    load_fixture,
    parse_minutes,
    rows_to_game_logs,
    rows_to_schedule,
)
from capspace.data.schemas import GameLog

# Recorded from the live API on 2026-10-29; see scripts in the plan's Task 3.
THIN_SAMPLE_PLAYER = 1631099  # Keegan Murray, 3 games in the window
NO_FREE_THROW_PLAYER = 1642877  # Micah Peavy, 14 games, 0 FTA


@pytest.fixture
def log_rows() -> list[dict]:
    return load_fixture("nba_gamelogs")["rows"]


@pytest.fixture
def sched_rows() -> list[dict]:
    return load_fixture("nba_schedule")["rows"]


class TestParseMinutes:
    def test_passes_through_a_float(self):
        assert parse_minutes(38.283333) == pytest.approx(38.283333)

    def test_parses_mm_ss_from_the_legacy_endpoint(self):
        assert parse_minutes("31:24") == pytest.approx(31.4, abs=0.01)

    def test_treats_none_as_a_did_not_play(self):
        assert parse_minutes(None) == 0.0

    def test_treats_blank_as_a_did_not_play(self):
        assert parse_minutes("") == 0.0


class TestGameLogParsing:
    def test_every_fixture_row_parses_into_a_model(self, log_rows):
        logs = rows_to_game_logs(log_rows)
        assert len(logs) == len(log_rows)
        assert all(isinstance(entry, GameLog) for entry in logs)

    def test_fixture_covers_enough_players_and_teams_to_standardize(self, log_rows):
        assert len({row["PLAYER_ID"] for row in log_rows}) == 10
        assert len({row["TEAM_ABBREVIATION"] for row in log_rows}) == 10

    def test_game_date_becomes_a_date_not_a_datetime(self, log_rows):
        assert isinstance(rows_to_game_logs(log_rows)[0].game_date, dt.date)

    def test_fixture_contains_a_thin_sample_player(self, log_rows):
        """Feeds Review Focus 2: shrinkage must dominate a tiny sample."""
        games = [r for r in log_rows if r["PLAYER_ID"] == THIN_SAMPLE_PLAYER]
        assert len(games) == 3

    def test_fixture_contains_a_player_with_no_free_throw_attempts(self, log_rows):
        """Feeds Review Focus 1: zero attempts must not produce NaN impact."""
        games = [r for r in log_rows if r["PLAYER_ID"] == NO_FREE_THROW_PLAYER]
        assert len(games) > 0
        assert sum(r["FTA"] for r in games) == 0

    def test_minutes_are_positive_for_players_who_appeared(self, log_rows):
        logs = rows_to_game_logs(log_rows)
        assert sum(1 for entry in logs if entry.minutes > 0) == len(logs)


class TestScheduleParsing:
    def test_schedule_has_exactly_the_three_expected_columns(self, sched_rows):
        assert list(rows_to_schedule(sched_rows).columns) == [
            "game_date",
            "team",
            "opponent",
        ]

    def test_home_matchups_split_on_vs(self):
        got = rows_to_schedule(
            [
                {
                    "TEAM_ABBREVIATION": "TOR",
                    "GAME_DATE": "2026-01-05T00:00:00",
                    "MATCHUP": "TOR vs. BKN",
                }
            ]
        )
        assert got.iloc[0]["team"] == "TOR"
        assert got.iloc[0]["opponent"] == "BKN"

    def test_away_matchups_split_on_at(self):
        got = rows_to_schedule(
            [
                {
                    "TEAM_ABBREVIATION": "PHX",
                    "GAME_DATE": "2026-01-05T00:00:00",
                    "MATCHUP": "PHX @ OKC",
                }
            ]
        )
        assert got.iloc[0]["team"] == "PHX"
        assert got.iloc[0]["opponent"] == "OKC"

    def test_every_team_appears(self, sched_rows):
        teams = set(rows_to_schedule(sched_rows)["team"].tolist())
        assert len(teams) == 30

    def test_game_dates_are_dates(self, sched_rows):
        dates = rows_to_schedule(sched_rows)["game_date"].tolist()
        assert all(isinstance(d, dt.date) for d in dates)


class TestFetchersUseTheCache:
    def test_game_logs_hit_the_upstream_once_then_cache(self, tmp_path, monkeypatch, log_rows):
        from capspace.data import ingestion
        from capspace.data.storage import Cache

        calls = []

        def fake_upstream(season: str) -> pd.DataFrame:
            calls.append(season)
            return pd.DataFrame(log_rows)

        monkeypatch.setattr(ingestion, "_upstream_game_logs", fake_upstream)
        cache = Cache(tmp_path / "c.duckdb")
        first = fetch_game_logs("2025-26", cache)
        second = fetch_game_logs("2025-26", cache)
        assert calls == ["2025-26"]
        assert len(first) == len(second) == len(log_rows)

    def test_schedule_hits_the_upstream_once_then_caches(self, tmp_path, monkeypatch, sched_rows):
        from capspace.data import ingestion
        from capspace.data.storage import Cache

        calls = []

        def fake_upstream(season: str) -> pd.DataFrame:
            calls.append(season)
            return pd.DataFrame(sched_rows)

        monkeypatch.setattr(ingestion, "_upstream_schedule", fake_upstream)
        cache = Cache(tmp_path / "c.duckdb")
        fetch_schedule("2025-26", cache)
        fetch_schedule("2025-26", cache)
        assert calls == ["2025-26"]

    def test_log_and_schedule_keys_do_not_collide(
        self, tmp_path, monkeypatch, log_rows, sched_rows
    ):
        from capspace.data import ingestion
        from capspace.data.storage import Cache

        monkeypatch.setattr(ingestion, "_upstream_game_logs", lambda s: pd.DataFrame(log_rows))
        monkeypatch.setattr(ingestion, "_upstream_schedule", lambda s: pd.DataFrame(sched_rows))
        cache = Cache(tmp_path / "c.duckdb")
        logs = fetch_game_logs("2025-26", cache)
        sched = fetch_schedule("2025-26", cache)
        assert len(logs) == len(log_rows)
        assert len(sched) == len(sched_rows)
