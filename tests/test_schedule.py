import datetime as dt

import pandas as pd
import pytest

from capspace.data.ingestion import load_fixture, rows_to_schedule
from capspace.engine.schedule import (
    NBA_TEAM_COUNT,
    off_night_density,
    plays_on,
    weekly_game_counts,
)

WEEK_START = dt.date(2026, 1, 5)
WEEK_END = dt.date(2026, 1, 11)


@pytest.fixture
def sched() -> pd.DataFrame:
    return rows_to_schedule(load_fixture("nba_schedule")["rows"])


def frame(rows: list[tuple[dt.date, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["game_date", "team", "opponent"])


class TestWeeklyGameCounts:
    def test_counts_real_games_for_a_known_team_and_week(self, sched):
        assert weekly_game_counts(sched, WEEK_START, WEEK_END)["BOS"] == 4

    def test_covers_every_team(self, sched):
        assert len(weekly_game_counts(sched, WEEK_START, WEEK_END)) == NBA_TEAM_COUNT

    def test_window_start_is_inclusive(self):
        s = frame([(dt.date(2026, 1, 5), "BOS", "NYK")])
        assert weekly_game_counts(s, dt.date(2026, 1, 5), dt.date(2026, 1, 6))["BOS"] == 1

    def test_window_end_is_inclusive(self):
        s = frame([(dt.date(2026, 1, 6), "BOS", "NYK")])
        assert weekly_game_counts(s, dt.date(2026, 1, 5), dt.date(2026, 1, 6))["BOS"] == 1

    def test_games_outside_the_window_are_not_counted(self):
        s = frame([(dt.date(2026, 1, 20), "BOS", "NYK")])
        assert weekly_game_counts(s, dt.date(2026, 1, 5), dt.date(2026, 1, 6)).get("BOS", 0) == 0

    def test_a_team_with_no_games_in_the_window_counts_zero(self, sched):
        counts = weekly_game_counts(sched, dt.date(2026, 1, 5), dt.date(2026, 1, 5))
        assert counts["BOS"] >= 0
        assert sum(counts.values()) == 16  # 16 teams played on Jan 5


class TestOffNightDensity:
    def test_density_is_the_fraction_of_teams_idle(self, sched):
        day = dt.date(2026, 1, 5)  # 16 of 30 teams played
        assert off_night_density(sched, day, day)[day] == pytest.approx(14 / 30)

    def test_a_busier_day_has_lower_density(self, sched):
        busy = dt.date(2026, 1, 5)  # 16 teams
        quiet = dt.date(2026, 1, 6)  # 12 teams
        d = off_night_density(sched, busy, quiet)
        assert d[quiet] > d[busy]

    def test_a_day_with_no_games_has_density_one(self):
        day = dt.date(2026, 2, 14)
        s = frame([(dt.date(2026, 1, 5), "BOS", "NYK")])
        assert off_night_density(s, day, day)[day] == 1.0

    def test_denominator_is_thirty_not_the_teams_present(self):
        """A trimmed frame must not inflate density toward zero."""
        day = dt.date(2026, 1, 5)
        s = frame([(day, "BOS", "NYK"), (day, "NYK", "BOS")])
        assert off_night_density(s, day, day)[day] == pytest.approx(28 / 30)

    def test_returns_one_entry_per_day_in_the_window_inclusive(self, sched):
        d = off_night_density(sched, dt.date(2026, 1, 5), dt.date(2026, 1, 11))
        assert len(d) == 7
        assert min(d) == dt.date(2026, 1, 5)
        assert max(d) == dt.date(2026, 1, 11)


class TestPlaysOn:
    def test_true_when_the_team_has_a_game(self):
        s = frame([(dt.date(2026, 1, 5), "BOS", "NYK")])
        assert plays_on(s, "BOS", dt.date(2026, 1, 5)) is True

    def test_false_on_an_off_night(self):
        s = frame([(dt.date(2026, 1, 5), "BOS", "NYK")])
        assert plays_on(s, "BOS", dt.date(2026, 1, 6)) is False

    def test_false_for_a_team_not_in_the_schedule(self):
        s = frame([(dt.date(2026, 1, 5), "BOS", "NYK")])
        assert plays_on(s, "LAL", dt.date(2026, 1, 5)) is False

    def test_agrees_with_the_real_schedule(self, sched):
        day = dt.date(2026, 1, 5)
        playing = set(sched[sched["game_date"] == day]["team"].tolist())
        assert plays_on(sched, next(iter(playing)), day) is True
        idle = {"ATL", "BOS", "LAL", "NYK", "MIA"} - playing
        if idle:
            assert plays_on(sched, next(iter(idle)), day) is False
