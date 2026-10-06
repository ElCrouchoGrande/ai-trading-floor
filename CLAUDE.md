# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-agent stock-trading **simulation** (no real money). One analyst agent turns real market data into structured DD reports; six trader agents, each with a fixed investing persona, react independently and trade against a simulated credit ledger. P&L is marked against real yfinance prices. Runs as a single always-on FastAPI process on Railway.

## Commands

Everything needs `DATABASE_URL` in the environment — the app and Alembic both **fail fast** if it's unset (no SQLite fallback wired up). Copy `.env.example` to `.env` for local dev; also set `ANTHROPIC_API_KEY`.

```bash
# Run the app locally (serves dashboard on / and admin API)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Apply DB schema (required before first run; runs automatically on Railway)
alembic upgrade head

# Create a new migration after editing db/models.py
alembic revision --autogenerate -m "describe change"

# Trigger the full pipeline by hand (analyst -> 6 traders -> debates)
curl -X POST localhost:8000/inject \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"ticker": "NVDA", "trigger_type": "manual"}'
```

There is **no test suite, linter, or build step** configured. `requirements.txt` is the only dependency manifest (plain pip, no lockfile).

## Deploy

Railway, configured via `railway.toml`. Build is Nixpacks (auto-detected Python). Start command runs migrations then the server:

```
alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port $PORT
```

Postgres is a Railway plugin that injects `DATABASE_URL`. Healthcheck is `GET /health`. The public dashboard is the service's root URL (`/`); admin endpoints require the `X-Admin-Token` header.

## Architecture

**One process, two halves**, both wired up in `main.py`:

1. **FastAPI app** — public read-only dashboard (`/`, `/api/state`, `/api/reports`, `/api/trades`, `/health`) plus token-gated admin endpoints (`/inject`, `/portfolio`, `/report/latest/{ticker}`, `/weekly/preview`).
2. **APScheduler** (started in the `lifespan` context) — three cron jobs: trigger check every 3 hours during the trading window (`hour="7-22/3"` UTC, weekdays — earnings + WSB), Monday weekly digest, daily P&L mark-to-market (21:00 UTC).

**The core pipeline** (`main.py:_run_all_traders`) is the thing to understand:

```
trigger/inject -> AnalystAgent.run_for_ticker() writes a Report row
              -> every trader in TRADERS reacts via react_to_report()
              -> DebateEngine.check_for_conflicts() finds LONG-vs-SHORT pairs
                 on the same report and runs a 2-round debate
```

- **Analyst** (`analyst/agent.py`) gathers data via `analyst/data_fetcher.py` (yfinance prices/fundamentals/earnings, SEC EDGAR Form 4, ApeWisdom WSB via `wsb/scraper.py`), prompts Claude, and persists a `Report`. `TriggerDetector` (same file) decides what to analyse. `CORE_TICKERS` in `data_fetcher.py` is the permanent universe; `WatchedTicker` rows are dynamic adds.
- **Traders** (`traders/`): `BaseTrader` owns all Claude-call + position/ledger mechanics. `all_traders.py` subclasses only override `system_prompt` and sizing. Each trader returns **JSON only** (`action`/`credits_risked`/`reasoning`); `BaseTrader._execute_decision` parses and clamps size to `max_position_pct * pot`.
- **Engines** (`engine/`): `pnl.py` marks positions to market, applies per-trader `EXIT_RULES`, and is also the source of `get_portfolio_summary()` (used by both dashboard and weekly report). `debate.py` runs the conflict debates.
- **Reporting** (`reporting/`): `weekly_report.py` builds the weekly HTML digest, viewable via `/weekly/preview` (it is not emailed or scheduled). `dashboard.py` holds the entire single-page UI as a string template plus the trader bios.
- **DB** (`db/`): `models.py` is SQLAlchemy 2.0 models (Report, Ledger, Trade, Position, Debate, LedgerSnapshot, WatchedTicker). `session.py` has the session contextmanager and all ledger helpers.

## Conventions and gotchas

