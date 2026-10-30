# Current state

Snapshot as of **2026-10-30**. This file answers "what works right now?"
`HANDOFF.md` answers "why is it like this, and what next?" The README is the
spec.

Branch `feat/data-valuation-core`, **not merged to `main`**.

---

## Verification

```
188 tests passing · ruff clean · ruff format clean · pyright 0 errors
```

Reproduce the whole gate:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uvx pyright capspace tests
```

| Suite | Tests | Covers |
|---|---:|---|
| `test_espn.py` | 60 | rank field, filter header, status map, crosswalk, auth |
| `test_projections.py` | 24 | per-minute rates, shrinkage, minutes, projection |
| `test_metrics.py` | 23 | z-scores, percentage impact, pool eligibility |
| `test_cli_rank.py` | 23 | `rank` table, spread, sorting, `.env` loading |
| `test_ingestion.py` | 18 | game log and schedule parsing, caching |
| `test_schedule.py` | 15 | game counts, off-night density |
| `test_schemas.py` | 13 | model validation |
| `test_storage.py` | 12 | TTL, stale fallback, persistence |

No test touches the network. ~1475 lines of implementation under `capspace/`.

---

## What runs

### Live, against the real league

```bash
uv run capspace rank --limit 15
uv run capspace rank --view zscore --sort-by spread
uv run capspace fixtures-record
```

Credentials come from `.env`. Last live run (2026-10-29): 1569 game logs, 106 ESPN players matched to
NBA ids, pool of 100.

### Without credentials or network

```bash
CAPSPACE_FIXTURES=1 uv run capspace rank --view zscore
```

### Options

| Flag | Values | Default |
|---|---|---|
| `--view` | `compact`, `zscore` | `compact` |
| `--sort-by` | `spread`, `model`, `espn` | `spread` |
| `--window` | `<n>d` | `14d` |
| `--limit` | integer | `50` |

`zscore` renders at 170 columns; a narrower terminal elides headers.

---

## Component status

| Module | State | Verified against |
|---|---|---|
| `data/schemas.py` | done | unit tests |
| `data/storage.py` | done | unit tests |
| `data/ingestion.py` | done | **live `nba_api`** |
| `data/espn.py` | done | **live private league** |
| `engine/schedule.py` | done, **no caller yet** | real recorded schedule |
| `engine/projections.py` | done | real recorded game logs |
| `engine/metrics.py` | done | real recorded game logs |
| `cli/main.py` | `rank` + `fixtures-record` | fixtures and live run |
| `engine/simulate.py` | **not started** | — |
| `engine/streaming.py` | **not started** | — |
| `engine/trades.py` | **not started** | — |
| `bot/` | **not started** | — |
| `write/` | **not started** | — |

README checklist: items 1–5 done, 6–10 not started.

---

## Fixtures

| File | Source | Shape |
|---|---|---|
| `nba_gamelogs.json` | live `nba_api` | 10 players, 10 teams, Jan 2026 (**2025-26**) |
| `nba_schedule.json` | live `nba_api` | 466 rows, 30 teams, Jan 2026 |
| `espn_league.json` | **synthetic** | 10 players matching the NBA fixture |
| `espn_league_shape.json` | live league, **anonymized** | 308 players, 8 teams (2026-27) |

The NBA fixture's cast was chosen for edge cases, not stars: Keegan Murray
has 3 games (thin sample), Micah Peavy has 0 free-throw attempts across 14
games and is `OUT`.

The two ESPN fixtures do different jobs, and the split is deliberate because
**this repo is public**:

- `espn_league.json` drives the `CAPSPACE_FIXTURES=1` demo. Its ten names are
  the public NBA players in `nba_gamelogs.json`; the ranks and statuses are
  invented to exercise the spread, the unranked dash and the `OUT` exclusion.
  It contains **no data from any real league**.
- `espn_league_shape.json` is a real recording with **player names and ids
  replaced by placeholders** (`P001`, id `900000+`). Ranks, injury statuses,
  eligible slots, pro teams, team assignments and the rostered-versus-free-
  agent split are real, so it documents the distribution the readers must
  survive. `test_espn.py::TestRecordedLeagueShape` guards it.

`capspace fixtures-record` anonymizes by default and writes the shape file.
`--with-names` keeps real names for a local file you must not commit.

Neither committed fixture contains credentials or a league identifier.

The NBA and ESPN fixtures are from **different seasons** (2025-26 engine
fixture, 2026-27 league), so the demo's numbers are illustrative. The live
run is the meaningful one.

## Known issues

Ordered by how much they matter.

1. **Early-season bias in the headline output.** The season is 9 days old;
   max games played by anyone is 6, pool median 5. ESPN ranks on
   season-to-date *totals*, which punish missed games; the model uses
   per-game rates and is games-played-neutral. So every player who missed
   games reads as badly under-rated — the top 15 spreads were all 3-game
   players. Intended mechanism, but currently a systematic bias rather than
   selective arbitrage. A `--min-games` filter would fix the sort; **not
   added, because it is a product decision for the owner.**

2. **`_load_from_live` has no happy-path test.** It runs correctly, but only
   the auth-failure branch is covered, since the rest needs credentials.

3. **ESPN sends `totalRanking: 0` for unranked players** — 21 of 308 in the
   real league, 17 `OUT` but 4 `ACTIVE`. Non-positive ranks are now treated
   as absent; before the fix those four would have sorted as the best players
   in the league. Watch for other sentinel values in the same payload.

4. **Crosswalk miss rate unaudited.** 106 of 308 ESPN players matched to NBA
   ids. Most of the gap is players with no games yet, but this has not been
   checked. `match_players_by_name` drops ambiguous names silently by design.

5. **`_nba_name_index` re-reads the cache** to recover player names. Game
   logs should carry names through `ingestion.py` instead.

6. **`engine/schedule.py` has no caller.** The streaming MILP will be its
   first consumer.

7. **`read_settings` values unasserted.** `acquisition_limit` and
   `waiver_process_days` are read from the live settings object but were not
   compared against what the ESPN UI actually shows.

8. **Two ESPN signals unexploited.** `ratings["1"]` is a recent-form window —
   the gap between it and `ratings["0"]` is the "role changed four days ago"
   signal the streaming thesis depends on. `ownership.percentOwned` is a
   second perceived-value measure. Neither is used.

---

## Credentials

In place and exercised: `ESPN_S2`, `ESPN_SWID`, `ESPN_LEAGUE_ID`,
`ESPN_TEAM_ID`. Not needed yet: `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, AWS.

`.env` and `SETUP_CREDENTIALS.md` are gitignored and untracked — verified.
ESPN cookies last about a year, sooner on logout or password change; expiry
surfaces as an `EspnAuthError` naming both variables.

---

## Deployment

AWS, not Fly.io — existing credits. `t4g.nano` always-on, because the Discord
gateway connection cannot be serverless. Nothing is deployed yet; this
matters from Plan 3 onward.
