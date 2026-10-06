"""
Data fetcher — wraps yfinance, SEC EDGAR, and news sources.
All functions return clean dicts; callers never touch raw API responses.
"""
import os
import re
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
import yfinance as yf
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# SEC EDGAR requires a User-Agent that identifies a contact address.
SEC_USER_AGENT = f"trading-research-bot {os.environ.get('SEC_CONTACT_EMAIL', 'contact@example.com')}"

# ---------------------------------------------------------------------------
# Core watchlist
# ---------------------------------------------------------------------------

CORE_TICKERS = {
    # Mag 7
    "AAPL":   {"name": "Apple",           "exchange": "US"},
    "MSFT":   {"name": "Microsoft",       "exchange": "US"},
    "GOOGL":  {"name": "Alphabet",        "exchange": "US"},
    "AMZN":   {"name": "Amazon",          "exchange": "US"},
    "META":   {"name": "Meta",            "exchange": "US"},
    "NVDA":   {"name": "NVIDIA",          "exchange": "US"},
    "TSLA":   {"name": "Tesla",           "exchange": "US"},
    # Cybersecurity
    "CRWD":   {"name": "CrowdStrike",     "exchange": "US"},
    "PANW":   {"name": "Palo Alto",       "exchange": "US"},
    "S":      {"name": "SentinelOne",     "exchange": "US"},
    "FTNT":   {"name": "Fortinet",        "exchange": "US"},
    "ZS":     {"name": "Zscaler",         "exchange": "US"},
    # Global handpicked
    "ASML":   {"name": "ASML",            "exchange": "US"},   # US-listed
    "TSM":    {"name": "TSMC",            "exchange": "US"},   # ADR
    "ARM":    {"name": "Arm Holdings",    "exchange": "US"},
    "SAP":    {"name": "SAP",             "exchange": "US"},   # ADR
}

# Regard trades US-listed only (WSB doesn't cover EU/AS exchanges)
REGARD_UNIVERSE = {k: v for k, v in CORE_TICKERS.items() if v["exchange"] == "US"}


# ---------------------------------------------------------------------------
# Price & fundamentals (yfinance)
# ---------------------------------------------------------------------------

def get_price_data(ticker: str, period: str = "3mo") -> dict:
    """
    Returns current price, moving averages, RSI, and basic OHLCV history.
    period: yfinance period string e.g. '1mo', '3mo', '6mo'
    """
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)

        if hist.empty:
            return {"error": f"No price data for {ticker}"}

        closes = hist["Close"]
        current_price = float(closes.iloc[-1])

        ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else None
        ma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else None

        # RSI (14-period)
        rsi = _calculate_rsi(closes, 14)

        # Week / month performance
        week_ago = float(closes.iloc[-6]) if len(closes) >= 6 else None
        month_ago = float(closes.iloc[-22]) if len(closes) >= 22 else None
        week_pct = ((current_price - week_ago) / week_ago * 100) if week_ago else None
        month_pct = ((current_price - month_ago) / month_ago * 100) if month_ago else None

        return {
            "ticker": ticker,
            "current_price": current_price,
            "ma20": ma20,
            "ma50": ma50,
            "rsi": rsi,
            "week_change_pct": round(week_pct, 2) if week_pct else None,
            "month_change_pct": round(month_pct, 2) if month_pct else None,
            "volume_today": int(hist["Volume"].iloc[-1]),
            "avg_volume_20d": int(hist["Volume"].tail(20).mean()),
        }
    except Exception as e:
        logger.error(f"Price data error for {ticker}: {e}")
        return {"error": str(e)}


