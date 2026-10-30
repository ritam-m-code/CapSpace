from datetime import date

import pytest
from pydantic import ValidationError

from capspace.data.schemas import CATEGORIES, POOL_SIZE, GameLog, LeagueSettings, Player


def a_log(**overrides) -> dict:
    """A valid GameLog payload; override one field to probe a single rule."""
    base = dict(
        nba_id=1,
        game_date=date(2026, 1, 1),
        minutes=30.0,
        pts=20,
        reb=5,
        ast=4,
        stl=1,
        blk=1,
        fg3m=2,
        fgm=8,
        fga=15,
        ftm=2,
        fta=2,
        tov=3,
    )
    return base | overrides


class TestConstants:
    def test_categories_are_the_nine_in_spec_order(self):
        assert CATEGORIES == (
            "PTS",
            "REB",
            "AST",
            "STL",
            "BLK",
            "3PM",
            "FG%",
            "FT%",
            "TO",
        )

    def test_pool_size_is_150(self):
        assert POOL_SIZE == 150


class TestGameLog:
    def test_accepts_a_valid_log(self):
        assert GameLog(**a_log()).minutes == 30.0

    def test_rejects_negative_minutes(self):
        with pytest.raises(ValidationError):
            GameLog(**a_log(minutes=-1.0))

    def test_accepts_zero_minutes_for_a_dnp(self):
        assert GameLog(**a_log(minutes=0.0)).minutes == 0.0

    def test_rejects_field_goals_made_exceeding_attempts(self):
        with pytest.raises(ValidationError, match="fgm"):
            GameLog(**a_log(fgm=9, fga=4))

    def test_rejects_free_throws_made_exceeding_attempts(self):
        with pytest.raises(ValidationError, match="ftm"):
            GameLog(**a_log(ftm=7, fta=3))

    def test_rejects_negative_counting_stats(self):
        with pytest.raises(ValidationError):
            GameLog(**a_log(reb=-2))

    def test_rejects_three_pointers_exceeding_field_goals_made(self):
        with pytest.raises(ValidationError, match="fg3m"):
            GameLog(**a_log(fg3m=9, fgm=4, fga=15))


class TestPlayer:
    def test_espn_fields_are_optional_for_an_unmatched_player(self):
        p = Player(nba_id=1, name="A", team="BOS", positions=["PG"], status="ACTIVE")
        assert p.espn_id is None and p.espn_rank is None

    def test_rejects_an_unknown_status(self):
        with pytest.raises(ValidationError):
            Player(nba_id=1, name="A", team="BOS", positions=["PG"], status="PROBABLY")  # type: ignore[arg-type]


class TestLeagueSettings:
    def test_round_trips_through_json(self):
        s = LeagueSettings(
            league_id=1,
            team_id=2,
            categories=list(CATEGORIES),
            roster_slots={"PG": 1, "BE": 3},
            acquisition_limit=7,
            waiver_days=1,
            lineup_lock="DAILY",
        )
        assert LeagueSettings.model_validate_json(s.model_dump_json()) == s

    def test_rejects_an_unknown_lineup_lock(self):
        with pytest.raises(ValidationError):
            LeagueSettings(
                league_id=1,
                team_id=2,
                categories=list(CATEGORIES),
                roster_slots={"PG": 1},
                acquisition_limit=7,
                waiver_days=1,
                lineup_lock="HOURLY",  # type: ignore[arg-type]
            )
