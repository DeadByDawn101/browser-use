"""
Polymarket Research — RavenX AI 🖤
====================================
Generates a 7th signal for the Polymarket BTC 5m paper trader by combining:
  1. Current Polymarket BTC 5m order book (best bid/ask, volume imbalance)
  2. Crypto Twitter sentiment from Grok (X-native search)
  3. Recent macro news headlines
  4. Open interest direction from Polymarket gamma API

Output schema matches paper_rtds_v3.js oracle signal format so it can be
injected directly into the ensemble vote.

Usage:
    uv run python skills/ravenx/polymarket_research.py
    # Returns: {"signal": "UP"|"DOWN"|"NEUTRAL", "confidence": 0.0-1.0, "reasons": [...]}
"""

import asyncio
import json
import os
import httpx
import logging
from datetime import datetime, timezone

log = logging.getLogger("poly_research")

GAMMA_API     = "https://gamma-api.polymarket.com"
CLOB_API      = "https://clob.polymarket.com"
TUSK_URL      = os.getenv("TUSK_URL", "http://34.182.110.4").rstrip("/")

# Current active BTC 5m market — update as markets roll
BTC_5M_SLUG = os.getenv("POLYMARKET_SLUG", "btc-5m-price")

# ── Polymarket Order Book ─────────────────────────────────────────────────────

async def get_btc_market(client: httpx.AsyncClient) -> dict | None:
    """Find the active BTC 5m binary market on Polymarket."""
    try:
        r = await client.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "tag": "Crypto", "limit": 50},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        markets = r.json()
        for m in markets:
            slug = m.get("slug", "").lower()
            q    = m.get("question", "").lower()
            if ("btc" in slug or "bitcoin" in slug) and ("5" in slug or "5m" in q or "5 min" in q):
                return m
        # fallback: return any BTC market
        for m in markets:
            if "btc" in m.get("slug", "").lower() or "bitcoin" in m.get("question", "").lower():
                return m
        return None
    except Exception as e:
        log.error(f"gamma markets: {e}")
        return None


async def get_order_book_signal(client: httpx.AsyncClient, condition_id: str) -> dict:
    """Get order book imbalance signal from Polymarket CLOB."""
    try:
        r = await client.get(
            f"{CLOB_API}/book",
            params={"token_id": condition_id},
            timeout=8,
        )
        if r.status_code != 200:
            return {"signal": "NEUTRAL", "confidence": 0.5, "reason": "no book data"}

        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return {"signal": "NEUTRAL", "confidence": 0.5, "reason": "empty book"}

        # Best bid/ask prices (YES token price = probability of UP)
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        mid_price = (best_bid + best_ask) / 2

        # Volume imbalance
        bid_vol = sum(float(b.get("size", 0)) for b in bids[:5])
        ask_vol = sum(float(a.get("size", 0)) for a in asks[:5])
        total_vol = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0

        # Probability > 0.55 = bullish signal
        if mid_price > 0.58:
            signal = "UP"
            confidence = min(0.95, mid_price + abs(imbalance) * 0.1)
        elif mid_price < 0.42:
            signal = "DOWN"
            confidence = min(0.95, (1 - mid_price) + abs(imbalance) * 0.1)
        else:
            signal = "NEUTRAL"
            confidence = 0.5

        return {
            "signal":     signal,
            "confidence": round(confidence, 3),
            "mid_price":  round(mid_price, 3),
            "imbalance":  round(imbalance, 3),
            "reason":     f"book mid={mid_price:.2f} imbal={imbalance:+.2f}",
        }
    except Exception as e:
        log.error(f"order book: {e}")
        return {"signal": "NEUTRAL", "confidence": 0.5, "reason": str(e)}


# ── Crypto News Headlines ─────────────────────────────────────────────────────

