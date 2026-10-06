"""
WSB scraper — uses ApeWisdom free public API.
No API key, no Reddit account, no PRAW needed.
https://apewisdom.io/api/

ApeWisdom aggregates r/wallstreetbets mention + upvote data,
updated hourly. Returns ranked tickers with 24h rank history,
which lets us detect fast-rising names as well as raw volume spikes.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/wallstreetbets"

# Trigger thresholds
WSB_MENTION_SPIKE = 50        # raw mentions in last 24h
WSB_RANK_RISE_THRESHOLD = 100 # rank improved by this much in 24h = rising fast


def get_wsb_sentiment() -> dict:
    """
    Fetches WSB ticker sentiment from ApeWisdom.

    Returns dict keyed by ticker:
        {
            "NVDA": {
                "mentions": 342,
                "upvotes": 15234,
                "rank": 1,
                "rank_24h_ago": 3,
                "rank_change": 2,        # positive = rising
                "sentiment": "bullish",
                "rising": False,
            },
            ...
            "spike_tickers": ["NVDA"],   # mention volume spike
            "rising_tickers": ["GME"],   # fast rank climbers
            "fetched_at": "...",
        }
    """
    try:
        resp = requests.get(APEWISDOM_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        result = {}
        spike_tickers = []
        rising_tickers = []

        for item in data.get("results", []):
            ticker = item.get("ticker", "").upper()
            if not ticker:
                continue

            mentions = int(item.get("mentions", 0))
            upvotes = int(item.get("upvotes", 0))
            rank = item.get("rank")
            rank_24h_ago = item.get("rank_24h_ago")

            # Rank change: positive means improving (falling rank number = rising)
            rank_change = 0
            rising = False
            if rank and rank_24h_ago:
                try:
                    rank_change = int(rank_24h_ago) - int(rank)
                    rising = rank_change >= WSB_RANK_RISE_THRESHOLD
                except (ValueError, TypeError):
                    pass

            sentiment = "bullish" if upvotes > 100 else "mixed"

            result[ticker] = {
                "mentions": mentions,
                "upvotes": upvotes,
                "rank": rank,
                "rank_24h_ago": rank_24h_ago,
                "rank_change": rank_change,
                "sentiment": sentiment,
                "rising": rising,
            }

            if mentions >= WSB_MENTION_SPIKE:
                spike_tickers.append(ticker)

            if rising:
                rising_tickers.append(ticker)

        result["spike_tickers"] = spike_tickers
        result["rising_tickers"] = rising_tickers
        result["fetched_at"] = datetime.now(timezone.utc).isoformat()
        result["total_tickers_tracked"] = len([k for k in result if k not in (
            "spike_tickers", "rising_tickers", "fetched_at", "total_tickers_tracked"
        )])

        return result

    except requests.RequestException as e:
        logger.error(f"ApeWisdom request error: {e}")
        return {"error": str(e), "spike_tickers": [], "rising_tickers": []}
    except Exception as e:
        logger.error(f"WSB sentiment error: {e}")
        return {"error": str(e), "spike_tickers": [], "rising_tickers": []}


def get_wsb_ticker_detail(ticker: str) -> dict:
    """
    Get this ticker's current WSB standing from the full ApeWisdom feed.
    Returns the ticker's row or an empty dict if not in today's feed.
    Also fetches page 2 to catch tickers outside the top 100.
    """
    try:
        for page in range(1, 4):
            url = f"https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/{page}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("results", []):
                if item.get("ticker", "").upper() == ticker.upper():
                    mentions = int(item.get("mentions", 0))
                    upvotes = int(item.get("upvotes", 0))
                    rank = item.get("rank")
                    rank_24h_ago = item.get("rank_24h_ago")

                    rank_change = 0
                    if rank and rank_24h_ago:
                        try:
                            rank_change = int(rank_24h_ago) - int(rank)
                        except (ValueError, TypeError):
                            pass

                    return {
                        "ticker": ticker,
                        "mentions": mentions,
                        "upvotes": upvotes,
                        "rank": rank,
                        "rank_24h_ago": rank_24h_ago,
                        "rank_change": rank_change,
                        "sentiment": "bullish" if upvotes > 100 else "mixed",
                        "rising": rank_change >= WSB_RANK_RISE_THRESHOLD,
                        "wsb_active": mentions > 0,
                    }

            # If this page had fewer than 100 results, no point checking further
            if len(data.get("results", [])) < 100:
                break

        return {
            "ticker": ticker,
            "mentions": 0,
            "upvotes": 0,
            "rank": None,
            "rank_change": 0,
            "sentiment": "none",
            "rising": False,
            "wsb_active": False,
        }

    except Exception as e:
        logger.error(f"WSB ticker detail error for {ticker}: {e}")
        return {"ticker": ticker, "error": str(e), "wsb_active": False}
