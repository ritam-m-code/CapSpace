# CapSpace Data & Valuation Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `capspace rank`, a CLI that prints every fantasy-relevant player's model value beside their ESPN perceived rank, sorted by the spread between them.

**Architecture:** A DuckDB-backed cache fronts two read-only sources (`nba_api` for box scores and schedule, ESPN for league state). Pure functions turn game logs into shrunk per-minute rates and projected minutes, then into volume-weighted category z-scores where percentage categories are z-scored on volume-weighted *impact* rather than on the rate. Every test runs against committed JSON fixtures; a `capspace fixtures record` subcommand refreshes them from live sources when credentials are present, so the suite never needs network or cookies.

**Tech Stack:** Python 3.11+, pydantic v2, duckdb, pandas, numpy, nba_api, espn-api, typer, rich, pytest, uv, ruff.

**Spec:** `README.md` (sections: The thesis, Method 1–2, Architecture, Stack, Interface, Setup)

## Global Constraints

- Python 3.11+; `uv` for env and running, `ruff` for lint and format, `pytest` for tests.
- The nine categories, in this order: `PTS, REB, AST, STL, BLK, 3PM, FG%, FT%, TO`. Turnovers are inverted (lower is better).
- Percentage categories are **never** z-scored on the rate. They are z-scored on volume-weighted impact: `(player_pct - league_mean_pct) * player_attempts`.
- Player pool is the top ~150 fantasy-relevant players; `POOL_SIZE = 150`.
- DuckDB cache TTL is 12 hours.
- Default rolling window is 14 days (≈ 6 games).
- Read and write credentials live on separate code paths. This plan creates **no** write path and must import nothing from `capspace/write/`.
- League settings — roster slots, lineup lock type, acquisition limits, scoring categories — are read from the ESPN API, never hand-configured.
- Credentials come from `.env` only: `ESPN_S2`, `ESPN_SWID`, `ESPN_LEAGUE_ID`, `ESPN_TEAM_ID`. Never commit them; `.env.example` carries the keys with empty values.
- No test may make a network call.
- **One deviation from the spec's file map, deliberate:** the README puts injury status in `data/ingestion.py`. It lives in `data/espn.py` instead, because ESPN's fantasy status (`OUT`/`DTD`/`INACTIVE`) is what actually gates a lineup slot, while `nba_api`'s status is editorial and lags. `Player.status` has exactly one source — ESPN. Do not add a second.

## Review Focus

Five input classes the spec implies but no task's happy path exercises. Each has its test pinned to the task that owns the code.

1. **Zero attempts in window** — a player with 0 FGA or 0 FTA must get impact `0.0`, never `NaN` or a division error. (Task 7)
2. **Thin sample** — a player with fewer than 5 games must have per-minute rates dominated by the season prior, not by the 1–4 observed games. (Task 6)
3. **Inactive and two-way players** — anyone with status `OUT`, `INACTIVE`, or no NBA minutes must be excluded from the 150-player pool rather than ranked with zeros. (Task 7)
4. **Expired ESPN cookies** — an auth failure must raise a named error naming the fix, not return an empty roster that silently reads as "you have no players." (Task 4)
5. **Stale cache with a dead upstream** — past the 12h TTL, if the source raises, serve the stale rows with a warning rather than returning empty. (Task 2)

---

