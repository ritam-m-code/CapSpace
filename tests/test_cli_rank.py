from typing import Any, cast

import pandas as pd
import pytest
from rich.console import Console
from typer.testing import CliRunner

from capspace.cli.main import _print, app, load_credentials, spread

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


def data_rows(stdout: str) -> list[list[str]]:
    """The table's body rows, split into cells.

    Parsed structurally rather than by player name, so re-recording the
    fixtures from a live league does not break these tests.
    """
    rows = []
    for line in stdout.splitlines():
        if line.startswith("│"):
            cells = [c.strip() for c in line.strip("│").split("│")]
            if cells and cells[0] not in {"Player", ""}:
                rows.append(cells)
    return rows


def column(stdout: str, index: int) -> list[str]:
    return [row[index] for row in data_rows(stdout)]


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

    def test_sorted_by_spread_is_non_increasing(self):
        """The biggest under-rate first: ESPN ranks them worst relative to
        the model, so they are the cheapest to acquire."""
        out = run("--sort-by", "spread").stdout
        spreads = [int(c.replace("+", "")) for c in column(out, 5) if c != "—"]
        assert spreads == sorted(spreads, reverse=True)
        assert len(spreads) >= 5

    def test_sorted_by_model_is_ascending_from_rank_one(self):
        out = run("--sort-by", "model").stdout
        models = [int(c) for c in column(out, 3)]
        assert models == sorted(models)
        assert models[0] == min(models)

    def test_players_out_for_the_season_are_excluded_from_the_pool(self):
        """Review Focus 3 at the CLI boundary: Micah Peavy is OUT."""
        assert "Peavy" not in run("--limit", "20").stdout

    def test_limit_caps_the_row_count(self):
        assert len(data_rows(run("--limit", "3").stdout)) == 3

    def test_every_row_carries_a_model_rank(self):
        out = run("--limit", "20").stdout
        assert all(c.isdigit() for c in column(out, 3))

    def test_zscore_view_shows_every_category_column(self):
        out = run("--view", "zscore").stdout
        for category in ("PTS", "REB", "AST", "STL", "BLK", "FG%", "FT%", "TO"):
            assert category in out

    def test_compact_view_omits_category_columns(self):
        out = run("--view", "compact").stdout
        assert "Spread" in out
        assert "STL" not in out

    def test_an_unranked_player_renders_a_dash_in_both_espn_columns(self):
        """Whether the live fixture contains an unranked player is not ours
        to control, so the rendering is asserted directly."""
        frame = pd.DataFrame(
            {
                "Player": ["Nobody"],
                "Team": ["BOS"],
                "Pos": ["PG"],
                "Model": [1],
                "ESPN": [float("nan")],
                "Spread": [float("nan")],
            }
        )
        console = Console(record=True, width=80)
        _print(frame, "compact", out=console)
        text = console.export_text()
        assert text.count("—") == 2

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


class TestCredentialLoading:
    def test_reads_credentials_from_a_dotenv_file(self, tmp_path, monkeypatch):
        """The four values live in .env, not in the shell environment."""
        for key in ("ESPN_S2", "ESPN_SWID", "ESPN_LEAGUE_ID", "ESPN_TEAM_ID"):
            monkeypatch.delenv(key, raising=False)
        env = tmp_path / ".env"
        env.write_text(
            "ESPN_S2='abc%3Ddef'\nESPN_SWID='{AAA-BBB}'\nESPN_LEAGUE_ID=1234567\nESPN_TEAM_ID=4\n"
        )
        creds = load_credentials(env)
        assert creds.s2 == "abc%3Ddef"
        assert creds.swid == "{AAA-BBB}"
        assert creds.league_id == 1234567
        assert creds.team_id == 4

    def test_the_shell_environment_wins_over_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ESPN_S2", "from-shell")
        monkeypatch.setenv("ESPN_SWID", "{shell}")
        monkeypatch.setenv("ESPN_LEAGUE_ID", "999")
        monkeypatch.setenv("ESPN_TEAM_ID", "1")
        env = tmp_path / ".env"
        env.write_text("ESPN_S2='from-file'\nESPN_SWID='{file}'\n")
        assert load_credentials(env).s2 == "from-shell"

    def test_an_empty_cookie_raises_naming_the_variable(self, tmp_path, monkeypatch):
        for key in ("ESPN_S2", "ESPN_SWID", "ESPN_LEAGUE_ID", "ESPN_TEAM_ID"):
            monkeypatch.delenv(key, raising=False)
        env = tmp_path / ".env"
        env.write_text("ESPN_S2=''\nESPN_SWID='{AAA}'\nESPN_LEAGUE_ID=1\nESPN_TEAM_ID=1\n")
        with pytest.raises(ValueError, match="ESPN_S2"):
            load_credentials(env)

    def test_a_missing_file_is_not_an_error_when_the_shell_has_the_values(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("ESPN_S2", "s")
        monkeypatch.setenv("ESPN_SWID", "{w}")
        monkeypatch.setenv("ESPN_LEAGUE_ID", "1")
        monkeypatch.setenv("ESPN_TEAM_ID", "2")
        assert load_credentials(tmp_path / "absent.env").league_id == 1
