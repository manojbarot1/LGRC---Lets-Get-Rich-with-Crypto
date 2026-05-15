"""Real-time crypto price data from CoinGecko (free, no API key) + Fear & Greed index."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

_BASE = "https://api.coingecko.com/api/v3"
_TIMEOUT = 12.0

# Map our short symbols to CoinGecko IDs
SYMBOL_TO_CG = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "XRP": "ripple", "ADA": "cardano",
    "DOGE": "dogecoin", "AVAX": "avalanche-2", "LINK": "chainlink",
    "DOT": "polkadot", "MATIC": "matic-network", "UNI": "uniswap",
    "LTC": "litecoin", "ATOM": "cosmos", "NEAR": "near",
    "SUI": "sui", "APT": "aptos", "ARB": "arbitrum",
    "OP": "optimism", "INJ": "injective-protocol",
    "PEPE": "pepe", "SHIB": "shiba-inu", "TRX": "tron",
    "TON": "the-open-network", "FIL": "filecoin",
    "SEI": "sei-network", "TIA": "celestia", "JUP": "jupiter-exchange-solana",
    "WIF": "dogwifcoin", "BONK": "bonk",
    # Extended — commonly suggested by AI models
    "RUNE": "thorchain", "HBAR": "hedera-hashgraph", "VET": "vechain",
    "ALGO": "algorand", "ICP": "internet-computer", "SAND": "the-sandbox",
    "MANA": "decentraland", "AXS": "axie-infinity", "GALA": "gala",
    "FTM": "fantom", "CRV": "curve-dao-token", "AAVE": "aave",
    "MKR": "maker", "SNX": "havven", "LDO": "lido-dao",
    "RNDR": "render-token", "FET": "fetch-ai", "AGIX": "singularitynet",
    "STX": "blockstack", "ROSE": "oasis-network", "ONE": "harmony",
    "ZIL": "zilliqa", "XLM": "stellar", "EOS": "eos",
    "CAKE": "pancakeswap-token", "GMT": "stepn", "CFX": "conflux-token",
}

CG_TO_SYMBOL = {v: k for k, v in SYMBOL_TO_CG.items()}


async def get_fear_greed_index() -> dict:
    """Crypto Fear & Greed Index from alternative.me (free, no key)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get("https://api.alternative.me/fng/")
        if r.status_code == 200:
            data = r.json()["data"][0]
            return {
                "value": int(data["value"]),
                "classification": data["value_classification"],
            }
    except Exception:
        pass
    return {"value": 50, "classification": "Neutral"}


async def get_top_movers(limit: int = 30) -> list[dict]:
    """Top coins ranked by 24h % change with 1h, 24h, 7d data."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_BASE}/coins/markets", params={
                "vs_currency": "usd",
                "order": "percent_change_24h_desc",
                "per_page": limit,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            })
        if r.status_code != 200:
            return []
        return [
            {
                "symbol": item["symbol"].upper(),
                "name": item["name"],
                "cg_id": item["id"],
                "price": item["current_price"],
                "change_1h": item.get("price_change_percentage_1h_in_currency") or 0.0,
                "change_24h": item.get("price_change_percentage_24h") or 0.0,
                "change_7d": item.get("price_change_percentage_7d_in_currency") or 0.0,
                "volume_24h": item.get("total_volume") or 0.0,
                "market_cap": item.get("market_cap") or 0.0,
                "ath_change_pct": item.get("ath_change_percentage") or 0.0,
            }
            for item in r.json()
        ]
    except Exception as e:
        log.warning("prices.top_movers_failed", error=str(e))
        return []


async def get_trending() -> list[dict]:
    """CoinGecko's trending coins (searched most in last 24h)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_BASE}/search/trending")
        if r.status_code != 200:
            return []
        return [
            {
                "symbol": item["item"]["symbol"].upper(),
                "name": item["item"]["name"],
                "cg_id": item["item"]["id"],
                "rank": item["item"].get("market_cap_rank"),
            }
            for item in r.json().get("coins", [])
        ]
    except Exception as e:
        log.warning("prices.trending_failed", error=str(e))
        return []


async def get_prices_by_cg_id(cg_ids: list[str]) -> dict[str, float]:
    if not cg_ids:
        return {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_BASE}/simple/price", params={
                "ids": ",".join(cg_ids),
                "vs_currencies": "usd",
            })
        if r.status_code != 200:
            return {}
        return {cg_id: data.get("usd", 0.0) for cg_id, data in r.json().items()}
    except Exception as e:
        log.warning("prices.fetch_failed", error=str(e))
        return {}


async def get_prices_for_symbols(symbols: list[str]) -> dict[str, float]:
    cg_ids = [SYMBOL_TO_CG[s] for s in symbols if s in SYMBOL_TO_CG]
    if not cg_ids:
        return {}
    raw = await get_prices_by_cg_id(cg_ids)
    return {CG_TO_SYMBOL[cg_id]: price for cg_id, price in raw.items() if cg_id in CG_TO_SYMBOL}


async def get_market_snapshot() -> dict[str, Any]:
    """Combined snapshot: top movers + trending + fear/greed, fetched in parallel."""
    top, trending, fg = await asyncio.gather(
        get_top_movers(30),
        get_trending(),
        get_fear_greed_index(),
    )
    return {"top_movers": top, "trending": trending, "fear_greed": fg}