### Task 1: Project scaffold and schemas

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore`, `capspace/__init__.py`, `capspace/data/__init__.py`, `capspace/data/schemas.py`
- Test: `tests/test_schemas.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `CATEGORIES: tuple[str, ...]` = `("PTS","REB","AST","STL","BLK","3PM","FG%","FT%","TO")`; `POOL_SIZE: int = 150`; pydantic v2 models `GameLog`, `Player`, `RosterSlot`, `LeagueSettings`, `CategoryLine`. `Player` fields: `nba_id: int`, `name: str`, `team: str`, `positions: list[str]`, `status: Literal["ACTIVE","DTD","OUT","INACTIVE"]`, `espn_id: int | None`, `espn_rank: int | None`. `GameLog` fields: `nba_id: int`, `game_date: date`, `minutes: float`, plus `pts, reb, ast, stl, blk, fg3m, fgm, fga, ftm, fta, tov` as `int`. `LeagueSettings` fields: `league_id: int`, `team_id: int`, `categories: list[str]`, `roster_slots: dict[str, int]`, `acquisition_limit: int`, `waiver_days: int`, `lineup_lock: Literal["DAILY","WEEKLY"]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_categories_are_the_nine_in_spec_order():
    assert CATEGORIES == ("PTS","REB","AST","STL","BLK","3PM","FG%","FT%","TO")

def test_gamelog_rejects_negative_minutes():
    with pytest.raises(ValidationError):
        GameLog(nba_id=1, game_date=date(2026,1,1), minutes=-1.0, pts=0, reb=0,
                ast=0, stl=0, blk=0, fg3m=0, fgm=0, fga=0, ftm=0, fta=0, tov=0)

def test_gamelog_rejects_makes_exceeding_attempts():
    with pytest.raises(ValidationError):
        GameLog(nba_id=1, game_date=date(2026,1,1), minutes=30.0, pts=0, reb=0,
                ast=0, stl=0, blk=0, fg3m=0, fgm=9, fga=4, ftm=0, fta=0, tov=0)

def test_league_settings_round_trips_through_json():
    s = LeagueSettings(league_id=1, team_id=2, categories=list(CATEGORIES),
                       roster_slots={"PG":1,"BE":3}, acquisition_limit=7,
                       waiver_days=1, lineup_lock="DAILY")
    assert LeagueSettings.model_validate_json(s.model_dump_json()) == s
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: capspace.data.schemas`

- [ ] **Step 3: Write `pyproject.toml`**

`requires-python = ">=3.11"`. Runtime deps: `nba_api`, `espn-api`, `pandas`, `numpy`, `pydantic>=2`, `duckdb`, `typer`, `rich`, `python-dotenv`. Dev deps: `pytest`, `ruff`. Configure `ruff` and set `pytest` testpaths to `tests`. Add `.gitignore` covering `.env`, `.venv/`, `__pycache__/`, `*.duckdb`.

- [ ] **Step 4: Implement the constants and models in `capspace/data/schemas.py`**

