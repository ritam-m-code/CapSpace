"""ESPN read-path tests.

The client is faked with stand-ins that mirror `espn_api.basketball`
attribute names, verified by inspecting the installed package. The raw
`kona_player_info` field paths used for player rank are NOT yet verified
against a live private league — see the plan's handoff notes.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from capspace.data.espn import (
    ATOMIC_POSITIONS,
    EspnAuthError,
    EspnCreds,
    match_players_by_name,
    normalize_name,
    read_free_agents,
    read_player_ranks,
    read_rosters,
    read_settings,
    to_status,
)
from capspace.data.ingestion import load_fixture
from capspace.data.schemas import CATEGORIES


@dataclass
class FakeSettings:
    acquisition_limit: int | None = 7
    waiver_process_days: list[str] = field(default_factory=lambda: ["MON"])
    scoring_type: str | None = "H2H_CATEGORY"
    name: str = "Test League"


@dataclass
class FakePlayer:
    name: str
    playerId: int
    proTeam: str = "BOS"
    position: str = "PG"
    eligibleSlots: list[str] = field(default_factory=lambda: ["PG", "G", "UTIL", "BE"])
    injuryStatus: str = "ACTIVE"
    injured: bool = False


@dataclass
class FakeTeam:
    team_id: int
    roster: list[FakePlayer]


@dataclass
class FakeRequest:
    payload: dict[str, Any] | None = None
    raises: Exception | None = None
    last_headers: dict[str, str] | None = None

    def league_get(self, params=None, headers=None):  # noqa: ANN001
        self.last_headers = headers
        if self.raises is not None:
            raise self.raises
        return self.payload or {}


@dataclass
class FakeLeague:
    league_id: int = 1234567
    year: int = 2027
    settings: FakeSettings = field(default_factory=FakeSettings)
    teams: list[FakeTeam] = field(default_factory=list)
    _free_agents: list[FakePlayer] = field(default_factory=list)
    espn_request: FakeRequest = field(default_factory=FakeRequest)
    settings_payload: dict[str, Any] = field(default_factory=dict)

    def free_agents(self, size: int = 50, **kwargs):  # noqa: ANN001
        return self._free_agents[:size]


def a_league() -> FakeLeague:
    return FakeLeague(
        teams=[
            FakeTeam(1, [FakePlayer("Luka Doncic", 3945274), FakePlayer("Jayson Tatum", 4065648)]),
            FakeTeam(2, [FakePlayer("Nikola Jokic", 3112335)]),
        ],
        _free_agents=[FakePlayer("Micah Peavy", 5106400, injuryStatus="OUT")],
    )


class TestCreds:
    def test_creds_are_frozen(self):
        creds = EspnCreds(s2="a", swid="{b}", league_id=1, team_id=2)
        with pytest.raises(AttributeError):
            creds.s2 = "c"  # type: ignore[misc]

    def test_missing_cookies_are_rejected_early(self):
        with pytest.raises(ValueError, match="ESPN_S2"):
            EspnCreds(s2="", swid="{b}", league_id=1, team_id=2)


class TestStatusMapping:
    @pytest.mark.parametrize(
        ("espn_status", "expected"),
        [
            ("ACTIVE", "ACTIVE"),
            ("DAY_TO_DAY", "DTD"),
            ("QUESTIONABLE", "DTD"),
            ("DOUBTFUL", "DTD"),
            ("OUT", "OUT"),
            ("SUSPENSION", "OUT"),
            ("INJURY_RESERVE", "INACTIVE"),
            ("FIVE_DAY_DNP", "INACTIVE"),
        ],
    )
    def test_maps_espn_statuses_onto_the_schema(self, espn_status, expected):
        assert to_status(espn_status) == expected

    def test_an_unknown_status_is_treated_as_active_not_dropped(self):
        """A new ESPN status string must not silently remove a healthy player."""
        assert to_status("SOMETHING_NEW") == "ACTIVE"

    def test_none_is_active(self):
        assert to_status(None) == "ACTIVE"


class TestReadSettings:
    def test_reads_the_acquisition_limit_from_the_api(self):
        s = read_settings(a_league())
        assert s.acquisition_limit == 7

    def test_defaults_an_absent_acquisition_limit_to_unlimited(self):
        league = a_league()
        league.settings.acquisition_limit = None
        assert read_settings(league).acquisition_limit == 0

    def test_categories_come_from_the_schema_order(self):
        assert read_settings(a_league()).categories == list(CATEGORIES)

    def test_waiver_days_is_the_count_of_process_days(self):
        assert read_settings(a_league()).waiver_days == 1

    def test_league_and_team_ids_are_carried_through(self):
        s = read_settings(a_league(), team_id=4)
        assert s.league_id == 1234567
        assert s.team_id == 4


class TestReadRosters:
    def test_returns_players_keyed_by_team_id(self):
        rosters = read_rosters(a_league())
        assert set(rosters) == {1, 2}
        assert len(rosters[1]) == 2

    def test_players_carry_their_espn_id(self):
        assert all(p.espn_id is not None for p in read_rosters(a_league())[1])

    def test_nba_id_is_absent_until_the_crosswalk_runs(self):
        """ESPN ids are not NBA ids; nba_id is 0 as a sentinel until matched."""
        assert read_rosters(a_league())[1][0].nba_id == 0

    def test_status_is_mapped_not_passed_through(self):
        league = a_league()
        league.teams[0].roster[0].injuryStatus = "DAY_TO_DAY"
        assert read_rosters(league)[1][0].status == "DTD"

    def test_expired_cookies_raise_a_named_error(self):
        """Review Focus 4: an auth failure must not read as an empty roster."""
        league = a_league()
        league.teams = []
        with pytest.raises(EspnAuthError, match="ESPN_S2"):
            read_rosters(league)

    def test_the_error_message_names_both_cookies_and_the_fix(self):
        league = a_league()
        league.teams = []
        with pytest.raises(EspnAuthError) as excinfo:
            read_rosters(league)
        message = str(excinfo.value)
        assert "ESPN_SWID" in message
        assert "SETUP_CREDENTIALS.md" in message


class TestReadFreeAgents:
    def test_returns_the_free_agent_pool(self):
        agents = read_free_agents(a_league())
        assert [p.name for p in agents] == ["Micah Peavy"]

    def test_free_agents_exclude_rostered_players(self):
        league = a_league()
        rostered = {p.espn_id for ps in read_rosters(league).values() for p in ps}
        assert not {p.espn_id for p in read_free_agents(league)} & rostered

    def test_free_agent_status_is_mapped(self):
        assert read_free_agents(a_league())[0].status == "OUT"


class TestReadPlayerRanks:
    """Shapes below are recorded from a live private league on 2026-10-29."""

    def ratings_payload(self, rows: list[tuple[int, int, int]]) -> dict:
        """`(espn_id, season_rank, recent_rank)` as ESPN returns it."""
        return {
            "players": [
                {
                    "id": espn_id,
                    "ratings": {
                        "0": {"totalRanking": season, "positionalRanking": 1},
                        "1": {"totalRanking": recent, "positionalRanking": 1},
                        "2": {"totalRanking": season, "positionalRanking": 1},
                        "3": {"totalRanking": season, "positionalRanking": 1},
                    },
                    "player": {
                        "fullName": f"P{espn_id}",
                        "draftRanksByRankType": {"STANDARD": {"rank": 999}},
                    },
                }
                for espn_id, season, recent in rows
            ]
        }

    def test_reads_the_season_to_date_total_ranking(self):
        league = a_league()
        league.espn_request = FakeRequest(self.ratings_payload([(11, 9, 2), (22, 29, 7)]))
        assert read_player_ranks(league) == {11: 9, 22: 29}

    def test_prefers_the_season_split_over_the_draft_rank(self):
        """The draft rank is preseason. Jokic was draft 1 and is now 9; using
        the draft rank would price every player as of October."""
        league = a_league()
        league.espn_request = FakeRequest(self.ratings_payload([(11, 9, 2)]))
        assert read_player_ranks(league)[11] == 9

    def test_prefers_the_season_split_over_the_recent_window(self):
        """Perceived value is what the manager sees in-app: season-to-date."""
        league = a_league()
        league.espn_request = FakeRequest(self.ratings_payload([(22, 29, 7)]))
        assert read_player_ranks(league)[22] == 29

    def test_sends_a_fantasy_filter_so_the_full_universe_comes_back(self):
        """Without the header ESPN returns a short alphabetical page, which
        silently ranked only players whose surname began with A or B."""
        league = a_league()
        league.espn_request = FakeRequest(self.ratings_payload([(11, 1, 1)]))
        read_player_ranks(league, limit=300)
        headers = league.espn_request.last_headers or {}
        assert "x-fantasy-filter" in headers
        sent = json.loads(headers["x-fantasy-filter"])
        assert sent["players"]["limit"] == 300

    def test_players_without_a_rank_are_omitted_not_zeroed(self):
        league = a_league()
        league.espn_request = FakeRequest({"players": [{"id": 1, "ratings": {}}]})
        assert read_player_ranks(league) == {}

    def test_a_zero_total_ranking_is_treated_as_unranked(self):
        """ESPN sends `totalRanking: 0` for players it does not rank, mostly
        injured ones. Passing it through would sort them best in the league.
        Seen on 21 of 308 players in a real league, 4 of them ACTIVE."""
        league = a_league()
        league.espn_request = FakeRequest(self.ratings_payload([(11, 0, 0), (22, 29, 7)]))
        assert read_player_ranks(league) == {22: 29}

    def test_a_negative_total_ranking_is_treated_as_unranked(self):
        league = a_league()
        league.espn_request = FakeRequest(self.ratings_payload([(11, -1, -1)]))
        assert read_player_ranks(league) == {}

    def test_a_null_total_ranking_is_omitted(self):
        league = a_league()
        league.espn_request = FakeRequest(
            {"players": [{"id": 1, "ratings": {"0": {"totalRanking": None}}}]}
        )
        assert read_player_ranks(league) == {}

    def test_an_auth_failure_is_reraised_as_an_auth_error(self):
        league = a_league()
        league.espn_request = FakeRequest(raises=Exception("401 Unauthorized"))
        with pytest.raises(EspnAuthError):
            read_player_ranks(league)

    def test_an_unexpected_payload_shape_is_an_empty_map_not_a_crash(self):
        league = a_league()
        league.espn_request = FakeRequest(payload={"unexpected": True})
        assert read_player_ranks(league) == {}


class TestPositionParsing:
    """Real `eligibleSlots` carry `UT` (not `UTIL`) plus combo slots."""

    def test_keeps_only_atomic_positions(self):
        league = a_league()
        league.teams[0].roster[0].eligibleSlots = [
            "PF",
            "C",
            "F",
            "G/F",
            "PF/C",
            "F/C",
            "UT",
            "BE",
        ]
        assert read_rosters(league)[1][0].positions == ["PF", "C"]

    def test_drops_the_ut_slot_espn_actually_sends(self):
        league = a_league()
        league.teams[0].roster[0].eligibleSlots = ["PG", "G", "UT", "BE"]
        assert read_rosters(league)[1][0].positions == ["PG"]

    def test_falls_back_to_the_default_position_when_no_atomic_slot_is_listed(self):
        league = a_league()
        league.teams[0].roster[0].eligibleSlots = ["UT", "BE"]
        league.teams[0].roster[0].position = "SG"
        assert read_rosters(league)[1][0].positions == ["SG"]


class TestNameNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Luka Dončić", "luka doncic"),
            ("Luka Doncic", "luka doncic"),
            ("Nikola Jokić", "nikola jokic"),
            ("Jaren Jackson Jr.", "jaren jackson"),
            ("Gary Trent Jr", "gary trent"),
            ("Wendell Carter Jr.", "wendell carter"),
            ("Kelly Oubre Jr.", "kelly oubre"),
            ("Marvin Bagley III", "marvin bagley"),
            ("P.J. Washington", "pj washington"),
            ("  Jalen   Green  ", "jalen green"),
            ("De'Aaron Fox", "deaaron fox"),
        ],
    )
    def test_normalizes_accents_suffixes_and_punctuation(self, raw, expected):
        assert normalize_name(raw) == expected

    def test_accented_and_unaccented_spellings_collide_deliberately(self):
        assert normalize_name("Luka Dončić") == normalize_name("Luka Doncic")


class TestCrosswalk:
    def test_matches_espn_ids_to_nba_ids_by_normalized_name(self):
        espn = {3945274: "Luka Doncic", 3112335: "Nikola Jokic"}
        nba = {1629029: "Luka Dončić", 203999: "Nikola Jokić"}
        assert match_players_by_name(espn, nba) == {3945274: 1629029, 3112335: 203999}

    def test_an_unmatched_espn_player_is_absent_from_the_map(self):
        assert match_players_by_name({1: "Nobody At All"}, {2: "Luka Dončić"}) == {}

    def test_suffix_differences_still_match(self):
        espn = {1: "Jaren Jackson Jr."}
        nba = {2: "Jaren Jackson Jr"}
        assert match_players_by_name(espn, nba) == {1: 2}

    def test_ambiguous_names_are_skipped_rather_than_guessed(self):
        """Two NBA players normalizing identically must not be matched blind."""
        espn = {1: "Jalen Williams"}
        nba = {10: "Jalen Williams", 11: "Jalen  Williams"}
        assert match_players_by_name(espn, nba) == {}


class TestRecordedLeagueShape:
    """Guards `espn_league_shape.json`: a real league recording with names and
    ids anonymized. It documents the distribution the readers must survive."""

    @pytest.fixture
    def shape(self) -> dict:
        return load_fixture("espn_league_shape")

    def test_carries_a_full_league(self, shape):
        assert len(shape["players"]) > 250
        assert len(shape["teams"]) == 8

    def test_identities_are_anonymized(self, shape):
        assert all(re.fullmatch(r"P\d{3}", p["name"]) for p in shape["players"])
        assert all(p["playerId"] >= 900000 for p in shape["players"])
        assert shape["league_id"] == 1234567

    def test_every_status_maps_to_the_schema(self, shape):
        for entry in shape["players"]:
            assert to_status(entry["injuryStatus"]) in {"ACTIVE", "DTD", "OUT", "INACTIVE"}

    def test_real_leagues_contain_plenty_of_unranked_players(self, shape):
        """45 of 308 here. ESPN leaves deep-bench and injured players
        unranked, and sends `totalRanking: 0` for some of them; the reader
        normalizes both to absent, so no zero survives into this file."""
        ranks = [p["espn_rank"] for p in shape["players"]]
        assert sum(1 for r in ranks if r is None) > 20
        assert all(r is None or r > 0 for r in ranks)

    def test_eligible_slots_only_contain_atomic_positions(self, shape):
        """The readers already filtered combination and bench slots out."""
        seen = {s for p in shape["players"] for s in p["eligibleSlots"]}
        assert seen <= set(ATOMIC_POSITIONS)
