"""
Gem Hunter — RavenX AI 🖤
=========================
browser-use agent that scans pump.fun and DexScreener for new token launches,
scores them against RAZOR DNA (the current winning meme assassin config),
and pushes qualifying gems to the TUSK Command Center /api/gems/incoming endpoint.

Run standalone:
    uv run python skills/ravenx/gem_hunter.py

Run continuously (daemon):
    uv run python skills/ravenx/gem_hunter.py --daemon --interval 60

Push to TUSK:
    TUSK_URL=http://34.182.110.4 uv run python skills/ravenx/gem_hunter.py
"""

import asyncio
import json
import os
import time
import argparse
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [GEM] %(message)s")
log = logging.getLogger("gem_hunter")

# ── Config ────────────────────────────────────────────────────────────────────

TUSK_URL         = os.getenv("TUSK_URL", "http://34.182.110.4").rstrip("/")
DEXSCREENER_API  = "https://api.dexscreener.com"
MEVX_API         = "https://api-fe.mevx.io/api"
RUGCHECK_API     = "https://api.rugcheck.xyz/v1"

# RAZOR DNA — the current winning assassin config (from leaderboard.json)
RAZOR_DNA = {
    "signal_gain_min":  0,      # min % gain signal to consider
    "age_max_min":      60,     # max token age in minutes
    "rug_score_max":    70,     # max rug score (0=safe, 100=rug)
    "liq_min_usd":      3000,   # min liquidity in USD
    "mcap_max_usd":     5000000,# max market cap in USD
    "buy_ratio_min":    50,     # min buy tx ratio %
    "take_profit":      1.5,    # 1.5x = 50% profit target
    "stop_loss":        0.85,   # 15% stop loss
    "max_hold_min":     15,     # max hold time in minutes
}

# ── DexScreener Integration ───────────────────────────────────────────────────

async def fetch_new_solana_tokens(client: httpx.AsyncClient, limit: int = 30) -> list[dict]:
    """Fetch latest Solana token profiles from DexScreener."""
    try:
        r = await client.get(
            f"{DEXSCREENER_API}/token-profiles/latest/v1",
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"DexScreener profiles: {r.status_code}")
            return []
        data = r.json()
        # Filter Solana only
        return [t for t in (data if isinstance(data, list) else []) if t.get("chainId") == "solana"][:limit]
    except Exception as e:
        log.error(f"DexScreener fetch error: {e}")
        return []


