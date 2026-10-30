"""One-off: record trimmed real nba_api responses into tests/fixtures/.

Selects a deterministic, small cast from a mid-season window, deliberately
including the edge cases the plan's Review Focus items need:
  - a thin-sample player (few games) -> shrinkage must dominate
  - a zero-free-throw-attempt player -> impact must be 0.0, not NaN
"""

import json
from pathlib import Path

import pandas as pd
from nba_api.stats.endpoints import playergamelogs, teamgamelogs

SEASON = "2025-26"
START, END = "2026-01-01", "2026-01-31"
OUT = Path("tests/fixtures")

LOG_COLS = [
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
]

logs = playergamelogs.PlayerGameLogs(
    season_nullable=SEASON, season_type_nullable="Regular Season", timeout=60
).get_data_frames()[0][LOG_COLS]
logs["GAME_DATE"] = pd.to_datetime(logs["GAME_DATE"])

window = logs[(logs["GAME_DATE"] >= START) & (logs["GAME_DATE"] <= END)].copy()

per_player = (
    window.groupby(["PLAYER_ID", "PLAYER_NAME", "TEAM_ABBREVIATION"])
    .agg(games=("MIN", "size"), mins=("MIN", "sum"), pts=("PTS", "sum"), fta=("FTA", "sum"))
    .reset_index()
)

# Eight heavy-minutes starters, spread across teams, highest scoring first.
starters = (
    per_player[per_player["games"] >= 10]
    .sort_values("pts", ascending=False)
    .drop_duplicates("TEAM_ABBREVIATION")
    .head(8)
)

# A thin-sample player: exactly 2-3 games but real minutes when he played.
thin = per_player[(per_player["games"].between(2, 3)) & (per_player["mins"] >= 30)]
thin = thin.sort_values("mins", ascending=False).head(1)

# A player who never shot a free throw in the window, but played real minutes.
no_ft = per_player[(per_player["fta"] == 0) & (per_player["games"] >= 5)]
no_ft = no_ft.sort_values("mins", ascending=False).head(1)

cast = pd.concat([starters, thin, no_ft]).drop_duplicates("PLAYER_ID")
chosen = window[window["PLAYER_ID"].isin(cast["PLAYER_ID"])].copy()
chosen["GAME_DATE"] = chosen["GAME_DATE"].dt.strftime("%Y-%m-%dT%H:%M:%S")
chosen = chosen.sort_values(["PLAYER_ID", "GAME_DATE"])

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "nba_gamelogs.json").write_text(
    json.dumps(
        {
            "season": SEASON,
            "window": {"start": START, "end": END},
            "columns": LOG_COLS,
            "rows": chosen.to_dict(orient="records"),
        },
        indent=1,
    )
)

teams = teamgamelogs.TeamGameLogs(
    season_nullable=SEASON, season_type_nullable="Regular Season", timeout=60
).get_data_frames()[0][["TEAM_ABBREVIATION", "GAME_ID", "GAME_DATE", "MATCHUP"]]
teams["GAME_DATE"] = pd.to_datetime(teams["GAME_DATE"])
sched = teams[(teams["GAME_DATE"] >= START) & (teams["GAME_DATE"] <= END)].copy()
sched["GAME_DATE"] = sched["GAME_DATE"].dt.strftime("%Y-%m-%dT%H:%M:%S")
sched = sched.sort_values(["GAME_DATE", "TEAM_ABBREVIATION"])

(OUT / "nba_schedule.json").write_text(
    json.dumps(
        {
            "season": SEASON,
            "window": {"start": START, "end": END},
            "rows": sched.to_dict(orient="records"),
        },
        indent=1,
    )
)

print(
    f"gamelogs: {len(chosen)} rows, {chosen['PLAYER_ID'].nunique()} players, "
    f"{chosen['TEAM_ABBREVIATION'].nunique()} teams"
)
print(
    cast[["PLAYER_NAME", "TEAM_ABBREVIATION", "games", "mins", "pts", "fta"]].to_string(index=False)
)
print(
    f"schedule: {len(sched)} rows, {sched['TEAM_ABBREVIATION'].nunique()} teams, "
    f"{sched['GAME_DATE'].nunique()} distinct days"
)
