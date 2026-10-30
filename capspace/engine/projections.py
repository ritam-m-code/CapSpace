"""Projection: stable rates times volatile minutes.

The spec's decomposition, and the reason for it: production splits into a
component that barely moves and a component that carries nearly all the
variance in fantasy value.

- **Per-minute rates** are close to a property of the player. A rotation
  change does not make someone score at a different rate per minute; it
  changes how many minutes they get.
- **Projected minutes** are driven by information absent from box scores —
  rotation changes, injuries, load management, back-to-backs. This is where
  the uncertainty lives.

So recent form is expressed as a *minutes* adjustment on stable rates, never
as a raw rolling average of production. A 14-day window is roughly six games,
which is far too noisy to read as a rate directly: that is what shrinkage is
for. A player with 40 window minutes keeps a rate near their season prior; a
player with 1800 lands near what they just did.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from capspace.data.schemas import COUNTING_STATS, GameLog

DEFAULT_WINDOW_DAYS: int = 14
"""Roughly six games. The spec's default rolling window."""

DEFAULT_SHRINKAGE_K: float = 200.0
"""Minutes of evidence at which a window rate and the prior weigh equally."""

MINUTES_HALF_LIFE_GAMES: float = 5.0
"""Half-life for the minutes estimate, in games. Small enough that a role
change three games ago moves the number, large enough to ignore one blowout."""


def _to_frame(logs: list[GameLog]) -> pd.DataFrame:
    """Game logs as a frame, newest last."""
    if not logs:
        return pd.DataFrame(columns=["nba_id", "game_date", "minutes", *COUNTING_STATS])
    frame = pd.DataFrame([entry.model_dump() for entry in logs])
    return frame.sort_values(["nba_id", "game_date"]).reset_index(drop=True)


def _window(frame: pd.DataFrame, window_days: int, asof: dt.date | None) -> pd.DataFrame:
    """Rows within `window_days` of `asof`, inclusive on both ends."""
    if frame.empty:
        return frame
    dates = pd.Series(frame["game_date"].tolist(), index=frame.index)
    end = asof if asof is not None else max(dates)
    start = end - dt.timedelta(days=window_days)
    return frame.loc[(dates >= start) & (dates <= end)]


def _rates_from(frame: pd.DataFrame) -> pd.DataFrame:
    """Pooled per-minute rates: summed stats over summed minutes.

    Pooled, not a mean of per-game rates — a 40-minute game must count four
    times as much as a 10-minute one, which averaging per-game rates would
    not do.
    """
    played = frame[frame["minutes"] > 0]
    if played.empty:
        return pd.DataFrame(columns=list(COUNTING_STATS), index=pd.Index([], dtype="int64"))
    totals = played.groupby("nba_id")[["minutes", *COUNTING_STATS]].sum()
    rates = totals[list(COUNTING_STATS)].div(totals["minutes"], axis=0)
    return rates.astype(float)


def per_minute_rates(
    logs: list[GameLog],
    window_days: int = DEFAULT_WINDOW_DAYS,
    asof: dt.date | None = None,
) -> pd.DataFrame:
    """Pooled per-minute rates over the trailing window, indexed by `nba_id`.

    Players with no game in the window are absent rather than zero — no
    evidence is not the same claim as no production.
    """
    return _rates_from(_window(_to_frame(logs), window_days, asof))


def shrink_to_prior(
    window: pd.DataFrame,
    prior: pd.DataFrame,
    window_minutes: pd.Series,
    k: float = DEFAULT_SHRINKAGE_K,
) -> pd.DataFrame:
    """Credibility-weight window rates toward `prior`.

        w = window_minutes / (window_minutes + k)
        shrunk = w * window + (1 - w) * prior

    `k` is in minutes, so the weight responds to how much the player actually
    played rather than to how many games elapsed. A player absent from
    `prior` keeps their window rate, since there is nothing to shrink toward.
    """
    if window.empty:
        return window.astype(float)

    weight = window_minutes.reindex(window.index).fillna(0.0)
    weight = weight / (weight + k)

    aligned_prior = prior.reindex(index=window.index, columns=window.columns)
    aligned_prior = aligned_prior.where(aligned_prior.notna(), window)

    shrunk = window.mul(weight, axis=0) + aligned_prior.mul(1.0 - weight, axis=0)
    return shrunk.astype(float)


def projected_minutes(
    logs: list[GameLog],
    window_days: int = DEFAULT_WINDOW_DAYS,
    asof: dt.date | None = None,
) -> pd.Series:
    """Expected minutes per game played, weighted toward recent games.

    An exponentially weighted mean over the window with a five-game half-life.
    Games at zero minutes are dropped rather than averaged in as zeros: a DNP
    says something about availability, not about the minutes a player gets
    when they do play, and the lineup optimizer handles availability itself.
    """
    frame = _window(_to_frame(logs), window_days, asof)
    played = frame[frame["minutes"] > 0]
    if played.empty:
        return pd.Series(dtype=float)
    return (
        played.groupby("nba_id")["minutes"]
        .apply(lambda s: s.ewm(halflife=MINUTES_HALF_LIFE_GAMES).mean().iloc[-1])
        .astype(float)
    )


def project(
    logs: list[GameLog],
    window_days: int = DEFAULT_WINDOW_DAYS,
    asof: dt.date | None = None,
) -> pd.DataFrame:
    """Projected per-game counting stats, indexed by `nba_id`.

    Shrunk per-minute rates times projected minutes. The prior is the
    player's own season-to-date rate; a player with no history beyond the
    window falls back to the pool's mean rate, so an unknown rookie is
    treated as replacement-level rather than as whatever their three games
    happened to show.
    """
    frame = _to_frame(logs)
    window_frame = _window(frame, window_days, asof)

    window_rates = _rates_from(window_frame)
    if window_rates.empty:
        return pd.DataFrame(columns=list(COUNTING_STATS), index=pd.Index([], dtype="int64"))

    season_rates = _rates_from(frame)
    prior = season_rates.reindex(index=window_rates.index, columns=window_rates.columns)
    pool_mean = season_rates.mean(axis=0) if not season_rates.empty else window_rates.mean(axis=0)
    prior = prior.fillna(pool_mean)

    played = window_frame[window_frame["minutes"] > 0]
    window_minutes = played.groupby("nba_id")["minutes"].sum()

    rates = shrink_to_prior(window_rates, prior, window_minutes)
    minutes = projected_minutes(logs, window_days, asof).reindex(rates.index).fillna(0.0)

    projected = rates.mul(minutes, axis=0)
    return projected.replace([np.inf, -np.inf], 0.0).fillna(0.0).astype(float)
