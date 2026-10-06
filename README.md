**NOTE: THIS A PUBLIC COMMIT OF A PRE-EXISTING PRIVATE REPO.**

**Watch the trading game play at https://trading-game-production-3366.up.railway.app/ 

# AI Trading Floor 🎰

Multi-agent stock trading simulation. One analyst agent produces DD reports from real market data. Six trader agents, each with a distinct investment philosophy, react independently and trade against a simulated credit ledger. P&L tracked against real price movement.

The traders have a message board to discuss their trades and give the viewer an opportunity to see how the independent agents interact

**This is a research experiment. No real money moves.** Everything here is a simulation run by fictional AI agents. Nothing in this repository, its dashboard or its reports is financial advice, and the agents' output should not be used to make real investment decisions.

---

## The Agents

### Analyst
- Triggers on: earnings calendar, WSB spikes (via ApeWisdom), fast-rising WSB tickers, weekly digest (>5% price movement), manual `/inject`
- Produces: structured BUY/SELL/HOLD report with bull case, bear case, risks, price target, confidence rating
- Model: Claude Sonnet (see the `model=` strings in `analyst/`, `traders/` and `engine/`)

### Traders (1000 credits each)

| Trader | Ethos | Special behaviour |
|--------|-------|------------------|
| Momentum | Trend following, 20/50 DMA + RSI | Exits on MA cross |
| Insider Tracker | Follows Form 4 purchases only | Low frequency, 60-90 day holds |
| Short Seller | Finds overvalued/fraudulent stocks | Can go negative |
| WSB Ape 🚀 | Pure vibes, diamond hands | Silent reset at <100 credits |
| The Boomer | Blue chips, dividends, approved universe only | Refuses anything post-2000 |
| Index Hugger | Only deviates on HIGH confidence reports | Control group, 15% max position |

---

## Architecture

- **Railway** — FastAPI + APScheduler, always-on
- **Postgres** — reports, trades, positions, debates, ledger snapshots
- **yfinance** — EOD prices, fundamentals, earnings calendar
- **SEC EDGAR** — Form 4 insider filings (free, no key)
- **ApeWisdom** — WSB sentiment API (free, no key)
- **CapitolTrades** — congressional trade disclosures (scraper, best effort)
- **Claude API** — Sonnet for all agents

---

## Setup

### 1. Railway project

```bash
# Install Railway CLI if needed
npm install -g @railway/cli
railway login
railway link  # link this repo to a new or existing Railway project
```

### 2. Add Postgres

In Railway dashboard: New > Database > PostgreSQL. `DATABASE_URL` is auto-injected.

### 3. Environment variables

Set these in Railway dashboard (Variables tab):

```
ANTHROPIC_API_KEY=sk-ant-...
ADMIN_TOKEN=...              # long random value; admin endpoints stay disabled if unset or "changeme"
SEC_CONTACT_EMAIL=...        # contact address for the SEC EDGAR User-Agent (their fair-access policy asks for one)
```

See `.env.example` for the optional settings (analyst cost throttles).

No Reddit credentials needed — WSB data comes from ApeWisdom (free, no auth).

### 4. Deploy

```bash
railway up
```

Railway detects Python, installs `requirements.txt`, and starts with (see `railway.toml`):
```
alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port $PORT
```

Migrations create the schema, and the six ledgers are seeded on first boot.

### 5. Verify

```bash
# Health check
curl https://your-app.railway.app/health

# Portfolio (should show all 6 traders at 1000 credits)
curl -H "X-Admin-Token: your-token" https://your-app.railway.app/portfolio
```

### 6. First analyst run

```bash
# Manually trigger an analyst report for any ticker
curl -X POST https://your-app.railway.app/inject \
  -H "X-Admin-Token: your-token" \
  -H "Content-Type: application/json" \
  -d '{"ticker": "NVDA", "trigger_type": "manual"}'
```

This runs the full pipeline: analyst report → all 6 traders react → debates if conflicts → ledgers updated.

### Running locally

```bash
cp .env.example .env     # then fill in DATABASE_URL (Postgres) and ANTHROPIC_API_KEY
pip install -r requirements.txt
alembic upgrade head
uvicorn main:app --reload
```

The dashboard is served at `/`. There is no test suite.

## Data sources and costs

Market data comes from yfinance (an unofficial Yahoo Finance wrapper), SEC EDGAR, ApeWisdom and CapitolTrades. Check each provider's terms before running this at scale or redistributing their data. Every analyst report and trader decision is a Claude API call, so running the scheduler costs money; the cooldown and per-run ticker cap in `.env.example` limit that.

---

## Scheduled Jobs

| Job | Schedule | What it does |
|-----|----------|-------------|
| Trigger check | Hourly, Mon-Fri 07:00-22:00 UTC | Earnings + WSB spike detection |
| Weekly digest | Mon 08:00 UTC | Analyst covers all tickers with >5% weekly movement |
| Daily P&L | Mon-Fri 21:00 UTC | Mark-to-market, exit conditions, regard reset check |

---

## Admin Endpoints

All require `X-Admin-Token` header.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness check |
| `/portfolio` | GET | All trader pots + unrealised P&L |
| `/inject` | POST | Manually trigger analyst for a ticker |
| `/report/latest/{ticker}` | GET | Most recent analyst report for ticker |
| `/weekly/preview` | GET | Preview weekly report HTML |

---

## Ticker Universe

**Core (permanent):** AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA, CRWD, PANW, S, FTNT, ZS

**Global:** ASML, TSM, ARM, SAP

**Dynamic:** Tickers added by WSB spikes or news spikes, expire after 30 days.

---

## The Ape's Secret

The WSB Ape resets silently to 1000 credits if their pot drops below 100. They don't know this. The weekly report tracks "lifetime resets" as a running stat. Do not tell the ape. (The internal `trader_id` is still `regard`.)

---

## File Structure

```
main.py                    # FastAPI app + APScheduler
analyst/
  agent.py                 # Analyst agent + trigger detection
  data_fetcher.py          # yfinance, EDGAR, CapitolTrades wrappers
traders/
  base_trader.py           # Abstract trader base class
  all_traders.py           # All 6 trader implementations
engine/
  pnl.py                   # Daily P&L, exit conditions, regard reset
  debate.py                # Conflict detection + two-round debates
reporting/
  weekly_report.py         # Weekly HTML report builder (/weekly/preview)
db/
  models.py                # SQLAlchemy models
  session.py               # Session management + ledger ops
wsb/
  scraper.py               # ApeWisdom API wrapper
```