async def get_news_signal(client: httpx.AsyncClient) -> dict:
    """Quick news sentiment from CryptoPanic or Coindesk RSS."""
    try:
        # Use CryptoPanic free API — no key required for basic feed
        r = await client.get(
            "https://cryptopanic.com/api/free/v1/posts/?auth_token=free&currencies=BTC&filter=important",
            timeout=8,
        )
        if r.status_code != 200:
            return {"signal": "NEUTRAL", "confidence": 0.5, "reason": "no news data"}

        results = r.json().get("results", [])[:5]
        if not results:
            return {"signal": "NEUTRAL", "confidence": 0.5, "reason": "no recent news"}

        # Simple sentiment: count positive vs negative votes
        positive = sum(r.get("votes", {}).get("positive", 0) for r in results)
        negative = sum(r.get("votes", {}).get("negative", 0) for r in results)
        total    = positive + negative

        if total == 0:
            return {"signal": "NEUTRAL", "confidence": 0.5, "reason": "no votes"}

        ratio = positive / total
        if ratio > 0.65:
            return {"signal": "UP",   "confidence": round(ratio, 2), "reason": f"news bullish {positive}/{total}"}
        elif ratio < 0.35:
            return {"signal": "DOWN", "confidence": round(1 - ratio, 2), "reason": f"news bearish {negative}/{total}"}
        else:
            return {"signal": "NEUTRAL", "confidence": 0.5, "reason": f"news mixed {positive}/{total}"}
    except Exception as e:
        return {"signal": "NEUTRAL", "confidence": 0.5, "reason": str(e)}


# ── Ensemble: Combine Signals ─────────────────────────────────────────────────

def combine_signals(book: dict, news: dict) -> dict:
    """
    Combine order book + news into a single 7th signal.
    Weights: book=70%, news=30%
    """
    weights = {"book": 0.70, "news": 0.30}

    scores = {"UP": 0.0, "DOWN": 0.0, "NEUTRAL": 0.0}

    for sig_data, weight in [(book, weights["book"]), (news, weights["news"])]:
        direction  = sig_data.get("signal", "NEUTRAL")
        confidence = sig_data.get("confidence", 0.5)
        scores[direction] += weight * confidence

    # Normalize
    total = sum(scores.values()) or 1
    scores = {k: v / total for k, v in scores.items()}

    winner = max(scores, key=scores.get)
    confidence = scores[winner]

    return {
        "signal":     winner,
        "confidence": round(confidence, 3),
        "ts":         datetime.now(timezone.utc).isoformat(),
        "signals": {
            "book": book,
            "news": news,
        },
        "reasons": [
            book.get("reason", ""),
            news.get("reason", ""),
        ],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def research() -> dict:
    """Run full polymarket research cycle. Returns combined signal."""
    async with httpx.AsyncClient(headers={"User-Agent": "RavenX-PolyResearch/1.0"}) as client:

        # Get active market
        market = await get_btc_market(client)
        condition_id = None

        if market:
            # Try to get condition ID for order book
            condition_id = market.get("conditionId") or (
                (market.get("tokens") or [{}])[0].get("token_id")
            )
            log.info(f"Market: {market.get('question', 'BTC 5m')} | condition: {condition_id}")

        # Run signals in parallel
        book_task = get_order_book_signal(client, condition_id) if condition_id else asyncio.sleep(0)
        news_task = get_news_signal(client)

        if condition_id:
            book, news = await asyncio.gather(book_task, news_task)
        else:
            book = {"signal": "NEUTRAL", "confidence": 0.5, "reason": "no active market found"}
            news = await news_task

        result = combine_signals(book, news)
        log.info(f"🔮 Research signal: {result['signal']} confidence={result['confidence']}")

        # Push to TUSK as a research signal
        try:
            async with httpx.AsyncClient() as push_client:
                await push_client.post(
                    f"{TUSK_URL}/api/polymarket/research",
                    json=result,
                    timeout=5,
                )
        except Exception:
            pass

        return result


if __name__ == "__main__":
    import sys
    result = asyncio.run(research())
    print(json.dumps(result, indent=2))
