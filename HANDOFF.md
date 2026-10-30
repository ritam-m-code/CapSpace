# CapSpace handoff — 2026-10-29

Written at the halfway mark for whoever picks this up next, human or agent.
The README is the spec and has not changed except its Status section and the
deployment target. **Read the README first, then this.**

Branch: `feat/data-valuation-core`, 11 commits, not merged to `main`.

---

## Where things stand

The data and valuation half is done and works end to end. Checklist items 1–5
of the README are shipped; 6–10 are not started.

```
169 tests passing · ruff clean · ruff format clean · pyright 0 errors
```

Run it:

```bash
uv sync --extra dev
CAPSPACE_FIXTURES=1 uv run capspace rank --view zscore --sort-by spread
```

That needs no credentials and no network. It prints the product's core
artifact: every pooled player's model value beside ESPN's perceived rank,
sorted by the gap.

### What exists

| File | Does | Verified against |
|---|---|---|
| `data/schemas.py` | pydantic v2 models, `CATEGORIES`, `POOL_SIZE` | — |
| `data/storage.py` | DuckDB cache, 12h TTL, stale-on-failure | unit tests |
| `data/ingestion.py` | `nba_api` game logs + schedule | **live API** |
| `data/espn.py` | ESPN reads, status map, name crosswalk | `espn_api` package source |
| `engine/schedule.py` | weekly game counts, off-night density | real recorded schedule |
| `engine/projections.py` | per-minute rates, shrinkage, minutes | real recorded game logs |
| `engine/metrics.py` | volume-weighted category z-scores | real recorded game logs |
| `cli/main.py` | `capspace rank`, `capspace fixtures-record` | fixtures |

Fixtures in `tests/fixtures/`:

- `nba_gamelogs.json`, `nba_schedule.json` — **recorded from the live
  `nba_api`** on 2026-10-29. 10 players, 10 teams, Jan 2026 window. Recorded
  by `scripts/record_nba_fixtures.py`.
- `espn_league.json` — **synthetic.** Shaped to `espn_api` attribute names,
  not recorded. This is the main thing to fix.

The fixture cast was chosen to carry edge cases, not just stars: Keegan
Murray has 3 games (thin sample), Micah Peavy has 0 free-throw attempts
across 14 games and is `OUT`.

---

## The two unverified seams

Both need ESPN private-league cookies. Nothing else is blocked.

### 1. ESPN response shapes are modeled, not recorded

`data/espn.py` was written against the installed `espn_api` 0.46 source, so
the attribute names (`playerId`, `proTeam`, `injuryStatus`, `eligibleSlots`,
`settings.acquisition_limit`, `settings.waiver_process_days`) are right for
that package version. What is unverified is how a *real* league populates
them.

**Fix in one command**, once `.env` has the four ESPN values:

```bash
uv run capspace fixtures-record
```

Then re-run the suite. Any mismatch surfaces in `tests/test_espn.py`.

### 2. `read_player_ranks` field path is community-mapped

This is the riskiest line in the codebase and it matters more than its size
suggests, because ESPN's overall rank **is** the perceived-value column the
entire thesis rests on.

The finding: `espn_api`'s basketball `Player` exposes `posRank` (positional)
but no overall rank and no `percent_owned` — its football and baseball
equivalents do, basketball does not. So the rank is read from the raw
`kona_player_info` view at:

```
players[].player.draftRanksByRankType.STANDARD.rank
```

That path has not been run against a live private basketball league. If
`fixtures-record` prints the yellow "No player ranks came back" warning, this
is why, and the fix is to dump the raw payload and find where the rank
actually lives. Everything else in the pipeline is correct regardless.

---

## Decisions made that the README does not state

Carry these forward; they are not arbitrary.

1. **Spread is computed on pool-relative ranks**, not raw ones. Model rank
   runs 1..150 within the pool; ESPN's is global over every ranked player.
   Subtracting them directly makes every fringe player look like a steal —
   the first working version ranked a 14-minute-a-night player as the biggest
   arbitrage in the league. `cli/main.py:pool_ranks` re-ranks ESPN's ordering
   within the pool so the two are subtractable. Keep this.

2. **`Player.status` has exactly one source: ESPN.** The README's file map
   puts injury status in `ingestion.py`; it is in `espn.py` instead, because
   ESPN's fantasy status is what gates a lineup slot while `nba_api`'s is
   editorial and lags. Do not add a second source.

3. **An unknown ESPN status maps to `ACTIVE`, not to unavailable.** ESPN adds
   status strings over time. Wrongly including a player is recoverable;
   wrongly excluding one silently removes them from the pool.

4. **An empty ESPN read is raised as `EspnAuthError`.** Expired cookies
   return a well-formed response with nothing in it, which would otherwise
   score as a roster of zeros.

