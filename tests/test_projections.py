import datetime as dt

import pandas as pd
import pytest

from capspace.data.ingestion import load_fixture, rows_to_game_logs
from capspace.data.schemas import GameLog
from capspace.engine.projections import (
    DEFAULT_SHRINKAGE_K,
    per_minute_rates,
    project,
    projected_minutes,
    shrink_to_prior,
)

ASOF = dt.date(2026, 2, 1)

THIN_SAMPLE_PLAYER = 1631099  # Keegan Murray, 3 games in the fixture window
NO_FREE_THROW_PLAYER = 1642877  # Micah Peavy, 14 games, 0 FTA


def log(nba_id: int, days_ago: int, minutes: float, **stats) -> GameLog:
    """A log `days_ago` before ASOF, with unnamed stats zeroed."""
    base = dict(pts=0, reb=0, ast=0, stl=0, blk=0, fg3m=0, fgm=0, fga=0, ftm=0, fta=0, tov=0)
    return GameLog(
        nba_id=nba_id,
        game_date=ASOF - dt.timedelta(days=days_ago),
        minutes=minutes,
        **(base | stats),
    )


@pytest.fixture
def real_logs() -> list[GameLog]:
    return rows_to_game_logs(load_fixture("nba_gamelogs")["rows"])


class TestPerMinuteRates:
    def test_divides_window_totals_by_window_minutes(self):
        logs = [
            log(1, days_ago=1, minutes=30.0, pts=30),
            log(1, days_ago=2, minutes=10.0, pts=10),
        ]
        assert per_minute_rates(logs, asof=ASOF).loc[1, "pts"] == pytest.approx(1.0)

    def test_is_a_pooled_rate_not_a_mean_of_per_game_rates(self):
        """20 pts in 50 min is 0.4/min, not the 0.625 mean of 1.0 and 0.25."""
        logs = [
            log(1, days_ago=1, minutes=10.0, pts=10),
            log(1, days_ago=2, minutes=40.0, pts=10),
        ]
        assert per_minute_rates(logs, asof=ASOF).loc[1, "pts"] == pytest.approx(0.4)

    def test_excludes_games_older_than_the_window(self):
        logs = [
            log(1, days_ago=1, minutes=10.0, pts=10),
            log(1, days_ago=20, minutes=10.0, pts=400),
        ]
        assert per_minute_rates(logs, window_days=14, asof=ASOF).loc[1, "pts"] == pytest.approx(1.0)

    def test_window_boundary_is_inclusive(self):
        logs = [log(1, days_ago=14, minutes=10.0, pts=20)]
        assert per_minute_rates(logs, window_days=14, asof=ASOF).loc[1, "pts"] == pytest.approx(2.0)

    def test_zero_minute_games_do_not_enter_the_denominator(self):
        logs = [
            log(1, days_ago=1, minutes=20.0, pts=20),
            log(1, days_ago=2, minutes=0.0),
        ]
        assert per_minute_rates(logs, asof=ASOF).loc[1, "pts"] == pytest.approx(1.0)

    def test_a_player_with_no_window_games_is_absent(self):
        logs = [log(1, days_ago=40, minutes=30.0, pts=30)]
        assert 1 not in per_minute_rates(logs, window_days=14, asof=ASOF).index

    def test_covers_every_counting_stat(self, real_logs):
        rates = per_minute_rates(real_logs, window_days=400, asof=ASOF)
        for column in (
            "pts",
            "reb",
            "ast",
            "stl",
            "blk",
            "fg3m",
            "fgm",
            "fga",
            "ftm",
            "fta",
            "tov",
        ):
            assert column in rates.columns


class TestShrinkage:
    def test_weight_is_minutes_over_minutes_plus_k(self):
        window = pd.DataFrame({"pts": [1.0]}, index=[1])
        prior = pd.DataFrame({"pts": [0.5]}, index=[1])
        minutes = pd.Series([200.0], index=[1])
        got = shrink_to_prior(window, prior, minutes, k=200.0)
        assert got.loc[1, "pts"] == pytest.approx(0.75)  # w = 200/400 = 0.5

    def test_heavy_minutes_barely_shrink(self):
        window = pd.DataFrame({"pts": [1.0]}, index=[1])
        prior = pd.DataFrame({"pts": [0.5]}, index=[1])
        minutes = pd.Series([1800.0], index=[1])
        got = shrink_to_prior(window, prior, minutes, k=200.0)
        assert got.loc[1, "pts"] == pytest.approx(0.95)  # w = 1800/2000 = 0.9

    def test_thin_sample_is_dominated_by_the_prior(self):
        """Review Focus 2: 1-4 games must not set a player's rates."""
        window = pd.DataFrame({"pts": [1.0]}, index=[1])
        prior = pd.DataFrame({"pts": [0.5]}, index=[1])
        minutes = pd.Series([40.0], index=[1])  # ~2 games
        got = float(shrink_to_prior(window, prior, minutes, k=200.0).loc[1, "pts"])
        assert abs(got - 0.5) < 0.1
        assert abs(got - 0.5) * 4 < abs(got - 1.0)

    def test_zero_minutes_returns_the_prior_exactly(self):
        window = pd.DataFrame({"pts": [1.0]}, index=[1])
        prior = pd.DataFrame({"pts": [0.5]}, index=[1])
        got = shrink_to_prior(window, prior, pd.Series([0.0], index=[1]), k=200.0)
        assert got.loc[1, "pts"] == pytest.approx(0.5)

    def test_default_k_is_documented(self):
        assert DEFAULT_SHRINKAGE_K == 200.0

    def test_a_player_missing_from_the_prior_keeps_the_window_rate(self):
        window = pd.DataFrame({"pts": [1.0]}, index=[1])
        prior = pd.DataFrame({"pts": []}, index=pd.Index([], dtype="int64"))
        got = shrink_to_prior(window, prior, pd.Series([200.0], index=[1]), k=200.0)
        assert got.loc[1, "pts"] == pytest.approx(1.0)


