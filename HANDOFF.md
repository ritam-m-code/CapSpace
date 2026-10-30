# CapSpace handoff — 2026-10-29

Written at the halfway mark for whoever picks this up next, human or agent.

Three files, three jobs:

| File | Answers |
|---|---|
| `README.md` | the spec — what CapSpace is meant to do |
| `STATE.md` | what works right now: commands, counts, known issues |
| this file | **why** it is built this way, and what to do next |

**Read the README first, then `STATE.md`, then this.** The README is unchanged
except its Status section and the deployment target.

Branch: `feat/data-valuation-core`, not merged to `main`.

---

## Where things stand

The data and valuation half is done and works end to end against live NBA and
ESPN data. README checklist items 1–5 are shipped; 6–10 are not started.

`STATE.md` carries the current snapshot — test counts, runnable commands,
per-module status, fixture inventory and the ranked list of known issues. It
is not repeated here. What follows is the reasoning a fresh session cannot
recover from the code.

## The ESPN seam — now verified

Both seams flagged in the first draft of this file are **resolved**. The owner
supplied credentials on 2026-10-29 and the live league corrected three wrong
guesses. Recorded fixtures now come from a real league, with league and team
ids anonymized; rosters, names, statuses and ranks are real.

What was wrong, in case any of it regresses:

1. **The CLI never loaded `.env`.** `python-dotenv` was a declared dependency
   that was never called, so credentials placed exactly where
   `SETUP_CREDENTIALS.md` says to put them were reported as missing. Now
   `cli/main.py:load_credentials` loads the file, with the shell winning on
   conflict.

2. **`read_player_ranks` omitted the `x-fantasy-filter` header.** Without it
   ESPN returns a short alphabetical page: only 11 of 108 players got a rank,
   all with surnames starting A or B. With the header, 284 of 308 do. Any
   raw `kona_player_info` call needs this header and an explicit `limit`.

3. **The rank was read from the wrong field.** `draftRanksByRankType` is a
   *preseason draft* rank — Jokić is draft 1 but currently 9, Wembanyama
   draft 4 but currently 1. Using it prices every player as of October. The
   in-app rank is `entry["ratings"]["0"]["totalRanking"]`, where the split
   keys mean:

   | key | meaning |
   |---|---|
   | `0`, `2`, `3` | season-to-date (they agree) |
   | `1` | recent-form window |

   `SEASON_RATING_SPLIT = "0"` because perceived value is what the opposing
   manager sees in the app. **Split `1` is unexploited and valuable**: the
   gap between `0` and `1` is the "role changed four days ago" signal the
   README's streaming thesis depends on. Mitchell was season-29 / recent-7.
   Worth surfacing in Plan 2.

4. **ESPN sends `totalRanking: 0` to mean unranked**, not rank one. 21 of
   308 players in the real league, 17 of them `OUT` but 4 `ACTIVE`. A
   non-positive rank is now treated as absent. This is the exact failure the
   reader's own docstring warned about and it still shipped once, so treat
   any new numeric field from this payload as sentinel-bearing until proven
   otherwise.

5. **Real `eligibleSlots` carry `UT`, not `UTIL`**, plus combination slots
   (`G`, `F`, `G/F`, `SG/SF`, `PF/C`, `F/C`). Only `ATOMIC_POSITIONS` are
   kept. `ownership.percentOwned` is also available and is a second
   perceived-value signal nothing uses yet.

### Fixture privacy

This repo is **public**, so the committed ESPN fixtures are split: a fully
synthetic one drives the demo, and the real recording is anonymized to
placeholder names and ids while keeping every structural fact. See `STATE.md`
for which file does what. `capspace fixtures-record` anonymizes by default;
`--with-names` is for local files only. Do not commit a `--with-names` file.

### The live path runs

```bash
uv run capspace rank --limit 15
```

Executed successfully against the real league on 2026-10-29: 1569 game logs,
106 ESPN players matched to NBA ids, pool of 100.

### One caveat worth carrying forward

The spreads this currently produces are dominated by games played, not by
quality: ESPN ranks on season-to-date totals, the model on per-game rates.
Nine days into the season that is a systematic bias, not arbitrage. See
`STATE.md` issue 1 — it resolves itself as games accumulate, and a
`--min-games` filter is the product decision left open.

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

The four ESPN values are **in place** and the live path has been exercised.
Discord and AWS are not needed until Plan 3. Hashtag Basketball is optional
and deliberately deferred — their free rankings page covers the sanity check
the README describes.

ESPN cookies expire in roughly a year, sooner on logout or password change.
When they do, every command fails with an `EspnAuthError` naming both
variables; the fix is to re-copy them.
