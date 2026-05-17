"""Tests for Indodax symbol parsing (no network)."""

from __future__ import annotations

import pytest

from valuecell.agents.common.trading.data import indodax_http


def test_parse_symbol_parts() -> None:
    assert indodax_http._parse_symbol_parts("BTC/IDR") == ("BTC", "IDR")
    assert indodax_http._parse_symbol_parts("ETH-IDR") == ("ETH", "IDR")


def test_ohlcv_rows_from_history() -> None:
    raw = [
        {
            "Time": 1708416900,
            "Open": 1.0,
            "High": 2.0,
            "Low": 0.5,
            "Close": 1.5,
            "Volume": "10",
        }
    ]
    rows = indodax_http.ohlcv_rows_from_history(raw)
    assert rows == [[1708416900000, 1.0, 2.0, 0.5, 1.5, 10.0]]


@pytest.mark.asyncio
async def test_resolve_pair_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pairs = [
        {
            "id": "btcidr",
            "ticker_id": "btc_idr",
            "traded_currency": "btc",
            "base_currency": "idr",
            "trade_min_base_currency": 50000,
            "trade_min_traded_currency": 0.0001,
        }
    ]

    async def _fake_get_pairs() -> list:
        return fake_pairs

    monkeypatch.setattr(indodax_http, "get_pairs", _fake_get_pairs)
    pair = await indodax_http.resolve_pair("BTC/IDR")
    assert pair["id"] == "btcidr"
    assert await indodax_http.symbol_to_chart_id("BTC-IDR") == "btcidr"
    assert await indodax_http.symbol_to_ticker_id("BTC/IDR") == "btc_idr"


@pytest.mark.asyncio
async def test_build_strategy_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_constraints(symbol: str) -> dict:
        if "BTC" in symbol:
            return {
                "min_notional_idr": 50_000,
                "min_traded_currency": 0.0001,
                "price_precision": 1000,
                "quantity_step": 1e-8,
            }
        return {
            "min_notional_idr": 75_000,
            "min_traded_currency": 0.01,
            "price_precision": 1,
            "quantity_step": 0.01,
        }

    monkeypatch.setattr(indodax_http, "get_trade_constraints", _fake_constraints)
    out = await indodax_http.build_strategy_constraints(["BTC/IDR", "ETH/IDR"])
    assert out["min_notional"] == 75_000
    assert out["min_trade_qty"] == 0.01
    assert out["quantity_step"] == 1e-8
