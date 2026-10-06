# Trading Game 🎰

Multi-agent stock trading simulation. One analyst agent produces DD reports from real market data. Six trader agents, each with a distinct investment philosophy, react independently and trade against a simulated credit ledger. P&L tracked against real price movement.

**This is a research experiment. No real money moves.**

---

## The Agents

### Analyst
- Triggers on: earnings calendar, WSB spikes (via ApeWisdom), fast-rising WSB tickers, weekly digest (>5% price movement), manual `/inject`
- Produces: structured BUY/SELL/HOLD report with bull case, bear case, risks, price target, confidence rating
- Model: Claude Sonnet 4

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
- **CapitolTrades** — congressional trade disclosures (scraper)
- **Claude API** — Sonnet 4 for all agents

---

## Setup

### 1. Railway project

```bash
# Install Railway CLI if needed
npm install -g @railway/cli
railway login
railway link  # link to existing Trading-game project
```

### 2. Add Postgres

In Railway dashboard: New > Database > PostgreSQL. `DATABASE_URL` is auto-injected.

### 3. Environment variables

Set these in Railway dashboard (Variables tab):

```
ANTHROPIC_API_KEY=sk-ant-...
ADMIN_TOKEN=...              # make something up, used for /inject and /portfolio
```

No Reddit credentials needed — WSB data comes from ApeWisdom (free, no auth).

### 4. Deploy

```bash
railway up
```

Railway detects Python, installs `requirements.txt`, starts with:
```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

On first boot, `create_tables()` and `initialise_ledgers()` run automatically.

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