class TestProjectedMinutes:
    def test_weights_recent_games_above_older_ones(self):
        """A role change three games ago must move the estimate."""
        logs = [log(1, days_ago=13 - i, minutes=10.0) for i in range(10)]
        logs += [log(1, days_ago=3 - i, minutes=34.0) for i in range(3)]
        flat_mean = (10 * 10.0 + 3 * 34.0) / 13
        assert projected_minutes(logs, window_days=14, asof=ASOF)[1] > flat_mean

    def test_a_shrinking_role_projects_below_the_flat_mean(self):
        logs = [log(1, days_ago=13 - i, minutes=34.0) for i in range(10)]
        logs += [log(1, days_ago=3 - i, minutes=10.0) for i in range(3)]
        flat_mean = (10 * 34.0 + 3 * 10.0) / 13
        assert projected_minutes(logs, window_days=14, asof=ASOF)[1] < flat_mean

    def test_a_stable_role_projects_at_its_level(self):
        logs = [log(1, days_ago=13 - i, minutes=28.0) for i in range(12)]
        assert projected_minutes(logs, window_days=14, asof=ASOF)[1] == pytest.approx(28.0)

    def test_did_not_play_games_are_excluded_not_averaged_as_zero(self):
        logs = [log(1, days_ago=5, minutes=30.0), log(1, days_ago=4, minutes=0.0)]
        assert projected_minutes(logs, window_days=14, asof=ASOF)[1] == pytest.approx(30.0)

    def test_real_fixture_minutes_are_plausible(self, real_logs):
        mins = projected_minutes(real_logs, window_days=400, asof=ASOF)
        assert mins.between(0, 48).all()
        assert mins.max() > 25


class TestProject:
    def test_multiplies_shrunk_rates_by_projected_minutes(self):
        """For a stable player the prior equals the window rate, so shrinkage
        is a no-op and the product must be exact."""
        logs = [log(1, days_ago=13 - i, minutes=30.0, pts=30) for i in range(12)]
        proj = project(logs, window_days=14, asof=ASOF)
        mins = projected_minutes(logs, window_days=14, asof=ASOF)
        rates = per_minute_rates(logs, window_days=14, asof=ASOF)
        assert proj.loc[1, "pts"] == pytest.approx(float(rates.loc[1, "pts"]) * mins[1])
        assert proj.loc[1, "pts"] == pytest.approx(30.0)

    def test_output_is_per_game_not_per_window(self):
        logs = [log(1, days_ago=13 - i, minutes=30.0, pts=30) for i in range(12)]
        assert project(logs, window_days=14, asof=ASOF).loc[1, "pts"] < 60

    def test_real_fixture_projects_every_player(self, real_logs):
        proj = project(real_logs, window_days=400, asof=ASOF)
        assert len(proj) == 10
        assert THIN_SAMPLE_PLAYER in proj.index
        assert NO_FREE_THROW_PLAYER in proj.index

    def test_no_projection_is_nan(self, real_logs):
        assert not project(real_logs, window_days=400, asof=ASOF).isna().to_numpy().any()

    def test_makes_never_exceed_attempts_in_projection(self, real_logs):
        proj = project(real_logs, window_days=400, asof=ASOF)
        assert (proj["fgm"] <= proj["fga"] + 1e-9).all()
        assert (proj["ftm"] <= proj["fta"] + 1e-9).all()

    def test_the_zero_free_throw_player_projects_zero_attempts(self, real_logs):
        """Feeds Review Focus 1 downstream in metrics."""
        proj = project(real_logs, window_days=400, asof=ASOF)
        assert proj.loc[NO_FREE_THROW_PLAYER, "fta"] == pytest.approx(0.0)
