"""Valuation: volume-weighted category z-scores.

Standard per-game z-scores for the six counting categories, with turnovers
inverted so that fewer is better and every category points the same way.

The percentage categories are the part that matters. **They are not z-scored
on the rate.** Weekly team FG% is a volume-weighted aggregate, so what a
player contributes is not their percentage but how far their percentage
moves the team's, which scales with how often they shoot:

    impact = (player_pct - pool_pct) * player_attempts

where `pool_pct` is itself volume-weighted — total makes over total attempts
across the pool, not the average of per-player rates. That impact is then
standardized like any other category.

Rate-based percentage z-scores are the single most common error in public
fantasy rankings. They rank a 90% free-throw shooter taking one attempt per
game above an elite high-volume scorer, which is backwards: the one-attempt
shooter barely moves a weekly total.

Z-scores here are also the fast heuristic that prunes candidates before the
Monte Carlo simulation, which is the actual objective function.
"""

from __future__ import annotations

from typing import Literal, cast

import numpy as np
import pandas as pd

from capspace.data.schemas import CATEGORIES, POOL_SIZE, Player

PctKind = Literal["FG", "FT"]

_PCT_COLUMNS: dict[PctKind, tuple[str, str]] = {
    "FG": ("fgm", "fga"),
    "FT": ("ftm", "fta"),
}

_COUNTING_CATEGORY_COLUMNS: dict[str, str] = {
    "PTS": "pts",
    "REB": "reb",
    "AST": "ast",
    "STL": "stl",
    "BLK": "blk",
    "3PM": "fg3m",
    "TO": "tov",
}

INVERTED_CATEGORIES: frozenset[str] = frozenset({"TO"})
"""Categories where less is better, negated after standardizing."""

UNAVAILABLE_STATUSES: frozenset[str] = frozenset({"OUT", "INACTIVE"})
"""Statuses that remove a player from the pool rather than ranking them."""


def pool_rate(proj: pd.DataFrame, kind: PctKind) -> float:
    """The pool's volume-weighted percentage for `kind`.

    Total makes over total attempts, which is what a weekly team percentage
    actually is. The mean of per-player rates would overweight players who
    barely shoot.
    """
    makes, attempts = _PCT_COLUMNS[kind]
    total_attempts = float(cast(float, proj[attempts].sum()))
    if total_attempts <= 0:
        return 0.0
    return float(cast(float, proj[makes].sum())) / total_attempts


def pct_impact(proj: pd.DataFrame, kind: PctKind) -> pd.Series:
    """Volume-weighted impact on the pool's percentage, per player.

    A player with no attempts gets exactly 0.0 — they neither help nor hurt
    the team's percentage, which is different from missing data and must not
    become NaN.
    """
    makes, attempts = _PCT_COLUMNS[kind]
    baseline = pool_rate(proj, kind)
    attempted = proj[attempts].astype(float)
    rate = (proj[makes].astype(float) / attempted.replace(0.0, np.nan)).fillna(baseline)
    return cast(pd.Series, ((rate - baseline) * attempted).astype(float))


def _standardize(values: pd.Series) -> pd.Series:
    """Z-score over the whole series, treating it as the population.

    A category with no spread returns zeros rather than NaN: if every player
    in the pool blocks the same number of shots, blocks carry no information
    and should contribute nothing, not poison the row sum.
    """
    spread = float(cast(float, values.std(ddof=0)))
    if spread <= 0 or not np.isfinite(spread):
        return pd.Series(0.0, index=values.index)
    return (values - float(values.mean())) / spread


def category_zscores(proj: pd.DataFrame, pool: pd.Index) -> pd.DataFrame:
    """Per-category z-scores for `pool`, standardized over `pool` alone.

    Returns exactly the nine `CATEGORIES` as columns. Restricting to the pool
    first matters: including deep-bench players would drag every mean down
    and compress the spread among the players actually worth comparing.
    """
    pooled = proj.loc[proj.index.intersection(pool)]

    columns: dict[str, pd.Series] = {}
    for category in CATEGORIES:
        if category in _COUNTING_CATEGORY_COLUMNS:
            raw = cast(pd.Series, pooled[_COUNTING_CATEGORY_COLUMNS[category]].astype(float))
        else:
            kind: PctKind = "FG" if category == "FG%" else "FT"
            raw = pct_impact(pooled, kind)
        standardized = _standardize(raw)
        if category in INVERTED_CATEGORIES:
            standardized = -standardized
        columns[category] = standardized

    return pd.DataFrame(columns, index=pooled.index).loc[:, list(CATEGORIES)]


def model_value(z: pd.DataFrame) -> pd.Series:
    """Total z-score across categories, descending-sortable.

    A deliberately blunt summary: it ignores that head-to-head is nine
    separate coin flips rather than a sum, which is what the Monte Carlo
    simulation exists to fix. Good enough to rank and prune.
    """
    return z.sum(axis=1).astype(float)


def eligible_pool(
    players: list[Player],
    proj: pd.DataFrame,
    minutes: pd.Series,
    size: int = POOL_SIZE,
) -> pd.Index:
    """The top `size` fantasy-relevant player ids, as the z-score pool.

    Two passes, which is what keeps this non-circular: standardize over every
    available player to get a provisional value, then keep the best `size`.
    `category_zscores` re-standardizes over the result.

    Excluded before ranking: players ESPN lists as `OUT` or `INACTIVE`, and
    anyone projecting zero minutes. Both would otherwise be ranked with zeros
    and pull the pool's means toward replacement level. `DTD` players stay —
    day-to-day is a lineup decision, not an absence from the player universe.
    """
    available = [
        p.nba_id
        for p in players
        if p.status not in UNAVAILABLE_STATUSES
        and p.nba_id in proj.index
        and float(cast(float, minutes.get(p.nba_id, 0.0))) > 0
    ]
    if not available:
        return pd.Index([], dtype="int64")

    candidates = pd.Index(available)
    provisional = model_value(category_zscores(proj, candidates))
    return pd.Index(provisional.sort_values(ascending=False).head(size).index)