- **Schema is Alembic-managed, not `create_tables()`.** Despite the README, the running app does *not* call `create_tables()` — `main.py`'s lifespan only seeds ledger rows (`initialise_ledgers`). Always add a migration in `alembic/versions/` when you change `db/models.py`. `alembic/env.py` pulls `DATABASE_URL` from the environment (the `sqlalchemy.url` in `alembic.ini` is intentionally blank).
- **`get_session` commits on `__exit__` with default `expire_on_commit`.** Reading ORM attributes after the `with` block raises `DetachedInstanceError`. Capture primitives (e.g. `report.id`) *inside* the block before returning — see the comment in `main.py` `/inject`.
- **The ape's reset is a deliberate secret.** `PnLEngine._check_regard_reset` (in the daily P&L job) silently restores the WSB Ape (display name; `trader_id` is still `regard`) to a clean 1000-credit *net worth* when it busts: it triggers on `cash + open positions at market < 100` (NOT cash alone — a cash-only check hands free credits to a merely-deployed ape, inflating net worth to ~2000), records the wipeout on each open trade (`reset_occurred=True`), clears the positions, refills cash to 1000, and bumps `lifetime_resets`. Its system prompt is explicitly told never to mention balances or resets — keep it that way; the dashboard surfaces resets as a 💀 counter but the agent must not know.
- **All three Claude callers hardcode `claude-sonnet-5`** (analyst, base trader, debate engine). Change all of them together if upgrading models — a dated snapshot going stale is what silently killed trading for a day (404s from every call, swallowed into per-report error logs).
- **`Numeric` columns come back as `Decimal`; NaN must die before JSON.** Convert with `float(...)` before arithmetic or JSON. `dashboard.py`'s `_f()` handles both Decimal→float and collapses NaN/Inf to `None`; `_jsonsafe()` recursively scrubs the cached dict for the same reason. **Don't remove these** — yfinance returns NaN for delisted tickers, and Starlette's JSON encoder rejects NaN *outside* any try/except inside our code (it fails during response render), so a single bad price would 500 the whole endpoint otherwise.
- **Per-trader sizing is enforced in code, not just prompts.** `max_position_pct` defaults to 0.30; short seller is 0.40, hugger 0.15, regard randomises 0.20–1.00 *per call* (`WSBRegard.react_to_report`). The Boomer hard-rejects any ticker outside `APPROVED_TICKERS` before calling Claude.
- **The dashboard `/api/state` is cached (`get_cached_state`, 60s TTL)** because building it hits yfinance for every open position. Don't bypass the cache in polling paths. `/api/reports` and `/api/trades` are cached the same way (60s TTL, separate dicts). `build_dashboard_state` builds each section (leaderboard / reports / trades / positions / debates) in its own try/except and surfaces failures in an `_errors` array on the response, so a broken section can't blank the whole page — keep that pattern when adding new sections.
- **Scheduled analyst spend is throttled by code, not just cron.** `AnalystAgent.run_for_ticker` skips a ticker if a `Report` newer than `ANALYST_COOLDOWN_HOURS` (default 12, env-overridable) already exists; `run_analyst_triggers` caps each run at `MAX_TICKERS_PER_RUN` (default 5, earnings before WSB). Manual `/inject` passes `force=True` to bypass the cooldown — don't propagate `force` to other callers.
- **`ADMIN_TOKEN="changeme"` (or unset) disables admin endpoints, doesn't enable them.** `verify_token` returns 503 in that mode so a publicly-reachable deploy can't be abused; a loud `WARNING` logs on lifespan startup. The public dashboard and scheduler stay up regardless. Don't accept the default as a valid token under any code path.
- **Old debate transcripts have raw `trader_id`s baked in.** Pre-`172f9ae` `Debate` rows store `"ROUND 1 - regard (LONG):"` etc. `engine.debate.humanize_transcript()` rewrites them on the way out (dashboard `build_dashboard_state` + weekly report) — safe for structured label slots across all traders, plus a `\bregard\b → "The WSB Ape"` swap in prose. `short`/`momentum` are deliberately *not* swapped in prose since they collide with ordinary finance words.
