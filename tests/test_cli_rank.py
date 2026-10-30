from typing import Any, cast

import pandas as pd
import pytest
from typer.testing import CliRunner

from capspace.cli.main import app, spread

runner = CliRunner()


def val(series: Any, key: Any) -> float:
    """One element of a Series as a plain float, for comparisons."""
    return float(cast(float, series[key]))


@pytest.fixture(autouse=True)
def fixtures_mode(monkeypatch):
    """Run every CLI test against recorded fixtures, never the network."""
    monkeypatch.setenv("CAPSPACE_FIXTURES", "1")


def run(*args: str):
    return runner.invoke(app, ["rank", *args])


class TestSpread:
    def test_is_positive_when_espn_ranks_a_player_worse_than_the_model(self):
        assert val(spread(pd.Series({1: 5}), pd.Series({1: 40})), 1) == 35

    def test_is_negative_when_espn_ranks_a_player_better(self):
        assert val(spread(pd.Series({1: 40}), pd.Series({1: 5})), 1) == -35

    def test_is_zero_on_agreement(self):
        assert val(spread(pd.Series({1: 7}), pd.Series({1: 7})), 1) == 0

    def test_players_without_an_espn_rank_get_no_spread(self):
        assert bool(pd.isna(val(spread(pd.Series({1: 7}), pd.Series({1: None})), 1)))


class TestRankCommand:
    def test_exits_zero_and_prints_a_table(self):
        result = run()
        assert result.exit_code == 0
        assert "Model" in result.stdout
        assert "ESPN" in result.stdout
        assert "Spread" in result.stdout

    def test_includes_fixture_players(self):
        out = run().stdout
        assert "Maxey" in out
        assert "Miller" in out

    def test_sorted_by_spread_puts_the_biggest_underrate_first(self):
        """Brandon Miller is ESPN rank 30 but mid-pack by model value."""
        out = run("--sort-by", "spread").stdout
        assert out.index("Miller") < out.index("Dončić")

    def test_sorted_by_model_puts_the_best_player_first(self):
        out = run("--sort-by", "model").stdout
        assert out.index("Maxey") < out.index("Murray")

    def test_players_out_for_the_season_are_excluded_from_the_pool(self):
        """Review Focus 3 at the CLI boundary: Micah Peavy is OUT."""
        assert "Peavy" not in run("--limit", "20").stdout

    def test_limit_caps_the_row_count(self):
        names = ["Maxey", "Gilgeous", "Dončić", "Edwards", "Durant", "Miller"]
        out = run("--limit", "3").stdout
        assert sum(1 for n in names if n in out) == 3

    def test_zscore_view_shows_every_category_column(self):
        out = run("--view", "zscore").stdout
        for category in ("PTS", "REB", "AST", "STL", "BLK", "FG%", "FT%", "TO"):
            assert category in out

    def test_compact_view_omits_category_columns(self):
        out = run("--view", "compact").stdout
        assert "Spread" in out
        assert "STL" not in out

    def test_players_without_an_espn_rank_render_a_dash(self):
        out = run("--view", "compact", "--limit", "20").stdout
        assert "—" in out

    def test_window_flag_is_accepted(self):
        assert run("--window", "30d").exit_code == 0

    def test_an_unparseable_window_exits_nonzero_with_a_message(self):
        """typer writes usage errors to stderr, which `output` combines."""
        result = run("--window", "banana")
        assert result.exit_code != 0
        assert "window must look like" in result.output

    def test_an_unknown_view_exits_nonzero(self):
        assert run("--view", "hologram").exit_code != 0

    def test_an_unknown_sort_key_exits_nonzero(self):
        assert run("--sort-by", "vibes").exit_code != 0


class TestAuthFailure:
    def test_missing_credentials_exit_nonzero_naming_the_cookies(self, monkeypatch):
        """Review Focus 4 at the CLI boundary: a readable message, not a stack."""
        monkeypatch.delenv("CAPSPACE_FIXTURES", raising=False)
        monkeypatch.setenv("ESPN_S2", "")
        monkeypatch.setenv("ESPN_SWID", "")
        monkeypatch.setenv("ESPN_LEAGUE_ID", "1")
        monkeypatch.setenv("ESPN_TEAM_ID", "1")
        result = run()
        assert result.exit_code != 0
        assert "ESPN_S2" in result.stdout
