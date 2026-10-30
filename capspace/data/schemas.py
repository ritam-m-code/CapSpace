"""Validated shapes for everything that crosses a module boundary.

Any data entering the engine from `nba_api` or ESPN is parsed into these models
first, so a malformed upstream response fails loudly at the edge rather than
silently producing a wrong valuation deep in the z-score pass.
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

CATEGORIES: tuple[str, ...] = (
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
"""The nine head-to-head categories, in the spec's order. `TO` is inverted."""

POOL_SIZE: int = 150
"""How many fantasy-relevant players the z-score pool is standardized over."""

COUNTING_STATS: tuple[str, ...] = (
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
)
"""Raw per-game box score columns. `FG%`/`FT%` are derived, never stored."""

PlayerStatus = Literal["ACTIVE", "DTD", "OUT", "INACTIVE"]
LineupLock = Literal["DAILY", "WEEKLY"]

Count = Annotated[int, Field(ge=0)]


class GameLog(BaseModel):
    """One player's box score for one game."""

    nba_id: int
    game_date: date
    minutes: Annotated[float, Field(ge=0.0)]

    pts: Count
    reb: Count
    ast: Count
    stl: Count
    blk: Count
    fg3m: Count
    fgm: Count
    fga: Count
    ftm: Count
    fta: Count
    tov: Count

    @model_validator(mode="after")
    def _makes_cannot_exceed_attempts(self) -> "GameLog":
        if self.fgm > self.fga:
            raise ValueError(f"fgm ({self.fgm}) exceeds fga ({self.fga})")
        if self.ftm > self.fta:
            raise ValueError(f"ftm ({self.ftm}) exceeds fta ({self.fta})")
        if self.fg3m > self.fgm:
            raise ValueError(f"fg3m ({self.fg3m}) exceeds fgm ({self.fgm})")
        return self


class Player(BaseModel):
    """Identity and fantasy availability. Production lives in `GameLog`.

    `espn_id` and `espn_rank` are optional because the NBA and ESPN player
    universes do not match: two-way and ten-day players appear in `nba_api`
    with no ESPN entry at all.
    """

    nba_id: int
    name: str
    team: str
    positions: list[str]
    status: PlayerStatus
    espn_id: int | None = None
    espn_rank: int | None = None


class RosterSlot(BaseModel):
    """A lineup slot and which positions may fill it."""

    slot: str
    eligible_positions: list[str]
    count: Annotated[int, Field(ge=0)]


class LeagueSettings(BaseModel):
    """League configuration, read from the ESPN API rather than configured.

    Hand-configuring these is how a model silently optimizes for the wrong
    league, so there is no default for any of them.
    """

    league_id: int
    team_id: int
    categories: list[str]
    roster_slots: dict[str, int]
    acquisition_limit: Annotated[int, Field(ge=0)]
    waiver_days: Annotated[int, Field(ge=0)]
    lineup_lock: LineupLock


class CategoryLine(BaseModel):
    """A realized or simulated nine-category result for one team-week."""

    values: dict[str, float]

    @model_validator(mode="after")
    def _covers_every_category(self) -> "CategoryLine":
        missing = set(CATEGORIES) - set(self.values)
        if missing:
            raise ValueError(f"missing categories: {sorted(missing)}")
        return self
