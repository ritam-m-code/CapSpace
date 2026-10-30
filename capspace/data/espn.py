"""Read-only ESPN league state: settings, rosters, free agents, perceived rank.

**This module performs no writes.** It issues GETs only. The write path lives
separately under `capspace/write/` with its own credentials, so a bug in the
research pipeline cannot modify a roster.

Three things about ESPN's undocumented fantasy API shaped this module, all
confirmed by inspecting `espn_api` 0.46 and the live NBA data:

1. **The wrapper does not expose an overall player rank.** `espn_api`'s
   basketball `Player` carries `posRank` (positional) but, unlike its
   football and baseball equivalents, no overall rank or percent-owned. The
   overall rank *is* the perceived-value column the whole thesis rests on, so
   `read_player_ranks` goes to the raw `kona_player_info` view for it.

2. **ESPN player ids are not NBA player ids.** ESPN assigns its own. Joining
   model value to perceived value therefore needs a name crosswalk, which is
   what `normalize_name` and `match_players_by_name` are for. Accents are the
   common failure: ESPN writes "Luka Doncic" where `nba_api` writes "Luka
   Dončić".

3. **An auth failure looks like an empty league, not an error.** Expired
   cookies return a well-formed response with nothing in it, which would
   score as a roster of zeros. Empty reads are therefore treated as auth
   failures and raised.

UNVERIFIED: the `draftRanksByRankType.STANDARD.rank` path in
`read_player_ranks` is read from the community-mapped `kona_player_info`
response and has not been run against a live private basketball league. It is
the first thing to check once credentials exist.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from capspace.data.schemas import CATEGORIES, LeagueSettings, Player, PlayerStatus

CREDENTIAL_HELP = (
    "ESPN rejected the request, or returned an empty league. The usual cause is "
    "expired cookies: re-copy ESPN_S2 and ESPN_SWID from fantasy.espn.com "
    "(DevTools, Application, Cookies) into .env. See SETUP_CREDENTIALS.md."
)

_STATUS_MAP: dict[str, PlayerStatus] = {
    "ACTIVE": "ACTIVE",
    "NORMAL": "ACTIVE",
    "DAY_TO_DAY": "DTD",
    "QUESTIONABLE": "DTD",
    "DOUBTFUL": "DTD",
    "PROBABLE": "DTD",
    "OUT": "OUT",
    "SUSPENSION": "OUT",
    "INJURY_RESERVE": "INACTIVE",
    "FIVE_DAY_DNP": "INACTIVE",
    "TEN_DAY_DNP": "INACTIVE",
    "NON_ROSTER": "INACTIVE",
    "SEASON_ENDING_INJURY": "INACTIVE",
}

_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

UNMATCHED_NBA_ID = 0
"""Sentinel for a player whose ESPN entry has not been joined to an NBA id."""


class EspnAuthError(RuntimeError):
    """ESPN refused the request or returned nothing, usually expired cookies."""


@dataclass(frozen=True)
class EspnCreds:
    """The four `.env` values needed for a private-league read."""

    s2: str
    swid: str
    league_id: int
    team_id: int

    def __post_init__(self) -> None:
        if not self.s2:
            raise ValueError("ESPN_S2 is empty; see SETUP_CREDENTIALS.md")
        if not self.swid:
            raise ValueError("ESPN_SWID is empty; see SETUP_CREDENTIALS.md")


def to_status(espn_status: str | None) -> PlayerStatus:
    """Map an ESPN injury status onto the schema's four states.

    An unrecognized status maps to `ACTIVE` rather than being dropped: ESPN
    adds status strings over time, and treating a new one as unavailable
    would silently remove a healthy player from the pool. Erring toward
    including a player is recoverable; erring toward excluding is not.
    """
    if espn_status is None:
        return "ACTIVE"
    return _STATUS_MAP.get(str(espn_status).upper(), "ACTIVE")


def normalize_name(name: str) -> str:
    """A name reduced to its matchable core.

    Strips accents, punctuation and generational suffixes, lowercases, and
    collapses whitespace, so "Luka Dončić" and "Luka Doncic" collide and
    "Jaren Jackson Jr." matches "Jaren Jackson Jr".
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^\w\s]", "", ascii_only).lower()
    parts = [p for p in cleaned.split() if p not in _NAME_SUFFIXES]
    return " ".join(parts)


def match_players_by_name(espn_names: dict[int, str], nba_names: dict[int, str]) -> dict[int, int]:
    """Map ESPN player id to NBA player id by normalized name.

    A normalized name shared by two NBA players is dropped rather than
    guessed: a wrong join would attribute one player's production to another,
    which is worse than a missing row that shows up as a blank rank.
    """
    by_normalized: dict[str, list[int]] = {}
    for nba_id, name in nba_names.items():
        by_normalized.setdefault(normalize_name(name), []).append(nba_id)

    matched: dict[int, int] = {}
    for espn_id, name in espn_names.items():
        candidates = by_normalized.get(normalize_name(name), [])
        if len(candidates) == 1:
            matched[espn_id] = candidates[0]
    return matched