Use pydantic v2 `field_validator` for the non-negative-minutes rule and a `model_validator(mode="after")` for the two makes-vs-attempts rules (`fgm <= fga`, `ftm <= fta`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_schemas.py -v && uv run ruff check .`
Expected: PASS, no lint findings.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore .env.example capspace/ tests/
git commit -m "Add project scaffold and core schemas"
```

---

### Task 2: DuckDB cache with 12h TTL

**Files:**
- Create: `capspace/data/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: Task 1 schemas.
- Produces: `class Cache` with `__init__(self, path: Path)`, and
  `get_or_fetch(self, key: str, fetch: Callable[[], pd.DataFrame], ttl_hours: float = 12.0) -> pd.DataFrame`.
  Also `class StaleCacheWarning(UserWarning)`. Tables: `cache_entries(key TEXT PRIMARY KEY, fetched_at TIMESTAMP)` and one payload table per key written via `df.to_sql`-equivalent DuckDB registration.

- [ ] **Step 1: Write the failing tests**

```python
def test_first_call_fetches_and_caches(tmp_path):
    c, calls = Cache(tmp_path / "t.duckdb"), []
    def fetch():
        calls.append(1)
        return pd.DataFrame({"a": [1]})
    c.get_or_fetch("k", fetch)
    c.get_or_fetch("k", fetch)
    assert len(calls) == 1

def test_fetches_again_once_ttl_expires(tmp_path):
    c, calls = Cache(tmp_path / "t.duckdb"), []
    def fetch():
        calls.append(1)
        return pd.DataFrame({"a": [1]})
    c.get_or_fetch("k", fetch)
    expire(c, "k", hours=13)        # test helper: rewrite fetched_at to 13h ago
    c.get_or_fetch("k", fetch)
    assert len(calls) == 2

def test_serves_stale_rows_with_warning_when_fetch_fails(tmp_path):
    c = Cache(tmp_path / "t.duckdb")
    c.get_or_fetch("k", lambda: pd.DataFrame({"a": [1]}))
    expire(c, "k", hours=13)
    def boom(): raise ConnectionError("upstream down")
    with pytest.warns(StaleCacheWarning):
        got = c.get_or_fetch("k", boom)
    assert got.equals(pd.DataFrame({"a": [1]}))   # Review Focus 5

def test_propagates_fetch_error_when_no_stale_rows_exist(tmp_path):
    def boom(): raise ConnectionError("upstream down")
    with pytest.raises(ConnectionError):
        Cache(tmp_path / "t.duckdb").get_or_fetch("k", boom)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -v`
Expected: FAIL — `ImportError: cannot import name 'Cache'`

- [ ] **Step 3: Implement `Cache` in `capspace/data/storage.py`**

Freshness is `now - fetched_at < ttl_hours`. On a miss or expiry, call `fetch()`; if it raises and a stale payload exists, `warnings.warn(..., StaleCacheWarning)` and return the stale frame; if it raises with no payload, re-raise. Create tables on first connect so a fresh `path` works with no migration step.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_storage.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add capspace/data/storage.py tests/test_storage.py
git commit -m "Add DuckDB cache with 12h TTL and stale fallback"
```

---

### Task 3: nba_api ingestion, fixture-backed

**Files:**
- Create: `capspace/data/ingestion.py`, `tests/fixtures/nba_gamelogs.json`, `tests/fixtures/nba_schedule.json`
- Test: `tests/test_ingestion.py`

**Interfaces:**
- Consumes: Task 1 `GameLog`, Task 2 `Cache`.
- Produces: `fetch_game_logs(season: str, cache: Cache) -> list[GameLog]`,
  `fetch_schedule(season: str, cache: Cache) -> pd.DataFrame` with columns `game_date: date, team: str, opponent: str`,
  and `load_fixture(name: str) -> dict` reading `tests/fixtures/<name>.json`.

Fixtures hold a trimmed real-shape `nba_api` response: ≥ 8 players across ≥ 3 teams, ≥ 10 games each, including one player with 2 games (feeds Review Focus 2) and one with 0 FTA across the window (feeds Review Focus 1).

- [ ] **Step 1: Write the failing tests**

```python
def test_parses_fixture_gamelogs_into_models(monkeypatch, tmp_path):
    # monkeypatch the nba_api call to return the fixture payload
    logs = fetch_game_logs("2025-26", Cache(tmp_path / "t.duckdb"))
    assert all(isinstance(l, GameLog) for l in logs)
    assert len({l.nba_id for l in logs}) >= 8

def test_minutes_parse_from_mm_ss_strings(...):
    # nba_api returns "31:24"; assert the parsed value == pytest.approx(31.4, abs=0.05)

def test_schedule_has_one_row_per_team_per_game(...):
    assert set(sched.columns) == {"game_date", "team", "opponent"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingestion.py -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_game_logs'`

- [ ] **Step 3: Implement the fetchers in `capspace/data/ingestion.py`**

Use `nba_api.stats.endpoints.playergamelogs` for logs and `scoreboardv2`/`leaguegamefinder` for the schedule, each wrapped in `cache.get_or_fetch`. Minutes arrive as `"MM:SS"` strings — parse to float minutes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ingestion.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add capspace/data/ingestion.py tests/test_ingestion.py tests/fixtures/
git commit -m "Add nba_api game log and schedule ingestion"
```

---

### Task 4: ESPN league reads

**Files:**
- Create: `capspace/data/espn.py`, `tests/fixtures/espn_league.json`
- Test: `tests/test_espn.py`

**Interfaces:**
- Consumes: Task 1 `LeagueSettings`, `Player`, `RosterSlot`; Task 2 `Cache`.
- Produces: `class EspnAuthError(RuntimeError)`;
  `read_settings(client) -> LeagueSettings`;
  `read_rosters(client) -> dict[int, list[Player]]` keyed by ESPN team id;
  `read_free_agents(client) -> list[Player]`;
  `read_player_ranks(client) -> dict[int, int]` mapping `espn_id -> espn_rank`;
  `build_client(creds: EspnCreds) -> League` where `EspnCreds` is a frozen dataclass of the four `.env` values.

This module is **read-only**. It must not import `requests` for any non-GET verb.

- [ ] **Step 1: Write the failing tests**

```python
def test_settings_come_from_the_api_not_defaults(fake_client):
    s = read_settings(fake_client)
    assert s.acquisition_limit == 7 and s.waiver_days == 1
    assert s.lineup_lock == "DAILY"
    assert s.categories == list(CATEGORIES)

def test_rosters_carry_espn_rank_for_perceived_value(fake_client):
    rosters = read_rosters(fake_client)
    assert all(p.espn_rank is not None for p in rosters[1])

def test_expired_cookies_raise_a_named_error_naming_the_fix(fake_client_401):
    with pytest.raises(EspnAuthError, match="ESPN_S2"):   # Review Focus 4
        read_rosters(fake_client_401)

def test_free_agents_exclude_rostered_players(fake_client):
    rostered = {p.nba_id for ps in read_rosters(fake_client).values() for p in ps}
    assert not {p.nba_id for p in read_free_agents(fake_client)} & rostered
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_espn.py -v`
Expected: FAIL — `ImportError: cannot import name 'read_settings'`

- [ ] **Step 3: Implement the readers in `capspace/data/espn.py`**

Wrap `espn_api.basketball.League`. Map ESPN's `scoringPeriodId`/lock fields onto `lineup_lock`, and its stat-id category list onto `CATEGORIES` names. Catch the 401/invalid-cookie failure and re-raise as `EspnAuthError` whose message names `ESPN_S2` and `ESPN_SWID` and says to re-copy them from DevTools.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_espn.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add capspace/data/espn.py tests/test_espn.py tests/fixtures/espn_league.json
git commit -m "Add read-only ESPN league state integration"
```

---

### Task 5: Schedule grid and off-night density

**Files:**
- Create: `capspace/engine/__init__.py`, `capspace/engine/schedule.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Consumes: Task 3 `fetch_schedule`.
- Produces: `weekly_game_counts(sched: pd.DataFrame, start: date, end: date) -> dict[str, int]`;
  `off_night_density(sched: pd.DataFrame, start: date, end: date) -> dict[date, float]` returning, per day, the fraction of the 30 teams **not** playing;
  `plays_on(sched: pd.DataFrame, team: str, day: date) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_counts_games_per_team_in_the_window():
    assert weekly_game_counts(sched, date(2026,1,5), date(2026,1,11))["BOS"] == 4

def test_window_bounds_are_inclusive():
    # a game exactly on `end` is counted

def test_off_night_density_is_fraction_of_teams_idle():
    # 6 of 30 teams play that day
    assert off_night_density(sched, d, d)[d] == pytest.approx(24/30)

def test_days_with_no_games_have_density_one():
    assert off_night_density(sched, d, d)[d] == 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schedule.py -v`
Expected: FAIL — `ModuleNotFoundError: capspace.engine.schedule`

- [ ] **Step 3: Implement the three functions in `capspace/engine/schedule.py`**

Density denominator is a fixed 30, not the count of teams appearing in the frame, so a trimmed fixture still yields correct fractions.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add capspace/engine/ tests/test_schedule.py
git commit -m "Add schedule grid and off-night density"
```

---

### Task 6: Projections — shrunk per-minute rates and projected minutes

**Files:**
- Create: `capspace/engine/projections.py`
- Test: `tests/test_projections.py`

**Interfaces:**
- Consumes: Task 1 `GameLog`.
- Produces: `per_minute_rates(logs: list[GameLog], window_days: int = 14, asof: date | None = None) -> pd.DataFrame` indexed by `nba_id` with one column per raw counting stat (`pts, reb, ast, stl, blk, fg3m, fgm, fga, ftm, fta, tov`) expressed per minute;
  `shrink_to_prior(window: pd.DataFrame, prior: pd.DataFrame, window_minutes: pd.Series, k: float = 200.0) -> pd.DataFrame`;
  `projected_minutes(logs: list[GameLog], window_days: int = 14, asof: date | None = None) -> pd.Series`;
  `project(logs: list[GameLog], window_days: int = 14, asof: date | None = None) -> pd.DataFrame` returning projected **per-game** counting stats, indexed by `nba_id`.

- [ ] **Step 1: Write the failing tests**

```python
def test_per_minute_rates_divide_totals_by_total_minutes():
    # one player, 2 games: 30 pts in 30 min, 10 pts in 10 min -> 1.0 pts/min
    assert rates.loc[1, "pts"] == pytest.approx(1.0)

def test_shrinkage_weight_is_window_minutes_over_window_minutes_plus_k():
    # window rate 1.0, prior rate 0.5, 200 window minutes, k=200 -> 0.75
    assert out.loc[1, "pts"] == pytest.approx(0.75)

def test_thin_sample_is_dominated_by_the_prior():   # Review Focus 2
    # a 2-game, 40-minute player with window rate 1.0 and prior 0.5
    # lands within 0.05 of the prior, not of the window rate
    assert abs(out.loc[1, "pts"] - 0.5) < 0.05

def test_projected_minutes_weight_recent_games_above_older_ones():
    # a player at 10 mpg for 10 games then 34 mpg for the last 3
    # projects above the 15.5 flat mean
    assert projected_minutes(logs)[1] > 15.5

def test_project_multiplies_shrunk_rates_by_projected_minutes():
    assert proj.loc[1, "pts"] == pytest.approx(rates.loc[1, "pts"] * mins[1])

def test_window_excludes_games_older_than_window_days():
    # a 40-pt game 20 days before `asof` does not move a 14-day window
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_projections.py -v`
Expected: FAIL — `ModuleNotFoundError: capspace.engine.projections`

- [ ] **Step 3: Implement the four functions in `capspace/engine/projections.py`**

Shrinkage is the standard credibility weight, applied per stat column:

```
w = window_minutes / (window_minutes + k)
shrunk = w * window_rate + (1 - w) * prior_rate
```

The prior is the player's season-to-date per-minute rate; a player with no season history falls back to the pool mean per-minute rate. Projected minutes are an exponentially weighted mean of per-game minutes over the window with a 5-game half-life, so a role change three games ago moves the estimate — this is the term the spec says carries nearly all the variance. Games with `minutes == 0` are dropped from the minutes estimate but kept out of rate denominators too.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_projections.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add capspace/engine/projections.py tests/test_projections.py
git commit -m "Add per-minute rates, shrinkage, and minutes projection"
```

---

### Task 7: Volume-weighted category z-scores

**Files:**
- Create: `capspace/engine/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: Task 1 `CATEGORIES`, `POOL_SIZE`, `Player`; Task 6 `project`.
- Produces: `eligible_pool(players: list[Player], proj: pd.DataFrame, size: int = POOL_SIZE) -> pd.Index`;
  `pct_impact(proj: pd.DataFrame, kind: Literal["FG","FT"]) -> pd.Series`;
  `category_zscores(proj: pd.DataFrame, pool: pd.Index) -> pd.DataFrame` with exactly the `CATEGORIES` columns;
  `model_value(z: pd.DataFrame) -> pd.Series` — the row sum, descending-sortable.

- [ ] **Step 1: Write the failing tests**

```python
def test_turnovers_are_inverted_so_fewer_is_better():
    # two players identical but for TO; the lower-TO player has the higher TO z
    assert z.loc[low_to, "TO"] > z.loc[high_to, "TO"]

def test_pct_categories_are_zscored_on_impact_not_on_rate():
    # A: 90% FT on 1 attempt/game. B: 80% FT on 10 attempts/game. Pool mean 75%.
    # Rate-based scoring would rank A above B; impact must rank B above A.
    assert z.loc["B", "FT%"] > z.loc["A", "FT%"]

def test_impact_is_mean_delta_times_attempts():
    # 0.80 FG on 20 FGA vs pool mean 0.47 -> impact == pytest.approx(6.6)
    assert pct_impact(proj, "FG")[1] == pytest.approx((0.80 - 0.47) * 20)

def test_zero_attempts_give_zero_impact_not_nan():   # Review Focus 1
    assert pct_impact(proj, "FT")[no_fta_player] == 0.0
    assert not z["FT%"].isna().any()

def test_out_and_inactive_players_are_excluded_from_the_pool():   # Review Focus 3
    pool = eligible_pool(players, proj)
    assert out_player.nba_id not in pool
    assert inactive_player.nba_id not in pool

def test_pool_is_capped_at_size_by_descending_model_value():
    assert len(eligible_pool(players, proj, size=5)) == 5

def test_zscores_are_computed_over_the_pool_only():
    # a sub-pool scrub does not shift the pool mean
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: capspace.engine.metrics`

- [ ] **Step 3: Implement the four functions in `capspace/engine/metrics.py`**

Counting categories: `(value - pool_mean) / pool_std`, with the `TO` column negated after standardizing. Percentage categories, per the spec:

```
impact_i = (pct_i - pool_mean_pct) * attempts_i
```

where `pool_mean_pct` is the **volume-weighted** pool rate (`sum(makes) / sum(attempts)`), not the mean of per-player rates. A player with zero attempts gets impact `0.0`. The impact series is then standardized like any counting category.

Two column-name mappings between `proj` and `CATEGORIES`: `fg3m -> 3PM` and `tov -> TO`. `FG%` and `FT%` columns do not exist in `proj` — they are derived from `fgm/fga` and `ftm/fta` by `pct_impact`.

`eligible_pool` is **two-pass**, which is what keeps it non-circular: drop `status in {"OUT","INACTIVE"}` and anyone with projected minutes of 0, standardize over *all* survivors to get a provisional value, take the top `size` by that value, and return their index. `category_zscores` then re-standardizes over only that returned pool. Do not call `model_value` from inside `eligible_pool`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add capspace/engine/metrics.py tests/test_metrics.py
git commit -m "Add volume-weighted category z-scores"
```

---

### Task 8: `capspace rank` CLI

**Files:**
- Create: `capspace/cli/__init__.py`, `capspace/cli/main.py`
- Modify: `pyproject.toml` (add the `capspace` console script entry point)
- Test: `tests/test_cli_rank.py`

**Interfaces:**
- Consumes: every prior task.
- Produces: `app: typer.Typer` with two commands —
  `rank(view: str = "zscore", window: str = "14d", sort_by: str = "spread", limit: int = 50)` and
  `fixtures_record()` (`capspace fixtures-record`, writes `tests/fixtures/*.json` from live sources; requires credentials, never run by the suite).
  Also `spread(model_rank: pd.Series, espn_rank: pd.Series) -> pd.Series`, defined as `espn_rank - model_rank`, so a positive spread means ESPN under-rates the player relative to the model.

- [ ] **Step 1: Write the failing tests**

```python
def test_spread_is_positive_when_espn_underrates():
    # model rank 5, ESPN rank 40 -> +35
    assert spread(pd.Series({1: 5}), pd.Series({1: 40}))[1] == 35

def test_rank_sorted_by_spread_puts_the_biggest_underrate_first(fixture_env):
    out = CliRunner().invoke(app, ["rank", "--sort-by", "spread"])
    assert out.exit_code == 0
    assert out.stdout.index("UnderratedGuy") < out.stdout.index("FairlyRatedGuy")

def test_rank_respects_limit(fixture_env):
    out = CliRunner().invoke(app, ["rank", "--limit", "3"])
    assert sum(1 for p in PLAYER_NAMES if p in out.stdout) == 3

def test_players_without_an_espn_rank_sort_last_and_show_a_dash(fixture_env):
    assert "—" in out.stdout

def test_rank_exits_nonzero_with_a_readable_message_on_auth_failure(bad_creds_env):
    out = CliRunner().invoke(app, ["rank"])
    assert out.exit_code != 0 and "ESPN_S2" in out.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_rank.py -v`
Expected: FAIL — `ModuleNotFoundError: capspace.cli.main`

- [ ] **Step 3: Implement the CLI in `capspace/cli/main.py`**

`model_rank` is `model_value(z).rank(ascending=False, method="min").astype(int)` — rank 1 is the most valuable player. `espn_rank` comes from `read_player_ranks`, joined on `espn_id`.

Render with `rich.table.Table`: columns `Player, Team, Pos, Model, ESPN, Spread`, plus one column per category when `--view zscore`. Players with `espn_rank is None` render `—` and sort last regardless of `--sort-by`. Catch `EspnAuthError` and exit non-zero with its message. `--window` parses a `<n>d` string into `window_days`.

- [ ] **Step 4: Run the whole suite and the lint gate**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check .`
Expected: all tests PASS, no lint or format findings.

- [ ] **Step 5: Verify the CLI runs end to end against fixtures**

Run: `CAPSPACE_FIXTURES=1 uv run capspace rank --limit 5`
Expected: a five-row table with populated Model, ESPN, and Spread columns.

- [ ] **Step 6: Commit**

```bash
git add capspace/cli/ pyproject.toml tests/test_cli_rank.py
git commit -m "Add capspace rank CLI with model-vs-ESPN spread"
```

---

## Out of scope for this plan

Deferred to Plan 2 (`simulate`, `stream`, `trade`) and Plan 3 (Discord bot, digest, write path): `capspace/engine/simulate.py`, `capspace/engine/streaming.py`, `capspace/engine/trades.py`, `capspace/bot/`, `capspace/write/`. Task 7's z-scores are the pruning heuristic those plans consume.
