"""Urllib-based Indodax public market data (spot IDR pairs).

Indodax is not available in ccxt.pro; use these helpers for candles and tickers
so strategies get real OHLCV for indicators without ccxt.pro.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

USER_AGENT = "valuecell/1.0"
BASE_URL = "https://indodax.com"

_pairs_cache: Optional[List[dict]] = None
_pairs_lock = asyncio.Lock()

# Pipeline may request 1s; Indodax minimum is 1m.
_INTERVAL_TO_TF: Dict[str, str] = {
    "1s": "1",
    "1m": "1",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "1D",
    "3d": "3D",
    "1w": "1W",
}

_SECONDS_PER_INTERVAL: Dict[str, int] = {
    "1s": 60,
    "1m": 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 3600,
    "4h": 4 * 3600,
    "1d": 86400,
    "3d": 3 * 86400,
    "1w": 7 * 86400,
}


async def fetch_json(url: str, timeout_s: float = 15.0) -> Any:
    """GET JSON from a public Indodax URL."""

    def _fetch_sync() -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise ValueError(f"Indodax HTTP {exc.code}: {body}") from exc

    return await asyncio.to_thread(_fetch_sync)


async def get_pairs() -> List[dict]:
    """Cached /api/pairs list."""
    global _pairs_cache
    if _pairs_cache is not None:
        return _pairs_cache
    async with _pairs_lock:
        if _pairs_cache is not None:
            return _pairs_cache
        data = await fetch_json(f"{BASE_URL}/api/pairs")
        if not isinstance(data, list):
            raise ValueError("Unexpected Indodax /api/pairs response")
        _pairs_cache = data
        return data


def _parse_symbol_parts(symbol: str) -> Tuple[str, str]:
    """Return (base, quote) uppercased, e.g. BTC, IDR."""
    base = symbol.split(":")[0]
    if "/" in base:
        parts = base.split("/")
    elif "-" in base:
        parts = base.split("-")
    else:
        raise ValueError(f"Invalid Indodax symbol format: {symbol}")
    if len(parts) != 2:
        raise ValueError(f"Invalid Indodax symbol format: {symbol}")
    return parts[0].upper(), parts[1].upper()


async def resolve_pair(symbol: str) -> dict:
    """Find pair metadata for a unified symbol like BTC/IDR."""
    base_ccy, quote_ccy = _parse_symbol_parts(symbol)
    if quote_ccy != "IDR":
        raise ValueError(
            f"Indodax spot pairs use IDR quote; got {symbol} (quote={quote_ccy})"
        )
    traded = base_ccy.lower()
    for row in await get_pairs():
        if (
            str(row.get("traded_currency", "")).lower() == traded
            and str(row.get("base_currency", "")).lower() == "idr"
        ):
            return row
    raise ValueError(f"{symbol} is not listed on Indodax")


async def symbol_to_chart_id(symbol: str) -> str:
    """CCXT market id for tradingview/history_v2 (e.g. btcidr)."""
    pair = await resolve_pair(symbol)
    chart_id = pair.get("id")
    if not chart_id:
        raise ValueError(f"No chart id for {symbol}")
    return str(chart_id)


async def symbol_to_ticker_id(symbol: str) -> str:
    """ticker_id for /api/ticker/$pair_id (e.g. btc_idr)."""
    pair = await resolve_pair(symbol)
    ticker_id = pair.get("ticker_id")
    if not ticker_id:
        raise ValueError(f"No ticker_id for {symbol}")
    return str(ticker_id)


def _interval_seconds(interval: str) -> int:
    return _SECONDS_PER_INTERVAL.get(interval, 60)


def _map_timeframe(interval: str) -> str:
    tf = _INTERVAL_TO_TF.get(interval)
    if tf is None:
        raise ValueError(
            f"Unsupported Indodax interval {interval!r}; "
            f"use one of {sorted(_INTERVAL_TO_TF)}"
        )
    return tf


def ohlcv_rows_from_history(data: List[dict]) -> List[list]:
    """Convert history_v2 candles to CCXT OHLCV rows [ts_ms, o, h, l, c, v]."""
    rows: List[list] = []
    for candle in data:
        ts_sec = int(candle["Time"])
        rows.append(
            [
                ts_sec * 1000,
                float(candle["Open"]),
                float(candle["High"]),
                float(candle["Low"]),
                float(candle["Close"]),
                float(candle.get("Volume") or 0),
            ]
        )
    return rows


def ticker_from_api(data: dict, symbol: str) -> dict:
    """Shape /api/ticker response into a CCXT-like ticker dict."""
    ticker = data.get("ticker") or {}
    last = float(ticker.get("last") or 0)
    if last <= 0:
        raise ValueError(f"Indodax ticker for {symbol} missing last price")
    high = float(ticker.get("high") or last)
    low = float(ticker.get("low") or last)
    buy = float(ticker.get("buy") or last)
    sell = float(ticker.get("sell") or last)
    server_time = ticker.get("server_time")
    ts_ms = int(server_time) * 1000 if server_time else int(time.time() * 1000)
    return {
        "symbol": symbol,
        "timestamp": ts_ms,
        "high": high,
        "low": low,
        "bid": buy,
        "ask": sell,
        "last": last,
        "close": last,
        "open": last,
        "change": 0.0,
        "percentage": 0.0,
        "baseVolume": 0.0,
        "quoteVolume": 0.0,
        "info": data,
    }


async def fetch_ohlcv(
    symbol: str, interval: str, limit: int
) -> List[list]:
    """Fetch OHLCV via /tradingview/history_v2."""
    chart_id = await symbol_to_chart_id(symbol)
    tf = _map_timeframe(interval)
    now = int(time.time())
    duration = _interval_seconds(interval)
    start = now - max(limit, 1) * duration - 1
    url = (
        f"{BASE_URL}/tradingview/history_v2"
        f"?from={start}&to={now}&tf={tf}&symbol={chart_id}"
    )
    data = await fetch_json(url)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected OHLCV response for {symbol}")
    rows = ohlcv_rows_from_history(data)
    if limit > 0 and len(rows) > limit:
        rows = rows[-limit:]
    return rows


async def fetch_ticker(symbol: str) -> dict:
    """Fetch single-pair ticker via /api/ticker/$pair_id."""
    ticker_id = await symbol_to_ticker_id(symbol)
    url = f"{BASE_URL}/api/ticker/{ticker_id}"
    data = await fetch_json(url)
    return ticker_from_api(data, symbol.replace("-", "/").split(":")[0])


async def get_trade_constraints(symbol: str) -> Dict[str, float]:
    """Return min_notional (IDR) and min base size from /api/pairs."""
    pair = await resolve_pair(symbol)
    min_idr = float(pair.get("trade_min_base_currency") or 50_000)
    min_base = float(pair.get("trade_min_traded_currency") or 0.0)
    price_inc = float(pair.get("price_precision") or 1)
    vol_prec = pair.get("volume_precision")
    vol_prec_int = int(vol_prec) if vol_prec is not None else 0
    if vol_prec_int > 0:
        qty_step = 10.0 ** (-vol_prec_int)
    else:
        qty_step = min_base if min_base > 0 else 1.0
    return {
        "min_notional_idr": min_idr,
        "min_traded_currency": min_base,
        "price_precision": price_inc,
        "quantity_step": qty_step,
    }


async def build_strategy_constraints(
    symbols: List[str],
) -> Dict[str, Optional[float]]:
    """Aggregate Indodax guardrails for a multi-symbol strategy (quote = IDR)."""
    if not symbols:
        return {}
    min_notional = 0.0
    min_trade_qty = 0.0
    quantity_step: Optional[float] = None
    for symbol in symbols:
        row = await get_trade_constraints(symbol)
        min_notional = max(min_notional, row["min_notional_idr"])
        min_trade_qty = max(min_trade_qty, row["min_traded_currency"])
        step = float(row["quantity_step"])
        quantity_step = step if quantity_step is None else min(quantity_step, step)
    return {
        "min_notional": min_notional,
        "min_trade_qty": min_trade_qty,
        "quantity_step": quantity_step,
    }