def build_client(creds: EspnCreds, year: int) -> Any:
    """A read-only `espn_api` League client for `creds`."""
    from espn_api.basketball import League

    try:
        return League(
            league_id=creds.league_id,
            year=year,
            espn_s2=creds.s2,
            swid=creds.swid,
        )
    except Exception as exc:  # noqa: BLE001 - upstream raises bare Exception
        raise EspnAuthError(f"{CREDENTIAL_HELP} (upstream said: {exc})") from exc


def _to_player(espn_player: Any) -> Player:
    """One ESPN player as a schema `Player`, with no NBA id yet."""
    slots = list(getattr(espn_player, "eligibleSlots", []) or [])
    positions = [s for s in slots if s not in {"BE", "IR", "UTIL"}] or [
        str(getattr(espn_player, "position", "UTIL"))
    ]
    return Player(
        nba_id=UNMATCHED_NBA_ID,
        name=str(espn_player.name),
        team=str(getattr(espn_player, "proTeam", "") or ""),
        positions=positions,
        status=to_status(getattr(espn_player, "injuryStatus", None)),
        espn_id=int(espn_player.playerId),
        espn_rank=None,
    )


def read_settings(client: Any, team_id: int | None = None) -> LeagueSettings:
    """League configuration, read from the API rather than configured by hand.

    Categories come from the schema's fixed nine rather than from ESPN's stat
    id list, which the simulator's category order depends on.
    """
    settings = client.settings
    waiver_days = len(list(getattr(settings, "waiver_process_days", []) or []))
    scoring = str(getattr(settings, "scoring_type", "") or "")
    return LeagueSettings(
        league_id=int(client.league_id),
        team_id=int(team_id if team_id is not None else 0),
        categories=list(CATEGORIES),
        roster_slots=_roster_slots(client),
        acquisition_limit=int(getattr(settings, "acquisition_limit", None) or 0),
        waiver_days=waiver_days,
        lineup_lock="WEEKLY" if "WEEK" in scoring.upper() else "DAILY",
    )


def _roster_slots(client: Any) -> dict[str, int]:
    """Slot counts, inferred from the first roster when settings omit them."""
    teams = list(getattr(client, "teams", []) or [])
    if not teams:
        return {}
    counts: dict[str, int] = {}
    for player in getattr(teams[0], "roster", []) or []:
        slot = str(getattr(player, "lineupSlot", "") or "UTIL")
        counts[slot] = counts.get(slot, 0) + 1
    return counts


def read_rosters(client: Any) -> dict[int, list[Player]]:
    """Every team's roster, keyed by ESPN team id.

    An empty team list is raised as an auth error rather than returned: a
    league with no teams is not a real league state, and silently returning
    `{}` would be scored downstream as "you have no players".
    """
    teams = list(getattr(client, "teams", []) or [])
    if not teams:
        raise EspnAuthError(CREDENTIAL_HELP)
    return {
        int(team.team_id): [_to_player(p) for p in (getattr(team, "roster", []) or [])]
        for team in teams
    }


def read_free_agents(client: Any, size: int = 200) -> list[Player]:
    """The instantly-addable and waiver free-agent pool."""
    try:
        agents = client.free_agents(size=size)
    except Exception as exc:  # noqa: BLE001 - upstream raises bare Exception
        raise EspnAuthError(f"{CREDENTIAL_HELP} (upstream said: {exc})") from exc
    return [_to_player(p) for p in agents]


def read_player_ranks(client: Any) -> dict[int, int]:
    """ESPN's overall player rank, keyed by ESPN player id.

    This is *perceived* value — what the opposing manager sees in the app,
    computed from season-to-date totals. The gap between it and model value
    is the entire product, so it is read from the raw `kona_player_info`
    view: the wrapper does not surface it.

    Players with no rank are omitted rather than given a sentinel, so callers
    can render a blank instead of mistaking 0 for "best in the league".
    """
    params = {"view": "kona_player_info"}
    try:
        payload = client.espn_request.league_get(params=params, headers=None)
    except Exception as exc:  # noqa: BLE001 - upstream raises bare Exception
        raise EspnAuthError(f"{CREDENTIAL_HELP} (upstream said: {exc})") from exc

    ranks: dict[int, int] = {}
    for entry in (payload or {}).get("players", []) or []:
        espn_id = entry.get("id")
        inner = entry.get("player") or {}
        standard = (inner.get("draftRanksByRankType") or {}).get("STANDARD") or {}
        rank = standard.get("rank")
        if espn_id is not None and rank is not None:
            ranks[int(espn_id)] = int(rank)
    return ranks
