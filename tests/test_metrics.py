import datetime as dt
from typing import Any, cast

import pandas as pd
import pytest

from capspace.data.ingestion import load_fixture, rows_to_game_logs
from capspace.data.schemas import CATEGORIES, POOL_SIZE, Player
from capspace.engine.metrics import (
    category_zscores,
    eligible_pool,
    model_value,
    pct_impact,
    pool_rate,
)
from capspace.engine.projections import project, projected_minutes

ASOF = dt.date(2026, 2, 1)
NO_FREE_THROW_PLAYER = 1642877  # Micah Peavy


def val(series: Any, key: Any) -> float:
    """One element of a Series as a plain float, for comparisons."""
    return float(cast(float, series[key]))


def proj_frame(rows: dict[int, dict[str, float]]) -> pd.DataFrame:
    """A projection frame with unnamed stats zeroed."""
    zeros = dict.fromkeys(
        ["pts", "reb", "ast", "stl", "blk", "fg3m", "fgm", "fga", "ftm", "fta", "tov"], 0.0
    )
    return pd.DataFrame.from_dict(
        {pid: zeros | stats for pid, stats in rows.items()}, orient="index"
    )


def player(nba_id: int, status: str = "ACTIVE", rank: int | None = None) -> Player:
    return Player(
        nba_id=nba_id,
        name=f"P{nba_id}",
        team="BOS",
        positions=["PG"],
        status=status,  # type: ignore[arg-type]
        espn_id=nba_id,
        espn_rank=rank,
    )


@pytest.fixture
def real_proj() -> pd.DataFrame:
    logs = rows_to_game_logs(load_fixture("nba_gamelogs")["rows"])
    return project(logs, window_days=400, asof=ASOF)


class TestPoolRate:
    def test_is_volume_weighted_not_a_mean_of_rates(self):
        """One attempt at 100% must not drag the pool rate to 75%."""
        proj = proj_frame({1: {"ftm": 1, "fta": 1}, 2: {"ftm": 5, "fta": 10}})
        assert pool_rate(proj, "FT") == pytest.approx(6 / 11)

    def test_returns_zero_when_the_pool_took_no_attempts(self):
        assert pool_rate(proj_frame({1: {}}), "FT") == 0.0


class TestPctImpact:
    def test_impact_is_rate_delta_times_attempts(self):
        proj = proj_frame({1: {"fgm": 16, "fga": 20}, 2: {"fgm": 4, "fga": 20}})
        pool = pool_rate(proj, "FG")  # 20/40 = 0.5
        assert val(pct_impact(proj, "FG"), 1) == pytest.approx((0.80 - pool) * 20)

    def test_zero_attempts_give_zero_impact_not_nan(self):
        """Review Focus 1."""
        proj = proj_frame({1: {"ftm": 0, "fta": 0}, 2: {"ftm": 8, "fta": 10}})
        impact = pct_impact(proj, "FT")
        assert val(impact, 1) == 0.0
        assert not bool(impact.isna().to_numpy().any())

    def test_a_below_average_shooter_has_negative_impact(self):
        proj = proj_frame({1: {"fgm": 8, "fga": 20}, 2: {"fgm": 12, "fga": 20}})
        assert val(pct_impact(proj, "FG"), 1) < 0

    def test_real_fixture_has_no_nan_impact(self, real_proj):
        for kind in ("FG", "FT"):
            assert not bool(pct_impact(real_proj, kind).isna().to_numpy().any())

    def test_the_zero_attempt_player_in_the_real_fixture_scores_zero(self, real_proj):
        assert val(pct_impact(real_proj, "FT"), NO_FREE_THROW_PLAYER) == 0.0


