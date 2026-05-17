import asyncio
import itertools
from collections import defaultdict
from typing import List, Optional

from loguru import logger

from valuecell.agents.common.trading.data import binance_http, indodax_http
from valuecell.agents.common.trading.models import (
    Candle,
    InstrumentRef,
    MarketSnapShotType,
)
from valuecell.agents.common.trading.utils import get_exchange_cls, normalize_symbol

from .interfaces import BaseMarketDataSource


def _ccxt_client_config(exchange_id: str) -> dict:
    """CCXT client options that avoid broken IPv6 routes on some hosts."""
    config: dict = {
        "newUpdates": False,
        "aiohttp_trust_env": False,
    }
    if exchange_id == "binance":
        config["options"] = {
            "defaultType": "future",
            "fetchCurrencies": False,
        }
        # Prefer IPv4; aiohttp happy-eyeballs may pick IPv6 and fail (errno 65).
        config["family"] = 4
    return config


class SimpleMarketDataSource(BaseMarketDataSource):
    """Generates synthetic candle data for each symbol or fetches via ccxt.pro.

    If `exchange_id` was provided at construction time and `ccxt.pro` is
    available, this class will attempt to fetch OHLCV data from the
    specified exchange. If any error occurs (missing library, unknown
    exchange, network error), it falls back to the built-in synthetic
    generator so the runtime remains functional in tests and offline.
    """

    def __init__(self, exchange_id: Optional[str] = None) -> None:
        if not exchange_id:
            self._exchange_id = "okx"
        else:
            self._exchange_id = exchange_id

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol format for the active exchange."""
        base_symbol = symbol.replace("-", "/").split(":")[0]
        if self._exchange_id == "indodax":
            return base_symbol
        if ":" not in base_symbol:
            parts = base_symbol.split("/")
            if len(parts) == 2:
                base_symbol = f"{parts[0]}/{parts[1]}:{parts[1]}"
        return base_symbol

    async def _get_binance_candles_urllib(
        self, symbols: List[str], interval: str, lookback: int
    ) -> List[Candle]:
        """Fetch perpetual candles via urllib (no ccxt/aiohttp)."""

        async def _one(symbol: str) -> List[Candle]:
            out: List[Candle] = []
            try:
                raw = await binance_http.fetch_fapi_klines(
                    symbol, interval, lookback
                )
                for row in raw:
                    ts, open_v, high_v, low_v, close_v, vol = row[:6]
                    out.append(
                        Candle(
                            ts=int(ts),
                            instrument=InstrumentRef(
                                symbol=symbol,
                                exchange_id=self._exchange_id,
                            ),
                            open=float(open_v),
                            high=float(high_v),
                            low=float(low_v),
                            close=float(close_v),
                            volume=float(vol),
                            interval=interval,
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "Binance urllib candles failed for {symbol} interval={interval}: {err}. "
                    "Remove this symbol or pick a Binance USDT-M perpetual pair.",
                    symbol=symbol,
                    interval=interval,
                    err=exc,
                )
            return out

        chunks = await asyncio.gather(*[_one(s) for s in symbols])
        return list(itertools.chain.from_iterable(chunks))

    async def _get_indodax_candles_urllib(
        self, symbols: List[str], interval: str, lookback: int
    ) -> List[Candle]:
        """Fetch spot IDR candles via Indodax tradingview/history_v2."""

        async def _one(symbol: str) -> List[Candle]:
            out: List[Candle] = []
            try:
                raw = await indodax_http.fetch_ohlcv(symbol, interval, lookback)
                for row in raw:
                    ts, open_v, high_v, low_v, close_v, vol = row[:6]
                    out.append(
                        Candle(
                            ts=int(ts),
                            instrument=InstrumentRef(
                                symbol=symbol,
                                exchange_id=self._exchange_id,
                            ),
                            open=float(open_v),
                            high=float(high_v),
                            low=float(low_v),
                            close=float(close_v),
                            volume=float(vol),
                            interval=interval,
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "Indodax urllib candles failed for {symbol} interval={interval}: {err}. "
                    "Use a spot pair listed on Indodax (quote IDR), e.g. BTC/IDR.",
                    symbol=symbol,
                    interval=interval,
                    err=exc,
                )
            return out

        chunks = await asyncio.gather(*[_one(s) for s in symbols])
        return list(itertools.chain.from_iterable(chunks))

    async def get_recent_candles(
        self, symbols: List[str], interval: str, lookback: int
    ) -> List[Candle]:
        if self._exchange_id == "binance":
            candles = await self._get_binance_candles_urllib(
                symbols, interval, lookback
            )
            logger.debug(
                "Fetched {count} Binance urllib candles symbols={symbols} interval={interval}",
                count=len(candles),
                symbols=symbols,
                interval=interval,
            )
            return candles

        if self._exchange_id == "indodax":
            candles = await self._get_indodax_candles_urllib(
                symbols, interval, lookback
            )
            logger.debug(
                "Fetched {count} Indodax urllib candles symbols={symbols} interval={interval}",
                count=len(candles),
                symbols=symbols,
                interval=interval,
            )
            return candles

        async def _fetch_and_process(symbol: str) -> List[Candle]:
            exchange_cls = get_exchange_cls(self._exchange_id)
            exchange = exchange_cls(_ccxt_client_config(self._exchange_id))

            symbol_candles: List[Candle] = []
            normalized_symbol = self._normalize_symbol(symbol)
            try:
                try:
                    raw = await exchange.fetch_ohlcv(
                        normalized_symbol,
                        timeframe=interval,
                        since=None,
                        limit=lookback,
                    )
                finally:
                    try:
                        await exchange.close()
                    except Exception:
                        pass

                for row in raw:
                    ts, open_v, high_v, low_v, close_v, vol = row
                    symbol_candles.append(
                        Candle(
                            ts=int(ts),
                            instrument=InstrumentRef(
                                symbol=symbol,
                                exchange_id=self._exchange_id,
                            ),
                            open=float(open_v),
                            high=float(high_v),
                            low=float(low_v),
                            close=float(close_v),
                            volume=float(vol),
                            interval=interval,
                        )
                    )
                return symbol_candles
            except Exception as exc:
                logger.warning(
                    "Failed to fetch candles for {} (normalized: {}) from {}, "
                    "interval={}, return empty candles. Error: {}",
                    symbol,
                    normalized_symbol,
                    self._exchange_id,
                    interval,
                    exc,
                )
                return []

        tasks = [_fetch_and_process(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks)
        candles: List[Candle] = list(itertools.chain.from_iterable(results))

        logger.debug(
            "Fetch {count} candles symbols: {symbols}, interval: {interval}, lookback: {lookback}",
            count=len(candles),
            symbols=symbols,
            interval=interval,
            lookback=lookback,
        )
        return candles

    async def _get_binance_snapshot_urllib(
        self, symbols: List[str]
    ) -> MarketSnapShotType:
        """Fetch tickers via urllib for Binance USDT-M perpetuals."""
        snapshot: MarketSnapShotType = {}

        async def _one(symbol: str) -> None:
            try:
                ticker = await binance_http.fetch_fapi_ticker(symbol)
                snapshot[symbol] = {"price": ticker}
            except Exception as exc:
                logger.warning(
                    "Binance urllib ticker failed for {symbol}: {err}. "
                    "Remove this symbol or pick a Binance USDT-M perpetual pair.",
                    symbol=symbol,
                    err=exc,
                )

        await asyncio.gather(*[_one(s) for s in symbols])
        return snapshot

    async def _get_indodax_snapshot_urllib(
        self, symbols: List[str]
    ) -> MarketSnapShotType:
        """Fetch tickers via Indodax public REST API."""
        snapshot: MarketSnapShotType = {}

        async def _one(symbol: str) -> None:
            try:
                ticker = await indodax_http.fetch_ticker(symbol)
                snapshot[symbol] = {"price": ticker}
            except Exception as exc:
                logger.warning(
                    "Indodax urllib ticker failed for {symbol}: {err}",
                    symbol=symbol,
                    err=exc,
                )

        await asyncio.gather(*[_one(s) for s in symbols])
        return snapshot

    async def get_market_snapshot(self, symbols: List[str]) -> MarketSnapShotType:
        """Fetch latest prices for the given symbols using exchange endpoints."""
        if self._exchange_id == "binance":
            snapshot = await self._get_binance_snapshot_urllib(symbols)
            logger.debug(
                "Binance urllib market snapshot for {count}/{total} symbols",
                count=len(snapshot),
                total=len(symbols),
            )
            return snapshot

        if self._exchange_id == "indodax":
            snapshot = await self._get_indodax_snapshot_urllib(symbols)
            logger.debug(
                "Indodax urllib market snapshot for {count}/{total} symbols",
                count=len(snapshot),
                total=len(symbols),
            )
            return snapshot

        snapshot = defaultdict(dict)

        exchange_cls = get_exchange_cls(self._exchange_id)
        exchange = exchange_cls(_ccxt_client_config(self._exchange_id))
        try:
            for symbol in symbols:
                sym = normalize_symbol(symbol, self._exchange_id)
                try:
                    ticker = await exchange.fetch_ticker(sym)
                    snapshot[symbol]["price"] = ticker

                    try:
                        oi = await exchange.fetch_open_interest(sym)
                        snapshot[symbol]["open_interest"] = oi
                    except Exception:
                        logger.debug(
                            "Open interest unavailable for {symbol} on {exchange}",
                            symbol=symbol,
                            exchange=self._exchange_id,
                        )

                    try:
                        fr = await exchange.fetch_funding_rate(sym)
                        snapshot[symbol]["funding_rate"] = fr
                    except Exception:
                        logger.debug(
                            "Funding rate unavailable for {symbol} on {exchange}",
                            symbol=symbol,
                            exchange=self._exchange_id,
                        )
                except Exception:
                    logger.exception(
                        "Failed to fetch market snapshot for {} at {}",
                        symbol,
                        self._exchange_id,
                    )
        finally:
            try:
                await exchange.close()
            except Exception:
                logger.exception(
                    "Failed to close exchange connection for {}",
                    self._exchange_id,
                )

        return dict(snapshot)