async def fetch_token_detail(client: httpx.AsyncClient, ca: str) -> dict | None:
    """Fetch detailed pair info for a token CA."""
    try:
        r = await client.get(
            f"{DEXSCREENER_API}/tokens/v1/solana/{ca}",
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        pairs = data if isinstance(data, list) else []
        if not pairs:
            return None
        # Get the highest liquidity pair
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        return pairs[0]
    except Exception as e:
        log.error(f"DexScreener detail error for {ca}: {e}")
        return None


async def fetch_pump_fun_new(client: httpx.AsyncClient, limit: int = 20) -> list[dict]:
    """Fetch newest launches from pump.fun via DexScreener."""
    try:
        # Use the correct v1 search endpoint
        r = await client.get(
            f"{DEXSCREENER_API}/latest/dex/search?q=pump.fun&chainIds=solana",
            timeout=10,
        )
        if r.status_code != 200:
            # Fallback: boosted tokens on Solana (often new launches)
            r2 = await client.get(f"{DEXSCREENER_API}/token-boosts/top/v1?chainId=solana", timeout=8)
            if r2.status_code == 200:
                boosts = r2.json() if isinstance(r2.json(), list) else []
                cas = [b.get("tokenAddress") for b in boosts[:15] if b.get("tokenAddress")]
                # Enrich each
                pairs = []
                for ca in cas[:10]:
                    detail = await fetch_token_detail(client, ca)
                    if detail:
                        pairs.append(detail)
                return pairs
            return []
        data = r.json()
        pairs = data.get("pairs") or []
        pairs.sort(key=lambda p: p.get("pairCreatedAt", 0) or 0, reverse=True)
        return pairs[:limit]
    except Exception as e:
        log.error(f"pump.fun fetch error: {e}")
        return []


# ── Rug Check ─────────────────────────────────────────────────────────────────

async def get_rug_score(client: httpx.AsyncClient, ca: str) -> int:
    """Get rug risk score from rugcheck.xyz (0=safe, 100=rug)."""
    try:
        r = await client.get(
            f"{RUGCHECK_API}/tokens/{ca}/report/summary",
            timeout=8,
        )
        if r.status_code != 200:
            return 50  # unknown = medium risk
        data = r.json()
        score = data.get("score", 50)
        return min(100, max(0, int(score)))
    except Exception:
        return 50


# ── RAZOR DNA Scorer ──────────────────────────────────────────────────────────

def age_minutes(created_at_ms: int | None) -> float:
    """How many minutes old is the token."""
    if not created_at_ms:
        return 999
    now_ms = time.time() * 1000
    return (now_ms - created_at_ms) / 60000


def buy_ratio(pair: dict) -> float:
    """Buy transaction ratio as percentage."""
    txns = pair.get("txns", {}).get("m5", {})
    buys  = txns.get("buys",  0) or 0
    sells = txns.get("sells", 0) or 0
    total = buys + sells
    if total == 0:
        return 50.0
    return (buys / total) * 100


def score_gem(pair: dict, rug_score: int) -> dict:
    """
    Score a token pair against RAZOR DNA.
    Returns {'pass': bool, 'score': int, 'reasons': list, 'flags': list}
    """
    dna    = RAZOR_DNA
    reasons = []
    flags   = []

    liq_usd  = float(pair.get("liquidity", {}).get("usd",  0) or 0)
    mcap_usd = float(pair.get("marketCap", 0) or 0)
    age_min  = age_minutes(pair.get("pairCreatedAt"))
    buy_pct  = buy_ratio(pair)
    gain_pct = float(pair.get("priceChange", {}).get("m5", 0) or 0)

    score = 0

    # Liquidity check
    if liq_usd >= dna["liq_min_usd"]:
        score += 20
        reasons.append(f"liq ${liq_usd:.0f} ✓")
    else:
        flags.append(f"low liq ${liq_usd:.0f}")

    # Market cap check
    if mcap_usd <= dna["mcap_max_usd"] or mcap_usd == 0:
        score += 20
        reasons.append(f"mcap ${mcap_usd:.0f} ✓")
    else:
        flags.append(f"mcap too high ${mcap_usd:.0f}")

    # Age check
    if age_min <= dna["age_max_min"]:
        score += 20
        reasons.append(f"age {age_min:.1f}m ✓")
    else:
        flags.append(f"too old {age_min:.1f}m")

    # Buy ratio check
    if buy_pct >= dna["buy_ratio_min"]:
        score += 20
        reasons.append(f"buys {buy_pct:.0f}% ✓")
    else:
        flags.append(f"sell pressure {buy_pct:.0f}% buys")

    # Rug score check
    if rug_score <= dna["rug_score_max"]:
        score += 20
        reasons.append(f"rug {rug_score} ✓")
    else:
        flags.append(f"rug risk {rug_score}")

    # Hard requirements: must have real liquidity AND pass 3/5 criteria
    passed = score >= 60 and liq_usd >= dna["liq_min_usd"]

    return {
        "pass":    passed,
        "score":   score,
        "reasons": reasons,
        "flags":   flags,
        "liq_usd":  liq_usd,
        "mcap_usd": mcap_usd,
        "age_min":  age_min,
        "buy_pct":  buy_pct,
        "rug_score": rug_score,
        "gain_pct":  gain_pct,
    }


# ── TUSK Push ─────────────────────────────────────────────────────────────────

async def push_to_tusk(client: httpx.AsyncClient, gems: list[dict]) -> bool:
    """Push scored gems to TUSK /api/gems/incoming."""
    if not gems:
        return True
    try:
        r = await client.post(
            f"{TUSK_URL}/api/gems/incoming",
            json=gems,
            timeout=8,
        )
        if r.status_code in (200, 201):
            log.info(f"✅ Pushed {len(gems)} gems to TUSK")
            return True
        else:
            log.warning(f"TUSK push {r.status_code}: {r.text[:100]}")
            return False
    except Exception as e:
        log.error(f"TUSK push error: {e}")
        return False


# ── Main Hunt Loop ────────────────────────────────────────────────────────────

async def hunt_once() -> list[dict]:
    """Run one gem hunt cycle. Returns list of qualifying gems."""
    qualifying = []

    async with httpx.AsyncClient(headers={"User-Agent": "RavenX-GemHunter/1.0"}) as client:

        # Pull new tokens from both sources
        log.info("🔍 Scanning pump.fun + DexScreener...")
        pump_pairs = await fetch_pump_fun_new(client, limit=30)
        dex_tokens = await fetch_new_solana_tokens(client, limit=20)

        # Enrich dex_tokens with pair details
        dex_pairs = []
        for tok in dex_tokens[:10]:  # limit to avoid rate limit
            ca = tok.get("tokenAddress")
            if ca:
                pair = await fetch_token_detail(client, ca)
                if pair:
                    dex_pairs.append(pair)

        all_pairs = pump_pairs + dex_pairs
        seen_cas: set[str] = set()
        log.info(f"📊 Evaluating {len(all_pairs)} pairs...")

        for pair in all_pairs:
            ca = (pair.get("baseToken") or {}).get("address", "")
            symbol = (pair.get("baseToken") or {}).get("symbol", "???")

            if not ca or ca in seen_cas:
                continue
            seen_cas.add(ca)

            # Get rug score
            rug = await get_rug_score(client, ca)

            # Score against RAZOR DNA
            result = score_gem(pair, rug)

            if result["pass"]:
                gem = {
                    "ts":       datetime.now(timezone.utc).isoformat(),
                    "bot":      "GEM_HUNTER",
                    "symbol":   symbol,
                    "ca":       ca,
                    "pnl_sol":  0,
                    "pnl_x":    1 + (result["gain_pct"] / 100),
                    "sol_in":   RAZOR_DNA["take_profit"],
                    "held_min": 0,
                    "reason":   f"SCORE {result['score']}/100 | " + " | ".join(result["reasons"]),
                    "win":      result["gain_pct"] > 0,
                    "meta": {
                        "liq_usd":    result["liq_usd"],
                        "mcap_usd":   result["mcap_usd"],
                        "age_min":    result["age_min"],
                        "buy_pct":    result["buy_pct"],
                        "rug_score":  result["rug_score"],
                        "dexUrl":     f"https://dexscreener.com/solana/{ca}",
                        "score":      result["score"],
                    }
                }
                qualifying.append(gem)
                log.info(f"  💎 {symbol} ({ca[:8]}...) score={result['score']} liq=${result['liq_usd']:.0f} age={result['age_min']:.1f}m")
            else:
                log.debug(f"  ❌ {symbol} flags: {result['flags']}")

        log.info(f"✅ Hunt complete: {len(qualifying)}/{len(all_pairs)} qualify")

        if qualifying:
            await push_to_tusk(client, qualifying)

    return qualifying


async def run_daemon(interval_seconds: int = 60):
    """Run gem hunter continuously."""
    log.info(f"🖤 RavenX Gem Hunter daemon started (interval: {interval_seconds}s)")
    log.info(f"   TUSK endpoint: {TUSK_URL}")
    log.info(f"   RAZOR DNA: liq≥${RAZOR_DNA['liq_min_usd']} age≤{RAZOR_DNA['age_max_min']}m rug≤{RAZOR_DNA['rug_score_max']}")

    while True:
        try:
            gems = await hunt_once()
            log.info(f"💤 Next hunt in {interval_seconds}s...")
        except Exception as e:
            log.error(f"Hunt error: {e}")
        await asyncio.sleep(interval_seconds)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RavenX Gem Hunter 🖤")
    parser.add_argument("--daemon",   action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=60, help="Daemon interval in seconds")
    parser.add_argument("--tusk",     type=str, default=TUSK_URL, help="TUSK base URL")
    args = parser.parse_args()

    if args.tusk:
        TUSK_URL = args.tusk.rstrip("/")

    if args.daemon:
        asyncio.run(run_daemon(args.interval))
    else:
        gems = asyncio.run(hunt_once())
        print(json.dumps(gems, indent=2))
