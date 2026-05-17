"""Urllib-based Binance HTTP client for public market data.

CCXT/aiohttp may fail with IPv6 "No route to host" on some networks while
urllib/curl works. Use these helpers for snapshots and candles.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, List, Optional, Set

USER_AGENT = "valuecell/1.0"

_fapi_perpetual_usdt_ids: Optional[Set[str]] = None
_fapi_symbols_lock = asyncio.Lock()


async def fetch_json(url: str, timeout_s: float = 15.0) -> Any:
    """GET JSON from a public Binance URL."""

    def _fetch_sync() -> Any:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                payload = json.loads(body)
                msg = payload.get("msg", body)
                code = payload.get("code")
                raise ValueError(
                    f"Binance HTTP {exc.code}: {msg} (code={code})"
                ) from exc
            except json.JSONDecodeError:
                raise ValueError(f"Binance HTTP {exc.code}: {body}") from exc

    return await asyncio.to_thread(_fetch_sync)


async def get_fapi_perpetual_usdt_ids() -> Set[str]:
    """Cached set of tradable Binance USDT-M perpetual symbol ids (e.g. BTCUSDT)."""
    global _fapi_perpetual_usdt_ids
    if _fapi_perpetual_usdt_ids is not None:
        return _fapi_perpetual_usdt_ids
    async with _fapi_symbols_lock:
        if _fapi_perpetual_usdt_ids is not None:
            return _fapi_perpetual_usdt_ids
        data = await fetch_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
        ids: Set[str] = set()
        for row in data.get("symbols") or []:
            if (
                row.get("status") == "TRADING"
                and row.get("contractType") == "PERPETUAL"
                and row.get("quoteAsset") == "USDT"
            ):
                sym = row.get("symbol")
                if sym:
                    ids.add(str(sym))
        _fapi_perpetual_usdt_ids = ids
        return ids


async def is_fapi_perpetual_symbol(symbol: str) -> bool:
    """Return True if symbol exists as a Binance USDT-M perpetual."""
    sym_id = symbol_to_binance_id(symbol)
    return sym_id in await get_fapi_perpetual_usdt_ids()


def symbol_to_binance_id(symbol: str) -> str:
    """Convert ETH/USDT or ETH/USDT:USDT to ETHUSDT."""
    base = symbol.split(":")[0]
    return base.replace("/", "").replace("-", "")


def binance_id_to_symbol(binance_id: str) -> str:
    """Convert ETHUSDT to ETH/USDT."""
    if binance_id.endswith("USDT"):
        return f"{binance_id[:-4]}/USDT"
    if binance_id.endswith("USDC"):
        return f"{binance_id[:-4]}/USDC"
    if binance_id.endswith("USD"):
        return f"{binance_id[:-3]}/USD"
    return binance_id


# USDT-M futures klines do not support 1s; map to 1m for micro-interval requests.
_FAPI_KLINE_INTERVAL_MAP = {"1s": "1m"}


def ticker_from_fapi_24hr(data: dict, symbol: str) -> dict:
    """Shape Binance fapi 24hr ticker into a CCXT-like ticker dict."""
    if data.get("code") is not None and data.get("msg"):
        raise ValueError(f"Binance ticker error for {symbol}: {data.get('msg')}")
    if "lastPrice" not in data:
        raise ValueError(
            f"Binance ticker for {symbol} missing lastPrice (not a valid fapi symbol?)"
        )
    last = float(data["lastPrice"])
    return {
        "symbol": symbol,
        "timestamp": int(data.get("closeTime") or 0),
        "high": float(data.get("highPrice", last)),
        "low": float(data.get("lowPrice", last)),
        "bid": float(data.get("bidPrice", last)),
        "ask": float(data.get("askPrice", last)),
        "last": last,
        "close": last,
        "open": float(data.get("openPrice", last)),
        "change": float(data.get("priceChange", 0)),
        "percentage": float(data.get("priceChangePercent", 0)),
        "baseVolume": float(data.get("volume", 0)),
        "quoteVolume": float(data.get("quoteVolume", 0)),
        "info": data,
    }


async def fetch_fapi_ticker(symbol: str) -> dict:
    """Fetch USDT-M perpetual 24hr ticker for a symbol like ETH/USDT."""
    sym_id = symbol_to_binance_id(symbol)
    if sym_id not in await get_fapi_perpetual_usdt_ids():
        raise ValueError(
            f"{symbol} ({sym_id}) is not listed on Binance USDT-M perpetual futures"
        )
    url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym_id}"
    data = await fetch_json(url)
    return ticker_from_fapi_24hr(data, symbol)


async def fetch_fapi_klines(
    symbol: str, interval: str, limit: int
) -> List[list]:
    """Fetch USDT-M perpetual klines (CCXT OHLCV row format)."""
    sym_id = symbol_to_binance_id(symbol)
    if sym_id not in await get_fapi_perpetual_usdt_ids():
        raise ValueError(
            f"{symbol} ({sym_id}) is not listed on Binance USDT-M perpetual futures"
        )
    fapi_interval = _FAPI_KLINE_INTERVAL_MAP.get(interval, interval)
    url = (
        "https://fapi.binance.com/fapi/v1/klines"
        f"?symbol={sym_id}&interval={fapi_interval}&limit={limit}"
    )
    rows = await fetch_json(url)
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected klines response for {symbol}")
    return rows
