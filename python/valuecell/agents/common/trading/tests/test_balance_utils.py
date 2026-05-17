"""Tests for CCXT balance parsing helpers."""

from __future__ import annotations

from valuecell.agents.common.trading.utils import (
    iter_ccxt_balance_holdings,
    parse_ccxt_balance_maps,
)


def test_parse_ccxt_balance_maps_lowercase_keys() -> None:
    balance = {
        "free": {"idr": 50_000.0, "xrp": 12.5},
        "total": {"idr": 50_000.0, "xrp": 12.5},
    }
    free_map, total_map = parse_ccxt_balance_maps(balance)
    assert free_map["IDR"] == 50_000.0
    assert total_map["XRP"] == 12.5


def test_iter_ccxt_balance_holdings_skips_zero() -> None:
    balance = {
        "total": {"idr": 0.0, "xrp": 3.0, "btc": 0.0},
    }
    holdings = dict(iter_ccxt_balance_holdings(balance))
    assert holdings == {"XRP": 3.0}
