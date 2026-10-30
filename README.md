# CapSpace

Arbitrage, daily schedule optimization, and multi-category trade evaluation for competitive NBA fantasy leagues.

CapSpace is a quantitative decision engine for head-to-head 9-category fantasy basketball. It runs a nightly research pipeline against live NBA and ESPN Fantasy data, then delivers ranked, actionable recommendations through a Discord bot — trades worth proposing, free agents worth streaming, and the optimal daily lineup.

---

## The thesis

Most fantasy tools answer "who is good?" CapSpace answers "where is my league mispricing someone, and can I act on it?"

That requires three separate valuations of every player, and the gap between them is the entire product:

| Valuation | Role | Source |
|---|---|---|
| **Model value** | Truth, as well as it can be computed | This engine |
| **Sharp consensus** | Validation — catches model bugs | Hashtag Basketball (manual check) |
| **Perceived value** | What the opposing manager believes | ESPN's in-app player rank |

A trade is worth proposing only when it is **positive under model value** and **positive-looking under ESPN rank from the counterparty's perspective**. That intersection is the default filter on every recommendation.

The same logic drives streaming: ESPN ranks on season-to-date totals, so a player whose role changed four days ago is still ranked where they were before the change.

---

## Method

### 1. Valuation — volume-weighted category z-scores

Per-game z-scores across the top ~150 fantasy-relevant players for the nine categories (PTS, REB, AST, STL, BLK, 3PM, FG%, FT%, TO):

$$Z_{i,c} = \frac{\mu_{i,c} - \bar{\mu}_c}{\sigma_c}$$

with turnovers inverted. **Percentage categories are not z-scored on the rate.** Weekly team FG% is a volume-weighted aggregate, so the quantity that matters is impact:

$$\text{Impact}_{i,\text{FG}} = (\mu_{i,\text{FG}\%} - \bar{\mu}_{\text{FG}\%}) \times \text{FGA}_i$$

and that is z-scored instead. Rate-based percentage z-scores are the single most common error in public fantasy rankings — they rank a 90% free-throw shooter taking one attempt per game above an elite high-volume scorer.

### 2. Projection — rates times minutes

Production is decomposed into a stable component and a volatile one:

- **Per-minute rates** are computed from `nba_api` game logs and shrunk toward a season prior. These barely move.
- **Projected minutes** carry nearly all the variance in fantasy value, and are driven by information absent from box scores: rotation changes, injuries, load management, back-to-backs.

Short rolling windows (14 days is roughly 6 games) are too noisy to z-score directly, so recent form is expressed as a minutes and usage adjustment on stable rates rather than as a raw rolling average.

### 3. Objective — category win probability, not total z-score

Head-to-head 9-cat is nine weekly coin flips against one opponent, not a sum. Two rosters with identical aggregate z-score can have very different expected category wins — one balanced roster wins 5-4 every week, a lumpy one alternates 7-2 and 2-7.

CapSpace simulates the matchup directly: sample per-game category lines (negative binomial for counting stats, beta-binomial on attempts for percentages), aggregate across roster and scheduled games, run 10k weeks, and report the per-category win probability and the probability of winning at least 5 of 9.

Two consequences:

- **Punt strategies are emergent.** No punt mask is needed; the optimizer discovers that conceding FT% and TO raises the probability of winning the matchup.
- **Points leagues** are a different scoring function over the same sampler.

Z-scores remain as a fast heuristic for pruning candidates before simulation.

### 4. Allocation — MILP under real league constraints

Streaming is a budgeted resource-allocation problem, not a ranked list. With daily lineups, a 7-acquisition weekly cap, and a 1-day waiver period:

- **Decision:** a binary variable per free agent per day — is this player rostered and started on day *d*?
- **Objective:** maximize expected category contribution over the matchup week
- **Constraints:** total adds at most 7; daily started players within slot limits; a player can only be started on days their team plays; waiver-pool players require a 1-day lookahead commit

Two availability pools are modeled separately: instant-add free agents, and recently-dropped players locked on waivers for one day.

Output is a schedule — *add A Monday, drop Thursday, add B Thursday* — not a list.

---

## Architecture

```
capspace/
├── data/
│   ├── ingestion.py       # nba_api game logs, schedule, injury status
│   ├── espn.py            # ESPN league state: rosters, settings, free agents, ranks
│   ├── storage.py         # DuckDB cache, 12h TTL, transaction log
│   └── schemas.py         # Pydantic v2: Player, LeagueSettings, Matchup, TradeOffer
├── engine/
│   ├── projections.py     # per-minute rates, minutes estimates, shrinkage
│   ├── metrics.py         # volume-weighted category z-scores
│   ├── schedule.py        # weekly game counts, league-wide off-night density
│   ├── simulate.py        # Monte Carlo matchup, per-category win probability
│   ├── streaming.py       # PuLP MILP: acquisition budget, daily slots, waivers
│   └── trades.py          # candidate search, acceptance scoring, pitch generation
├── write/
│   ├── transport.py       # ESPN write path, dry-run by default
│   └── guards.py          # protected players, value ceiling, rate limits
├── bot/
│   ├── main.py            # discord.py commands + scheduled digest
│   └── render.py          # embeds and matplotlib table images
├── cli/
│   └── main.py            # typer entrypoint for desk-based exploration
└── tests/
```

