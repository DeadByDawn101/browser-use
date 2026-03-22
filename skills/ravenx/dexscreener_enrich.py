"""
DexScreener Enrichment — RavenX AI 🖤
======================================
Enriches a token CA with holder count, LP depth, creator wallet age,
top holders, and recent trade history. Used by TUSK to show context
when a gem appears in the panel.

Usage:
    from skills.ravenx.dexscreener_enrich import enrich_token
    data = await enrich_token("Ca...address...")
"""

import asyncio
import os
import time
import httpx
import logging
from typing import Any

log = logging.getLogger("dex_enrich")

DEXSCREENER_API = "https://api.dexscreener.com"
HELIUS_API      = f"https://mainnet.helius-rpc.com/?api-key={os.getenv('HELIUS_API_KEY', '')}"
SOLANA_RPC      = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# ── Helpers ───────────────────────────────────────────────────────────────────

async def solana_rpc(client: httpx.AsyncClient, method: str, params: list) -> Any:
    r = await client.post(
        HELIUS_API if os.getenv("HELIUS_API_KEY") else SOLANA_RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=8,
    )
    data = r.json()
    if "error" in data:
        raise Exception(f"RPC {method}: {data['error']}")
    return data.get("result")


# ── Enrichment Functions ──────────────────────────────────────────────────────

async def get_pair_data(client: httpx.AsyncClient, ca: str) -> dict | None:
    """Full pair data from DexScreener."""
    try:
        r = await client.get(f"{DEXSCREENER_API}/tokens/v1/solana/{ca}", timeout=8)
        if r.status_code != 200:
            return None
        pairs = r.json()
        if not pairs:
            return None
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
        return pairs[0]
    except Exception as e:
        log.error(f"pair_data {ca}: {e}")
        return None


async def get_holder_count(client: httpx.AsyncClient, ca: str) -> int:
    """Approximate holder count via token largest accounts."""
    try:
        result = await solana_rpc(client, "getTokenLargestAccounts", [ca, {"commitment": "confirmed"}])
        accounts = (result or {}).get("value", [])
        return len([a for a in accounts if float(a.get("uiAmount", 0) or 0) > 0])
    except Exception:
        return 0


async def get_creator_age_days(client: httpx.AsyncClient, ca: str) -> float | None:
    """Estimate creator wallet age by first transaction slot."""
    try:
        # Get mint account info to find mint authority (creator)
        result = await solana_rpc(client, "getAccountInfo", [ca, {"encoding": "jsonParsed"}])
        if not result:
            return None
        parsed = ((result.get("value") or {}).get("data") or {}).get("parsed") or {}
        info   = (parsed.get("info") or {})
        mint_authority = info.get("mintAuthority")
        if not mint_authority:
            return None

        # Get oldest tx for creator
        sigs = await solana_rpc(client, "getSignaturesForAddress", [
            mint_authority, {"limit": 1, "before": None}
        ])
        if not sigs:
            return None
        oldest_slot = sigs[-1].get("blockTime")
        if not oldest_slot:
            return None
        age_days = (time.time() - oldest_slot) / 86400
        return round(age_days, 1)
    except Exception:
        return None


async def get_recent_trades(client: httpx.AsyncClient, ca: str, limit: int = 5) -> list[dict]:
    """Last N trades from DexScreener."""
    try:
        r = await client.get(f"{DEXSCREENER_API}/latest/dex/search?q={ca}", timeout=8)
        if r.status_code != 200:
            return []
        pairs = (r.json().get("pairs") or [])
        if not pairs:
            return []
        pair = pairs[0]
        txns = pair.get("txns", {})
        # Return summary per timeframe
        return [
            {"window": "5m",  "buys": txns.get("m5",  {}).get("buys",  0), "sells": txns.get("m5",  {}).get("sells", 0)},
            {"window": "1h",  "buys": txns.get("h1",  {}).get("buys",  0), "sells": txns.get("h1",  {}).get("sells", 0)},
            {"window": "24h", "buys": txns.get("h24", {}).get("buys",  0), "sells": txns.get("h24", {}).get("sells", 0)},
        ]
    except Exception:
        return []


# ── Main Enrichment ───────────────────────────────────────────────────────────

async def enrich_token(ca: str) -> dict:
    """
    Full enrichment for a token CA. Returns structured data for TUSK.

    Returns:
    {
        "ca": str,
        "symbol": str,
        "name": str,
        "price_usd": float,
        "liq_usd": float,
        "mcap_usd": float,
        "volume_24h": float,
        "price_change": {"m5": float, "h1": float, "h24": float},
        "holders": int,
        "creator_age_days": float | None,
        "trades": [{"window": str, "buys": int, "sells": int}],
        "dex_url": str,
        "pair_address": str | None,
    }
    """
    async with httpx.AsyncClient(headers={"User-Agent": "RavenX-GemHunter/1.0"}) as client:
        pair, holders, creator_age, trades = await asyncio.gather(
            get_pair_data(client, ca),
            get_holder_count(client, ca),
            get_creator_age_days(client, ca),
            get_recent_trades(client, ca),
        )

        if not pair:
            return {"ca": ca, "error": "pair not found"}

        base  = pair.get("baseToken", {})
        price = float(pair.get("priceUsd", 0) or 0)
        liq   = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        mcap  = float(pair.get("marketCap", 0) or 0)
        vol   = float((pair.get("volume") or {}).get("h24", 0) or 0)
        chg   = pair.get("priceChange", {}) or {}

        return {
            "ca":             ca,
            "symbol":         base.get("symbol", "???"),
            "name":           base.get("name", ""),
            "price_usd":      price,
            "liq_usd":        liq,
            "mcap_usd":       mcap,
            "volume_24h":     vol,
            "price_change": {
                "m5":  float(chg.get("m5",  0) or 0),
                "h1":  float(chg.get("h1",  0) or 0),
                "h24": float(chg.get("h24", 0) or 0),
            },
            "holders":          holders,
            "creator_age_days": creator_age,
            "trades":           trades,
            "dex_url":          f"https://dexscreener.com/solana/{ca}",
            "pair_address":     pair.get("pairAddress"),
        }


if __name__ == "__main__":
    import sys, json
    ca = sys.argv[1] if len(sys.argv) > 1 else "3G36hCsP5DgDT2hGxACivRvzWeuX56mU9DrFibbKpump"
    result = asyncio.run(enrich_token(ca))
    print(json.dumps(result, indent=2))
