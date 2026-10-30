"""Read-only ingestion of NBA box scores and schedule from `nba_api`.

`nba_api` needs no credentials, so this module's fixtures are recorded from
the live API rather than invented. Column names below are the API's own,
verified against a live response on 2026-10-29.

Two shapes caught us out and are handled rather than assumed:

- `PlayerGameLogs.MIN` is a float of minutes (`38.283333`). The older
  singular `PlayerGameLog` endpoint returns `"MM:SS"`, so `parse_minutes`
  accepts both and treats a missing value as a DNP.
- `TeamGameLogs.MATCHUP` encodes home as `"TOR vs. BKN"` and away as
  `"PHX @ OKC"`; the opponent has to be split out of the string.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, cast

import pandas as pd

from capspace.data.schemas import GameLog
from capspace.data.storage import Cache

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

GAME_LOG_COLUMNS: tuple[str, ...] = (
    "PLAYER_ID",
    "PLAYER_NAME",
    "TEAM_ABBREVIATION",
    "GAME_DATE",
    "MIN",
    "PTS",
    "REB",
    "AST",
    "STL",
    "BLK",
    "FG3M",
    "FGM",
    "FGA",
    "FTM",
    "FTA",
    "TOV",
)

_STAT_BY_COLUMN: dict[str, str] = {
    "PTS": "pts",
    "REB": "reb",
    "AST": "ast",
    "STL": "stl",
    "BLK": "blk",
    "FG3M": "fg3m",
    "FGM": "fgm",
    "FGA": "fga",
    "FTM": "ftm",
    "FTA": "fta",
    "TOV": "tov",
}

HOME_SEPARATOR = " vs. "
AWAY_SEPARATOR = " @ "


def load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded upstream response from `tests/fixtures/<name>.json`."""
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


def parse_minutes(value: float | int | str | None) -> float:
    """Minutes played as a float, from either API representation.

    A null or blank value means the player did not appear, which is 0.0 and
    not missing data — a DNP is information about availability.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if ":" in value:
        minutes, _, seconds = value.partition(":")
        return int(minutes) + int(seconds) / 60.0
    return float(value)


def _parse_game_date(value: Any) -> dt.date:
    """Coerce the API's ISO string, or a cached datetime64, to a plain date.

    A missing date is raised rather than passed through: `pd.Timestamp(None)`
    is `NaT`, and a `NaT` game date would quietly land a log outside every
    rolling window instead of failing.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError(f"game date is missing or unparseable: {value!r}")
    return cast(dt.date, stamp.date())


def rows_to_game_logs(rows: list[dict[str, Any]]) -> list[GameLog]:
    """Parse raw `PlayerGameLogs` rows into validated models."""
    return [
        GameLog(
            nba_id=int(row["PLAYER_ID"]),
            game_date=_parse_game_date(row["GAME_DATE"]),
            minutes=parse_minutes(row["MIN"]),
            **{field: int(row[column]) for column, field in _STAT_BY_COLUMN.items()},
        )
        for row in rows
    ]


def rows_to_schedule(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Parse raw `TeamGameLogs` rows into a `game_date, team, opponent` frame."""
    records: list[dict[str, Any]] = []
    for row in rows:
        matchup = str(row["MATCHUP"])
        separator = HOME_SEPARATOR if HOME_SEPARATOR in matchup else AWAY_SEPARATOR
        team, _, opponent = matchup.partition(separator)
        records.append(
            {
                "game_date": _parse_game_date(row["GAME_DATE"]),
                "team": team.strip() or str(row["TEAM_ABBREVIATION"]),
                "opponent": opponent.strip(),
            }
        )
    return pd.DataFrame(records, columns=["game_date", "team", "opponent"])


def _upstream_game_logs(season: str) -> pd.DataFrame:
    """Hit the live `nba_api` player game log endpoint. Patched out in tests."""
    from nba_api.stats.endpoints import playergamelogs

    frame = playergamelogs.PlayerGameLogs(
        season_nullable=season,
        season_type_nullable="Regular Season",
        timeout=60,
    ).get_data_frames()[0]
    return frame.loc[:, list(GAME_LOG_COLUMNS)]


def _upstream_schedule(season: str) -> pd.DataFrame:
    """Hit the live `nba_api` team game log endpoint. Patched out in tests."""
    from nba_api.stats.endpoints import teamgamelogs

    frame = teamgamelogs.TeamGameLogs(
        season_nullable=season,
        season_type_nullable="Regular Season",
        timeout=60,
    ).get_data_frames()[0]
    return frame.loc[:, ["TEAM_ABBREVIATION", "GAME_ID", "GAME_DATE", "MATCHUP"]]


def fetch_game_logs(season: str, cache: Cache) -> list[GameLog]:
    """Every regular-season player game log for `season`, cached for 12h."""
    frame = cache.get_or_fetch(f"nba_game_logs:{season}", lambda: _upstream_game_logs(season))
    return rows_to_game_logs(frame.to_dict(orient="records"))


def fetch_schedule(season: str, cache: Cache) -> pd.DataFrame:
    """The `season` schedule as `game_date, team, opponent`, cached for 12h."""
    frame = cache.get_or_fetch(f"nba_schedule:{season}", lambda: _upstream_schedule(season))
    return rows_to_schedule(frame.to_dict(orient="records"))