**Design split:** research is fully automated, actions are human-approved. The nightly pipeline computes everything; the bot surfaces it with buttons. No transaction is submitted without an explicit click.

Read and write credentials are kept on separate code paths so a bug in the research pipeline cannot modify a roster.

---

## Stack

- **Python 3.11+**
- `nba_api` — NBA stats and game logs
- `espn-api` — ESPN league reads (read-only; the write path is custom)
- `pandas`, `numpy` — analytics
- `pulp` (CBC) — mixed-integer programming
- `pydantic` v2 — schema validation
- `duckdb` — local cache and transaction log
- `discord.py` — primary interface
- `typer` + `rich` — CLI
- `APScheduler` — in-process nightly pipeline

---

## Interface

### Discord

```
/today                  Optimal lineup vs. current, with Apply button
/matchup                Category-by-category win probability for the active week
/stream                 This week's acquisition plan against the 7-add budget
/trade @manager         Ranked trade candidates with a drafted pitch
/rank --window 14d      Model value vs. ESPN rank, sorted by spread
```

A scheduled digest posts each morning, and only when something material changed against the previous run — a minutes projection jump, a new trade candidate, a lineup conflict.

### CLI

```bash
capspace rank --view zscore --window 14d --sort-by spread
capspace stream --days 7 --pos "PG,SG"
capspace trade --send "Player A" --receive "Player B, Player C"
capspace simulate --week current --n 10000
```

---

## Setup

```bash
git clone https://github.com/ritam-m-code/CapSpace.git
cd CapSpace
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

ESPN private leagues require two browser cookies. On `fantasy.espn.com`, open DevTools, then Application, then Cookies, and copy them into `.env`:

```
ESPN_S2=...
ESPN_SWID={...}
ESPN_LEAGUE_ID=...
ESPN_TEAM_ID=...
DISCORD_TOKEN=...
DISCORD_CHANNEL_ID=...
```

League settings — roster slots, lineup lock type, acquisition limits, scoring categories — are read from the API rather than configured by hand.

---

## Status

Data and valuation core shipped; `capspace rank` runs against live NBA and
ESPN data. 181 tests, `ruff` and `pyright` clean.

- [x] ESPN read integration, league settings, DuckDB schema
- [x] Projections: per-minute rates and minutes estimates
- [x] Volume-weighted category z-scores
- [x] Schedule grid and off-night density
- [x] CLI exploration commands — `rank` only; `stream`, `trade`, `simulate` pending
- [ ] Monte Carlo matchup simulator
- [ ] Streaming MILP
- [ ] Trade search and acceptance scoring
- [ ] Discord bot and scheduled digest
- [ ] Lineup write path with guardrails

Both ESPN seams are now verified against a live private league. Recorded
fixtures come from a real league with ids anonymized, and `capspace rank`
has been run end to end against live NBA and ESPN data.

One caveat on current output: the season is nine days old, so ESPN's
season-to-date ranks punish missed games while the model's per-game rates do
not. Every player who has missed games currently reads as under-rated. That
is the intended mechanism, but at this point in the season it is a
systematic bias rather than selective arbitrage.

See `HANDOFF.md`.

---

## Operating cost

Deployed on AWS. The Discord bot holds a persistent gateway connection, so the compute has to be always-on rather than serverless; a `t4g.nano` runs both the bot and the APScheduler nightly pipeline.

| Item | Monthly |
|---|---|
| EC2 `t4g.nano`, always on (on-demand, ARM) | $3.07 |
| EBS `gp3` 8GB root volume | $0.64 |
| Hashtag Basketball premium (in-season, validation only) | $2.50 |
| `nba_api`, ESPN API, Discord, GitHub | $0.00 |
| **Total** | **~$6.21** |

Roughly $43/year running in-season only. Existing AWS credits cover the compute, which leaves the $2.50 subscription as the only real cost — and that is validation-only, so it is optional. A reserved instance or a Graviton savings plan would cut the compute by about a third if the credits run out.

---

## Notes

ESPN's fantasy API is undocumented. Read endpoints are well-mapped by the community; write endpoints (`lm-api-writes.fantasy.espn.com/transactions/`) are reverse-engineered and unverified for basketball specifically. All write operations default to dry-run, log their exact payload, and refuse to drop protected players.

Automated access is contrary to ESPN's terms of service. CapSpace is a personal tool operating on a single league the author manages.

## License

MIT