class TestCategoryZscores:
    def test_columns_are_exactly_the_nine_categories(self, real_proj):
        z = category_zscores(real_proj, real_proj.index)
        assert tuple(z.columns) == CATEGORIES

    def test_turnovers_are_inverted_so_fewer_is_better(self):
        proj = proj_frame({1: {"tov": 1.0}, 2: {"tov": 5.0}})
        z = category_zscores(proj, proj.index)
        assert float(z.loc[1, "TO"]) > float(z.loc[2, "TO"])

    def test_percentage_categories_use_impact_not_rate(self):
        """The spec's headline correction, and the single most common error in
        public rankings: a 90% shooter on one attempt must not outrank an
        80% shooter on ten."""
        proj = proj_frame(
            {
                1: {"ftm": 0.9, "fta": 1.0},  # 90% on 1 attempt
                2: {"ftm": 8.0, "fta": 10.0},  # 80% on 10 attempts
                3: {"ftm": 6.0, "fta": 9.0},  # filler to give the pool spread
            }
        )
        z = category_zscores(proj, proj.index)
        assert float(z.loc[2, "FT%"]) > float(z.loc[1, "FT%"])

    def test_a_high_volume_average_shooter_is_near_zero(self):
        proj = proj_frame(
            {1: {"fgm": 5, "fga": 10}, 2: {"fgm": 5, "fga": 10}, 3: {"fgm": 2, "fga": 10}}
        )
        z = category_zscores(proj, proj.index)
        assert abs(float(z.loc[1, "FG%"]) - float(z.loc[2, "FG%"])) < 1e-9

    def test_counting_categories_are_standardized(self):
        proj = proj_frame({1: {"pts": 10.0}, 2: {"pts": 20.0}, 3: {"pts": 30.0}})
        z = category_zscores(proj, proj.index)
        assert float(cast(float, z["PTS"].mean())) == pytest.approx(0.0)
        assert z.loc[2, "PTS"] == pytest.approx(0.0)
        assert float(z.loc[3, "PTS"]) > 0 > float(z.loc[1, "PTS"])

    def test_a_constant_category_is_zero_not_nan(self):
        proj = proj_frame({1: {"blk": 2.0}, 2: {"blk": 2.0}})
        z = category_zscores(proj, proj.index)
        assert z["BLK"].tolist() == [0.0, 0.0]

    def test_standardization_uses_only_the_pool(self):
        """A sub-pool scrub must not shift the pool's mean."""
        proj = proj_frame({1: {"pts": 20.0}, 2: {"pts": 30.0}, 99: {"pts": 0.0}})
        pool = pd.Index([1, 2])
        z = category_zscores(proj, pool)
        assert z.index.tolist() == [1, 2]
        assert float(cast(float, z["PTS"].mean())) == pytest.approx(0.0)

    def test_no_nan_on_the_real_fixture(self, real_proj):
        z = category_zscores(real_proj, real_proj.index)
        assert not bool(z.isna().to_numpy().any())


class TestEligiblePool:
    def test_excludes_out_and_inactive_players(self):
        """Review Focus 3."""
        proj = proj_frame({1: {"pts": 20.0}, 2: {"pts": 25.0}, 3: {"pts": 30.0}})
        minutes = pd.Series({1: 30.0, 2: 30.0, 3: 30.0})
        players = [player(1), player(2, status="OUT"), player(3, status="INACTIVE")]
        pool = eligible_pool(players, proj, minutes)
        assert pool.tolist() == [1]

    def test_excludes_players_projecting_no_minutes(self):
        proj = proj_frame({1: {"pts": 20.0}, 2: {"pts": 0.0}})
        minutes = pd.Series({1: 30.0, 2: 0.0})
        pool = eligible_pool([player(1), player(2)], proj, minutes)
        assert pool.tolist() == [1]

    def test_keeps_day_to_day_players(self):
        proj = proj_frame({1: {"pts": 20.0}})
        minutes = pd.Series({1: 30.0})
        assert eligible_pool([player(1, status="DTD")], proj, minutes).tolist() == [1]

    def test_caps_the_pool_at_size_by_descending_value(self):
        proj = proj_frame({i: {"pts": float(i)} for i in range(1, 11)})
        minutes = pd.Series({i: 30.0 for i in range(1, 11)})
        players = [player(i) for i in range(1, 11)]
        pool = eligible_pool(players, proj, minutes, size=3)
        assert sorted(pool.tolist()) == [8, 9, 10]

    def test_default_size_is_the_spec_pool(self):
        proj = proj_frame({i: {"pts": float(i)} for i in range(1, 11)})
        minutes = pd.Series({i: 30.0 for i in range(1, 11)})
        players = [player(i) for i in range(1, 11)]
        assert len(eligible_pool(players, proj, minutes)) == 10 <= POOL_SIZE

    def test_ignores_players_absent_from_the_projection(self):
        proj = proj_frame({1: {"pts": 20.0}})
        minutes = pd.Series({1: 30.0})
        pool = eligible_pool([player(1), player(404)], proj, minutes)
        assert pool.tolist() == [1]


class TestModelValue:
    def test_is_the_sum_across_categories(self):
        z = pd.DataFrame({c: [1.0] for c in CATEGORIES}, index=[1])
        assert val(model_value(z), 1) == pytest.approx(9.0)

    def test_orders_the_real_fixture_sensibly(self, real_proj):
        logs = rows_to_game_logs(load_fixture("nba_gamelogs")["rows"])
        minutes = projected_minutes(logs, window_days=400, asof=ASOF)
        players = [player(int(i)) for i in real_proj.index]
        pool = eligible_pool(players, real_proj, minutes)
        value = model_value(category_zscores(real_proj, pool))
        assert value.idxmax() != NO_FREE_THROW_PLAYER
        assert value.idxmin() == NO_FREE_THROW_PLAYER
