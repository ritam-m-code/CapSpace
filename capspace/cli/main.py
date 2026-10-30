"""`capspace` — desk-based exploration of the model's valuations.

`rank` is the thesis made legible: model value beside ESPN's perceived rank,
sorted by the gap between them. A large positive spread is a player the
league is mispricing downward, which is where a trade or a stream is worth
proposing.

Set `CAPSPACE_FIXTURES=1` to run against recorded fixtures instead of live
sources. That is how the test suite and a credential-less demo both work.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, cast

import pandas as pd
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from capspace.data.espn import (
    EspnAuthError,
    EspnCreds,
    build_client,
    match_players_by_name,
    read_free_agents,
    read_player_ranks,
    read_rosters,
    to_status,
)
from capspace.data.ingestion import (
    fetch_game_logs,
    load_fixture,
    rows_to_game_logs,
)
from capspace.data.schemas import CATEGORIES, GameLog, Player
from capspace.data.storage import Cache
from capspace.engine.metrics import category_zscores, eligible_pool, model_value
from capspace.engine.projections import DEFAULT_WINDOW_DAYS, project, projected_minutes

app = typer.Typer(
    add_completion=False,
    help="Quantitative decision engine for head-to-head 9-category fantasy basketball.",
)
console = Console()

VIEWS = ("compact", "zscore")
SORT_KEYS = ("spread", "model", "espn")

FIXTURE_SEASON = "2025-26"
FIXTURE_ASOF = dt.date(2026, 2, 1)
FIXTURE_WINDOW_DAYS = 400

DEFAULT_CACHE_PATH = Path.home() / ".cache" / "capspace" / "cache.duckdb"
DEFAULT_ENV_PATH = Path(".env")

ANONYMIZED_LEAGUE_ID = 1234567
ANONYMIZED_TEAM_ID = 1
"""Placeholders written into the committed fixture in place of real ids."""


def load_credentials(env_path: Path | None = None) -> EspnCreds:
    """The four ESPN values, from `.env` unless the shell already has them.

    `.env` is where SETUP_CREDENTIALS.md tells the owner to put these, so the
    CLI has to read it; relying on the shell environment alone would report
    perfectly good cookies as missing. The shell wins on conflict, which is
    what makes a one-off override possible without editing the file.
    """
    load_dotenv(env_path if env_path is not None else DEFAULT_ENV_PATH, override=False)
    return EspnCreds(
        s2=os.environ.get("ESPN_S2", ""),
        swid=os.environ.get("ESPN_SWID", ""),
        league_id=int(os.environ.get("ESPN_LEAGUE_ID") or 0),
        team_id=int(os.environ.get("ESPN_TEAM_ID") or 0),
    )


def spread(model_rank: pd.Series, espn_rank: pd.Series) -> pd.Series:
    """`espn_rank - model_rank`, both ranks taken over the same players.

    Positive means ESPN ranks the player *worse* than the model does — the
    league under-rates them, and they are cheap to acquire. Negative means
    the league over-rates them, and they are expensive to hold.

    Callers must pass ranks computed over the same population. `pool_ranks`
    exists because they are not comparable as given: model rank runs 1..150
    over the pool, while ESPN's is global over every ranked player. Naively
    subtracting them makes every fringe player look like a steal.
    """
    return espn_rank.reindex(model_rank.index) - model_rank


def pool_ranks(espn_rank: pd.Series) -> pd.Series:
    """Re-rank ESPN's global ranks 1..N within just these players.

    Preserves ESPN's ordering while putting it on the model's scale, so the
    two are subtractable. Unranked players stay unranked.
    """
    return espn_rank.rank(method="min", na_option="keep")


def _parse_window(window: str) -> int:
    """`"14d"` to `14`."""
    match = re.fullmatch(r"(\d+)d", window.strip().lower())
    if match is None:
        raise typer.BadParameter(f"window must look like '14d', got {window!r}")
    return int(match.group(1))


def _load_from_fixtures() -> tuple[list[GameLog], list[Player], dict[int, int]]:
    """Game logs, ESPN players and ESPN ranks, all from recorded fixtures."""
    raw_logs = load_fixture("nba_gamelogs")["rows"]
    logs = rows_to_game_logs(raw_logs)
    nba_names = {int(r["PLAYER_ID"]): str(r["PLAYER_NAME"]) for r in raw_logs}

    league = load_fixture("espn_league")
    espn_names = {int(p["playerId"]): str(p["name"]) for p in league["players"]}
    crosswalk = match_players_by_name(espn_names, nba_names)

    players: list[Player] = []
    ranks: dict[int, int] = {}
    for entry in league["players"]:
        espn_id = int(entry["playerId"])
        nba_id = crosswalk.get(espn_id)
        if nba_id is None:
            continue
        players.append(
            Player(
                nba_id=nba_id,
                name=nba_names[nba_id],
                team=str(entry["proTeam"]),
                positions=[s for s in entry["eligibleSlots"] if s not in {"UTIL", "BE"}],
                status=to_status(entry["injuryStatus"]),
                espn_id=espn_id,
                espn_rank=entry["espn_rank"],
            )
        )
        if entry["espn_rank"] is not None:
            ranks[nba_id] = int(entry["espn_rank"])
    return logs, players, ranks


def _load_from_live(season: str) -> tuple[list[GameLog], list[Player], dict[int, int]]:
    """Game logs from `nba_api`, players and ranks from the ESPN league."""
    creds = load_credentials()
    cache = Cache(DEFAULT_CACHE_PATH)
    logs = fetch_game_logs(season, cache)

    client = build_client(creds, year=int(season.split("-")[0]) + 1)
    rosters = read_rosters(client)
    espn_players = [p for roster in rosters.values() for p in roster]
    espn_ranks = read_player_ranks(client)

    nba_names = _nba_name_index(logs, season, cache)
    crosswalk = match_players_by_name(
        {p.espn_id: p.name for p in espn_players if p.espn_id is not None}, nba_names
    )

    players: list[Player] = []
    ranks: dict[int, int] = {}
    for player in espn_players:
        nba_id = crosswalk.get(player.espn_id) if player.espn_id is not None else None
        if nba_id is None:
            continue
        rank = espn_ranks.get(player.espn_id) if player.espn_id is not None else None
        players.append(player.model_copy(update={"nba_id": nba_id, "espn_rank": rank}))
        if rank is not None:
            ranks[nba_id] = rank
    return logs, players, ranks


def _nba_name_index(logs: list[GameLog], season: str, cache: Cache) -> dict[int, str]:
    """NBA id to name, taken from the cached game log frame."""
    frame = cache.get_or_fetch(f"nba_game_logs:{season}", lambda: pd.DataFrame())
    if frame.empty or "PLAYER_NAME" not in frame.columns:
        return {}
    pairs = frame.loc[:, ["PLAYER_ID", "PLAYER_NAME"]].drop_duplicates()
    return {int(r.PLAYER_ID): str(r.PLAYER_NAME) for r in pairs.itertuples()}


@app.command()
def rank(
    view: str = typer.Option("compact", help=f"One of {', '.join(VIEWS)}."),
    window: str = typer.Option(f"{DEFAULT_WINDOW_DAYS}d", help="Rolling window, e.g. 14d."),
    sort_by: str = typer.Option("spread", help=f"One of {', '.join(SORT_KEYS)}."),
    limit: int = typer.Option(50, help="Rows to print."),
) -> None:
    """Model value beside ESPN rank, sorted by the gap between them."""
    if view not in VIEWS:
        raise typer.BadParameter(f"view must be one of {', '.join(VIEWS)}")
    if sort_by not in SORT_KEYS:
        raise typer.BadParameter(f"sort-by must be one of {', '.join(SORT_KEYS)}")
    window_days = _parse_window(window)

    use_fixtures = os.environ.get("CAPSPACE_FIXTURES") == "1"
    try:
        if use_fixtures:
            logs, players, _ = _load_from_fixtures()
            window_days, asof = FIXTURE_WINDOW_DAYS, FIXTURE_ASOF
        else:
            logs, players, _ = _load_from_live(_current_season())
            asof = None
    except (EspnAuthError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    proj = project(logs, window_days=window_days, asof=asof)
    minutes = projected_minutes(logs, window_days=window_days, asof=asof)
    pool = eligible_pool(players, proj, minutes)
    if len(pool) == 0:
        console.print("[red]No eligible players; nothing to rank.[/red]")
        raise typer.Exit(code=1)

    z = category_zscores(proj, pool)
    value = model_value(z)
    model_rank = value.rank(ascending=False, method="min").astype(int)

    by_nba_id = {p.nba_id: p for p in players}
    espn_rank = pd.Series(
        {nba_id: by_nba_id[nba_id].espn_rank for nba_id in value.index}, dtype="float64"
    )

    frame = pd.DataFrame(
        {
            "Player": [by_nba_id[i].name for i in value.index],
            "Team": [by_nba_id[i].team for i in value.index],
            "Pos": ["/".join(by_nba_id[i].positions[:2]) for i in value.index],
            "value": value,
            "Model": model_rank,
            "ESPN": espn_rank,
            "Spread": spread(model_rank, pool_ranks(espn_rank)),
        }
    )
    frame = frame.join(z.round(2)) if view == "zscore" else frame
    frame = _sort(frame, sort_by).head(limit)
    _print(frame, view)


def _sort(frame: pd.DataFrame, sort_by: str) -> pd.DataFrame:
    """Sort by the chosen key, always pushing unranked players last."""
    key = {"spread": "Spread", "model": "Model", "espn": "ESPN"}[sort_by]
    ascending = sort_by != "spread"
    return frame.sort_values(key, ascending=ascending, na_position="last")


ZSCORE_VIEW_WIDTH = 170
"""The nine-category table needs this much room; narrower elides headers."""


def _print(frame: pd.DataFrame, view: str, out: Console | None = None) -> None:
    if out is None:
        out = Console(width=ZSCORE_VIEW_WIDTH) if view == "zscore" else console
    table = Table(title="Model value vs. ESPN perceived rank", header_style="bold")
    for column in ("Player", "Team", "Pos"):
        table.add_column(column, no_wrap=True)
    for column in ("Model", "ESPN", "Spread"):
        table.add_column(column, justify="right")
    if view == "zscore":
        for category in CATEGORIES:
            table.add_column(category, justify="right")

    for _, row in frame.iterrows():
        espn = cast(float, row["ESPN"])
        gap = cast(float, row["Spread"])
        model = cast(float, row["Model"])
        cells = [str(row["Player"]), str(row["Team"]), str(row["Pos"]), str(int(model))]
        cells.append("—" if pd.isna(espn) else str(int(espn)))
        cells.append("—" if pd.isna(gap) else f"{int(gap):+d}")
        if view == "zscore":
            cells += [f"{row[c]:+.2f}" for c in CATEGORIES]
        table.add_row(*cells)

    out.print(table)


@app.command("fixtures-record")
def fixtures_record(
    out: Path = typer.Option(
        Path("tests/fixtures/espn_league_shape.json"), help="Where to write the fixture."
    ),
    with_names: bool = typer.Option(
        False,
        "--with-names",
        help="Keep real player names. Do not commit the result to a public repo.",
    ),
) -> None:
    """Record this league's real ESPN shapes into `tests/fixtures/`.

    Needs credentials in `.env`, and is never run by the test suite.

    **Player names and ids are anonymized by default**, because this repo is
    public and a league's roster composition is the owner's to share. Ranks,
    statuses, eligible slots, pro teams, team assignments and the
    rostered-versus-free-agent split are all preserved, so the recording
    still documents a real league's distribution. Pass `--with-names` only
    for a local file you will not commit.
    """
    creds = load_credentials()
    season = _current_season()
    try:
        client = build_client(creds, year=int(season.split("-")[0]) + 1)
        rosters = read_rosters(client)
        ranks = read_player_ranks(client)
        agents = read_free_agents(client)
    except (EspnAuthError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    aliases: dict[str, str] = {}
    alias_ids: dict[int, int] = {}

    def alias(name: str) -> str:
        if with_names:
            return name
        return aliases.setdefault(name, f"P{len(aliases) + 1:03d}")

    def alias_id(espn_id: int | None) -> int | None:
        if with_names or espn_id is None:
            return espn_id
        return alias_ids.setdefault(espn_id, 900000 + len(alias_ids))

    def as_entry(player: Player) -> dict[str, Any]:
        espn_id = player.espn_id
        return {
            "name": alias(player.name),
            "playerId": alias_id(espn_id),
            "espn_rank": ranks.get(espn_id) if espn_id is not None else None,
            "injuryStatus": player.status,
            "proTeam": player.team,
            "eligibleSlots": player.positions,
        }

    rostered = [p for roster in rosters.values() for p in roster]
    players = [as_entry(p) for p in rostered + agents]

    document = {
        "_note": (
            f"Recorded from a live private ESPN league for {season} on "
            f"{dt.date.today().isoformat()}."
            + (
                " Real player names retained — DO NOT COMMIT."
                if with_names
                else " Anonymized: player names and ids are placeholders, while "
                "ranks, statuses, slots, pro teams and team assignments are real."
            )
        ),
        "league_id": ANONYMIZED_LEAGUE_ID,
        "team_id": ANONYMIZED_TEAM_ID,
        "year": int(season.split("-")[0]) + 1,
        "teams": [
            {"team_id": tid, "roster": [alias(p.name) for p in roster]}
            for tid, roster in rosters.items()
        ],
        "free_agents": [alias(p.name) for p in agents],
        "players": players,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=1))
    ranked = sum(1 for p in players if p["espn_rank"] is not None)
    console.print(
        f"[green]Wrote {len(players)} players "
        f"({len(rostered)} rostered, {len(agents)} free agents) "
        f"from {len(rosters)} teams to {out}. {ranked} carry an ESPN rank.[/green]"
    )
    if with_names:
        console.print(
            "[yellow]Real player names retained. Do not commit this file to a "
            "public repository.[/yellow]"
        )
    if not ranks:
        console.print(
            "[yellow]No player ranks came back. The kona_player_info field path is "
            "unverified for basketball — check it before trusting the ESPN column.[/yellow]"
        )


def _current_season() -> str:
    """The NBA season string for today, e.g. `2026-27`."""
    today = dt.date.today()
    start = today.year if today.month >= 10 else today.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def main() -> Any:
    return app()


if __name__ == "__main__":
    main()
