"""Schedule structure: how many games, and on which nights.

Two quantities drive streaming decisions, and neither is about talent:

- **Weekly game counts.** A four-game week from a mediocre player can beat a
  three-game week from a better one, because categories accumulate per game.
- **Off-night density.** The fraction of the league idle on a given night. On
  a night when 24 of 30 teams are off, the free-agent pool is thin and a
  player who actually plays is worth disproportionately more, because
  everyone else's roster is also sitting out.

Density uses a fixed 30-team denominator rather than counting the teams
present in the frame, so a trimmed test fixture still yields true fractions.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

NBA_TEAM_COUNT: int = 30
"""Denominator for off-night density. Fixed, not inferred from the data."""


def _within(sched: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Rows whose `game_date` falls in `[start, end]`, both bounds inclusive."""
    dates = pd.Series(sched["game_date"].tolist(), index=sched.index)
    mask = (dates >= start) & (dates <= end)
    return sched.loc[mask]


def weekly_game_counts(sched: pd.DataFrame, start: dt.date, end: dt.date) -> dict[str, int]:
    """Games each team plays in `[start, end]`.

    Every team in `sched` gets an entry, including teams with zero games in
    the window, so callers never have to distinguish "no games" from "unknown
    team".
    """
    window = _within(sched, start, end)
    counts = dict.fromkeys(sorted(set(sched["team"].tolist())), 0)
    for team, played in window["team"].value_counts().items():
        counts[str(team)] = int(played)
    return counts


def off_night_density(sched: pd.DataFrame, start: dt.date, end: dt.date) -> dict[dt.date, float]:
    """Fraction of the league idle on each day in `[start, end]`.

    1.0 means nobody plays, 0.0 means all 30 teams do. Every day in the
    window gets an entry even if no game is scheduled.
    """
    window = _within(sched, start, end)
    playing_by_day: dict[dt.date, set[str]] = {}
    for day, team in zip(window["game_date"], window["team"], strict=True):
        playing_by_day.setdefault(day, set()).add(str(team))

    density: dict[dt.date, float] = {}
    day = start
    while day <= end:
        playing = len(playing_by_day.get(day, set()))
        density[day] = (NBA_TEAM_COUNT - playing) / NBA_TEAM_COUNT
        day += dt.timedelta(days=1)
    return density


def plays_on(sched: pd.DataFrame, team: str, day: dt.date) -> bool:
    """Whether `team` has a game on `day`.

    Gates every daily lineup decision: a player cannot be started on a night
    their team is off, however good they are.
    """
    window = _within(sched, day, day)
    return team in set(window["team"].tolist())