def get_fundamentals(ticker: str) -> dict:
    """
    Returns key fundamental metrics for the analyst and traders.
    """
    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        return {
            "ticker": ticker,
            "company_name": info.get("longName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "ps_ratio": info.get("priceToSalesTrailing12Months"),
            "pb_ratio": info.get("priceToBook"),
            "ev_ebitda": info.get("enterpriseToEbitda"),
            "revenue_growth_yoy": info.get("revenueGrowth"),
            "earnings_growth_yoy": info.get("earningsGrowth"),
            "gross_margins": info.get("grossMargins"),
            "operating_margins": info.get("operatingMargins"),
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "dividend_yield": info.get("dividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "float_shares": info.get("floatShares"),
            "short_ratio": info.get("shortRatio"),
            "short_percent_float": info.get("shortPercentOfFloat"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "analyst_target_price": info.get("targetMeanPrice"),
            "analyst_recommendation": info.get("recommendationKey"),
            "ipo_year": _extract_ipo_year(info),
        }
    except Exception as e:
        logger.error(f"Fundamentals error for {ticker}: {e}")
        return {"error": str(e)}


def get_earnings_calendar(tickers: list[str], days_ahead: int = 7) -> list[dict]:
    """
    Returns tickers with earnings dates within the next N days.
    """
    upcoming = []
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)

    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            cal = stock.calendar
            if cal is None or cal.empty:
                continue

            # calendar is a DataFrame with columns like 'Earnings Date'
            if "Earnings Date" in cal.columns:
                dates = cal["Earnings Date"].dropna()
                for dt in dates:
                    if isinstance(dt, datetime):
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if now <= dt <= cutoff:
                            upcoming.append({
                                "ticker": ticker,
                                "earnings_date": dt.isoformat(),
                                "days_until": (dt - now).days,
                            })
        except Exception as e:
            logger.warning(f"Earnings calendar error for {ticker}: {e}")

    return sorted(upcoming, key=lambda x: x["days_until"])


def get_spy_price() -> Optional[float]:
    """Current SPY closing price for benchmark tracking."""
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"SPY price error: {e}")
    return None


# ---------------------------------------------------------------------------
# SEC EDGAR — Form 4 insider filings
# ---------------------------------------------------------------------------

EDGAR_BASE = "https://efts.sec.gov/LATEST/search-index"
EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index?q=%22form+4%22&dateRange=custom"

def get_insider_filings(ticker: str, days_back: int = 30) -> list[dict]:
    """
    Fetches recent Form 4 filings for a ticker from SEC EDGAR.
    Returns only purchases (not sales or option exercises).
    """
    try:
        # EDGAR full-text search API
        since = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = (
            f"https://efts.sec.gov/LATEST/search-index?"
            f"q=%22{ticker}%22&dateRange=custom&startdt={since}"
            f"&forms=4"
        )
        headers = {"User-Agent": SEC_USER_AGENT}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        filings = []
        for hit in data.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            # Filter to purchases only
            transaction_type = src.get("period_of_report", "")
            display_names = src.get("display_names", [])
            filing_date = src.get("file_date", "")
            entity_name = src.get("entity_name", "")

            filings.append({
                "ticker": ticker,
                "entity_name": entity_name,
                "filing_date": filing_date,
                "display_names": display_names,
                "accession_no": src.get("accession_no", ""),
                "form_type": src.get("form_type", ""),
            })

        # Parse the actual XML for transaction details
        purchases = []
        for filing in filings[:10]:  # cap at 10 per run
            details = _parse_form4_xml(filing["accession_no"], ticker)
            if details and details.get("transaction_type") == "P":  # P = purchase
                purchases.append({**filing, **details})

        return purchases

    except Exception as e:
        logger.error(f"EDGAR error for {ticker}: {e}")
        return []


def _parse_form4_xml(accession_no: str, ticker: str) -> Optional[dict]:
    """Parse Form 4 XML to extract transaction type and value."""
    try:
        # Convert accession number to filing path
        acc_clean = accession_no.replace("-", "")
        cik = acc_clean[:10].lstrip("0")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/"
        headers = {"User-Agent": SEC_USER_AGENT}

        # Get filing index
        idx_resp = requests.get(url, headers=headers, timeout=10)
        if idx_resp.status_code != 200:
            return None

        soup = BeautifulSoup(idx_resp.text, "html.parser")
        xml_link = None
        for link in soup.find_all("a", href=True):
            if link["href"].endswith(".xml") and "form4" in link["href"].lower():
                xml_link = "https://www.sec.gov" + link["href"]
                break

        if not xml_link:
            return None

        xml_resp = requests.get(xml_link, headers=headers, timeout=10)
        xml_soup = BeautifulSoup(xml_resp.text, "xml")

        transaction_type = None
        shares = None
        price = None

        tx = xml_soup.find("nonDerivativeTransaction")
        if tx:
            transaction_type = getattr(tx.find("transactionCode"), "text", None)
            shares_el = tx.find("transactionShares")
            price_el = tx.find("transactionPricePerShare")
            shares = float(getattr(shares_el.find("value"), "text", 0) or 0)
            price = float(getattr(price_el.find("value"), "text", 0) or 0)

        return {
            "transaction_type": transaction_type,
            "shares": shares,
            "price_per_share": price,
            "total_value": (shares or 0) * (price or 0),
        }
    except Exception as e:
        logger.debug(f"Form 4 XML parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# Capitol Trades — congressional disclosures
# ---------------------------------------------------------------------------

def get_congress_trades(days_back: int = 30) -> list[dict]:
    """
    Scrapes recent congressional trade disclosures from CapitolTrades.com.
    Returns structured list of trades for the Insider Tracker's reference.
    """
    try:
        url = "https://www.capitoltrades.com/trades?pageSize=50"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        trades = []
        cutoff = datetime.now() - timedelta(days=days_back)

        # CapitolTrades table rows
        rows = soup.select("table tbody tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 6:
                continue
            try:
                politician = cells[0].get_text(strip=True)
                ticker = cells[2].get_text(strip=True)
                action = cells[3].get_text(strip=True)  # Purchase / Sale
                amount = cells[4].get_text(strip=True)
                date_str = cells[5].get_text(strip=True)

                trade_date = datetime.strptime(date_str, "%m/%d/%Y")
                if trade_date < cutoff:
                    continue

                trades.append({
                    "politician": politician,
                    "ticker": ticker,
                    "action": action,
                    "amount_range": amount,
                    "trade_date": trade_date.isoformat(),
                })
            except Exception:
                continue

        return trades
    except Exception as e:
        logger.error(f"CapitolTrades scrape error: {e}")
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _calculate_rsi(closes, period: int = 14) -> Optional[float]:
    """Standard RSI calculation."""
    if len(closes) < period + 1:
        return None
    deltas = closes.diff().dropna()
    gains = deltas.clip(lower=0)
    losses = -deltas.clip(upper=0)
    avg_gain = gains.tail(period).mean()
    avg_loss = losses.tail(period).mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _extract_ipo_year(info: dict) -> Optional[int]:
    """Try to extract IPO/founding year from yfinance info."""
    # yfinance doesn't give IPO year directly; use firstTradeDateEpochUtc
    ts = info.get("firstTradeDateEpochUtc")
    if ts:
        return datetime.fromtimestamp(ts, tz=timezone.utc).year
    return None