5. **`eligible_pool` is two-pass.** Standardize over all available players
   for a provisional value, take the top N, then re-standardize over that
   pool. A single pass is circular, since the pool defines the z-scores that
   define the pool.

6. **Pool percentage baselines are volume-weighted** (`sum(makes) /
   sum(attempts)`), not the mean of per-player rates. The mean of rates
   overweights players who barely shoot — the same error the impact formula
   exists to correct.

7. **A category with zero spread yields zeros, not NaN.** If every pooled
   player blocks the same amount, blocks carry no information and must
   contribute nothing rather than poison the row sum.

8. **`project()` returns per-game counting stats only.** `FG%`/`FT%` are
   never stored, always derived from `fgm/fga` and `ftm/fta`, so a rate can
   never drift out of sync with its volume.

9. **The cache guarantees values, not dtypes.** DuckDB widens `date` to
   `datetime64[us]`. Pinned in `test_date_columns_come_back_as_datetime64`;
   `_parse_game_date` normalizes on the way back in.

10. **Deployment target is AWS, not Fly.io.** `t4g.nano` always-on, because
    the Discord gateway connection cannot be serverless. Cost table updated.

### Evidence the valuation is right

On the real recorded data, Keegan Murray shoots **100% from the free-throw
line on 0.7 attempts per game**. Rate-based z-scores rank him **1st of 10**
in FT%. Volume-weighted impact correctly drops him to **5th**, and lifts
Shai Gilgeous-Alexander (91% on 9.8 attempts) from 3rd to **1st**. Luka
Dončić's 74.7% on 10.3 attempts scores **-1.89**, the worst in the pool.

That is the README's headline claim, reproduced. If a refactor breaks it,
`test_percentage_categories_use_impact_not_rate` is the test that fires.

---

## What to do next

The remaining work splits into two plans, in dependency order. Neither is
written yet; write the plan before the code, the way
`docs/superpowers/plans/2026-10-29-capspace-data-valuation-core.md` was.

### Plan 2 — Simulation and allocation (README items 6–8)

1. **`engine/simulate.py`** — Monte Carlo matchup. Sample per-game category
   lines (negative binomial for counting stats, beta-binomial on attempts for
   percentages), aggregate across roster and scheduled games, run 10k weeks,
   report per-category win probability and P(win ≥ 5 of 9).
   Thread the seed as an argument; do not call a global RNG seeder.
   This is the real objective function — z-scores are only the pruning
   heuristic. Punt strategies must *emerge*; do not add a punt mask.
2. **`engine/streaming.py`** — PuLP/CBC MILP. Binary variable per free agent
   per day, maximize expected category contribution, constrained by the
   7-add cap, daily slot limits, `plays_on`, and a 1-day waiver commit.
   Output is a schedule, not a ranked list.
3. **`engine/trades.py`** — candidate search, acceptance scoring, pitch text.
   The filter is the intersection: positive under model value **and**
   positive-looking under ESPN rank from the counterparty's side.

### Plan 3 — Delivery and writes (README items 9–10)

4. **`bot/`** — discord.py commands and the morning digest, which posts only
   when something material changed against the previous run.
5. **`write/`** — ESPN write path, dry-run by default, with `guards.py`:
   protected players, value ceiling, rate limits. **Separate credentials from
   the read path.** No transaction without an explicit click.

### Housekeeping worth doing early

- `_nba_name_index` in `cli/main.py` re-reads the cached frame to recover
  player names. Game logs should carry names through `ingestion.py` instead.
- `engine/schedule.py` is written but nothing consumes it yet; the streaming
  MILP is its first caller.
- The live `rank` path (`_load_from_live`) has never been executed. It is
  covered only by the auth-failure test.

---

## Conventions in force

- `uv` for everything, `ruff` for lint and format, `pytest`. Not pip.
- TDD: failing test, verify it fails for the right reason, implement, verify
  it passes, commit. Every commit in this branch followed that.
- Commits are author-only — no co-author trailers.
- Type-annotate every public function. `uvx pyright capspace tests` must be
  clean. pandas-stubs types `groupby().sum()` loosely; `cast` at the boundary
  rather than loosening the annotation.
- **No test may touch the network.** Live calls go in `scripts/` or a
  `fixtures-record` command.
- Show the command and its output before claiming something passes.

## Credentials

See `SETUP_CREDENTIALS.md` (gitignored, local only) for how to obtain each
value. `.env` is gitignored; `.env.example` lists the keys.

Still needed from the owner: the four ESPN values. Discord and AWS are not
needed until Plan 3. Hashtag Basketball is optional and deliberately
deferred — their free rankings page covers the sanity check the README
describes.
