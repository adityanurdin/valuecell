"""CCXT-based real exchange execution gateway.

Supports:
- Spot trading
- Futures/Perpetual contracts (USDT-margined, coin-margined)
- Leverage trading (cross/isolated margin)
- Multiple exchanges via CCXT unified API
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

import ccxt.async_support as ccxt
from loguru import logger

from valuecell.agents.common.trading.models import (
    FeatureVector,
    MarketType,
    PriceMode,
    TradeInstruction,
    TradeSide,
    TxResult,
    TxStatus,
    derive_side_from_action,
)

from .interfaces import BaseExecutionGateway


class CCXTExecutionGateway(BaseExecutionGateway):
    """Async execution gateway using CCXT unified API for real exchanges.

    Features:
    - Supports spot, futures, and perpetual contracts
    - Automatic leverage and margin mode setup
    - Symbol format normalization (BTC-USD -> BTC/USD:USD for futures)
    - Proper error handling and partial fill support
    - Fee tracking from exchange responses
    """

    def __init__(
        self,
        exchange_id: str,
        api_key: str = "",
        secret_key: str = "",
        passphrase: Optional[str] = None,
        wallet_address: Optional[str] = None,
        private_key: Optional[str] = None,
        testnet: bool = False,
        default_type: str = "swap",
        margin_mode: str = "cross",
        position_mode: str = "oneway",
        ccxt_options: Optional[Dict] = None,
    ) -> None:
        """Initialize CCXT exchange gateway.

        Args:
            exchange_id: Exchange identifier (e.g., 'binance', 'okx', 'bybit', 'hyperliquid')
            api_key: API key for authentication (not required for Hyperliquid)
            secret_key: Secret key for authentication (not required for Hyperliquid)
            passphrase: Optional passphrase (required for OKX, Coinbase Exchange)
            wallet_address: Wallet address (required for Hyperliquid)
            private_key: Private key (required for Hyperliquid)
            testnet: Whether to use testnet/sandbox mode
            default_type: Default market type ('spot', 'future', 'swap', "margin")
            margin_mode: Default margin mode ('isolated' or 'cross')
            position_mode: Position mode ('oneway' or 'hedged'), default 'oneway'
            ccxt_options: Additional CCXT exchange options
        """
        self.exchange_id = exchange_id.lower()
        self.api_key = (api_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.passphrase = passphrase.strip() if passphrase else None
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.testnet = testnet
        self.default_type = self._coerce_market_type(default_type)
        self.margin_mode = margin_mode
        self.position_mode = position_mode
        self._ccxt_options = ccxt_options or {}

        # Track leverage settings per symbol to avoid redundant calls
        self._leverage_cache: Dict[str, float] = {}
        self._margin_mode_cache: Dict[str, str] = {}

        # Exchange instance (lazy-initialized)
        self._exchange: Optional[ccxt.Exchange] = None
        self._markets_loaded = False

    @staticmethod
    def _coerce_market_type(market_type: str | MarketType) -> str:
        """Normalize market type to a CCXT defaultType string."""
        if isinstance(market_type, MarketType):
            return market_type.value
        raw = str(market_type).strip()
        if raw.startswith("MarketType."):
            return raw.rsplit(".", 1)[-1].lower()
        return raw.lower()

    def _choose_default_type_for_exchange(self) -> str:
        """Return a safe defaultType for the selected exchange.

        - Binance: map 'swap' to 'future' (USDT-M futures)
        - Others: keep configured default_type
        """
        if self.exchange_id == "binance" and self.default_type == "swap":
            return "future"
        return self.default_type

    def _balance_fetch_params(self) -> Optional[Dict[str, str]]:
        """CCXT fetch_balance params for the active market type."""
        mtype = self._choose_default_type_for_exchange()
        if self.exchange_id == "binance":
            if mtype in ("future", "swap"):
                return {"type": "future"}
            if mtype == "spot":
                return {"type": "spot"}
        if self.exchange_id == "okx":
            return {"type": "trading"}
        return None

    async def _get_exchange(
        self,
        *,
        load_markets: bool = True,
        configure_position_mode: bool = True,
    ) -> ccxt.Exchange:
        """Get or create the CCXT exchange instance."""
        if self._exchange is not None:
            if load_markets:
                await self._load_markets(self._exchange)
            return self._exchange

        # Get exchange class by name
        try:
            exchange_class = getattr(ccxt, self.exchange_id)
        except AttributeError:
            raise ValueError(
                f"Exchange '{self.exchange_id}' not supported by CCXT. "
                f"Available: {', '.join(ccxt.exchanges)}"
            )

        exchange_options: Dict = {
            "defaultType": self._choose_default_type_for_exchange(),
            # Avoid wallet SAPI calls unless currencies are explicitly needed
            "fetchCurrencies": self._ccxt_options.get("fetchCurrencies", False),
            **self._ccxt_options,
        }
        if self.exchange_id == "binance":
            # Sync local clock to Binance server (fixes -1021 recvWindow errors)
            exchange_options.setdefault("adjustForTimeDifference", True)
            exchange_options.setdefault("recvWindow", 60_000)

        config: Dict = {
            "enableRateLimit": True,  # Respect rate limits
            "options": exchange_options,
            # Ignore HTTP_PROXY; prefer IPv4 (aiohttp may fail on IPv6 routes).
            "aiohttp_trust_env": False,
        }
        if self.exchange_id == "binance":
            config["family"] = 4

        # Hyperliquid uses wallet-based authentication
        if self.exchange_id == "hyperliquid":
            if self.wallet_address:
                config["walletAddress"] = self.wallet_address
            if self.private_key:
                config["privateKey"] = self.private_key
            # Disable builder fees by default (can be overridden in ccxt_options)
            if "builderFee" not in config["options"]:
                config["options"]["builderFee"] = False
            if "approvedBuilderFee" not in config["options"]:
                config["options"]["approvedBuilderFee"] = False
        else:
            # Standard API key/secret authentication
            config["apiKey"] = self.api_key
            config["secret"] = self.secret_key

            # Add passphrase if provided (required for OKX, Coinbase Exchange)
            if self.passphrase:
                config["password"] = self.passphrase

        # Create exchange instance
        self._exchange = exchange_class(config)

        # Enable sandbox/testnet mode if requested
        if self.testnet:
            self._exchange.set_sandbox_mode(True)

        await self._sync_exchange_clock(self._exchange)

        # Position mode applies to derivatives only (not spot)
        if configure_position_mode:
            mtype = self._choose_default_type_for_exchange()
            if mtype in ("future", "swap"):
                try:
                    if self._exchange.has.get("setPositionMode"):
                        hedged = self.position_mode.lower() in (
                            "hedged",
                            "dual",
                            "hedge",
                        )
                        await self._exchange.set_position_mode(hedged)
                except Exception as e:
                    logger.warning(
                        "Could not set position mode ({mode}) on {exchange}: {err}",
                        mode=self.position_mode,
                        exchange=self.exchange_id,
                        err=e,
                    )

        if load_markets:
            await self._load_markets(self._exchange)

        return self._exchange

    def _normalize_symbol(self, symbol: str, market_type: Optional[str] = None) -> str:
        """Normalize symbol format for CCXT.

        Examples:
            BTC-USD -> BTC/USD (spot)
            BTC-USDT -> BTC/USDT:USDT (USDT futures on colon exchanges)
            ETH-USD -> ETH/USD:USD (USD futures on colon exchanges)

        Args:
            symbol: Symbol in format 'BTC-USD', 'BTC-USDT', etc.
            market_type: Optional market type override ('spot', 'future', 'swap')

        Returns:
            Normalized CCXT symbol
        """
        mtype = market_type or self.default_type

        # Replace dash with slash
        base_symbol = symbol.replace("-", "/")

        # For futures/swap, only append settlement currency for non-Binance exchanges
        if mtype in ("future", "swap") and self.exchange_id not in ("binance",):
            if ":" not in base_symbol:
                parts = base_symbol.split("/")
                if len(parts) == 2:
                    base_symbol = f"{parts[0]}/{parts[1]}:{parts[1]}"

        return base_symbol

    async def _setup_leverage(
        self, symbol: str, leverage: Optional[float], exchange: ccxt.Exchange
    ) -> None:
        """Set leverage for a symbol if needed and supported.

        Args:
            symbol: CCXT normalized symbol
            leverage: Desired leverage (None means 1x)
            exchange: CCXT exchange instance
        """
        if leverage is None:
            leverage = 1.0

        # Check if already set to avoid redundant calls
        if self._leverage_cache.get(symbol) == leverage:
            return

        # Check if exchange supports setting leverage
        if not exchange.has.get("setLeverage"):
            return

        try:
            # Pass marginMode for exchanges that require it (e.g., OKX)
            params = {}
            if self.exchange_id == "okx":
                params["marginMode"] = self.margin_mode  # 'cross' or 'isolated'
            await exchange.set_leverage(int(leverage), symbol, params)
            self._leverage_cache[symbol] = leverage
        except Exception as e:
            # Some exchanges don't support leverage on certain symbols
            # Log but don't fail the trade
            print(f"Warning: Could not set leverage for {symbol}: {e}")

    async def _setup_margin_mode(self, symbol: str, exchange: ccxt.Exchange) -> None:
        """Set margin mode for a symbol if needed and supported.

        Args:
            symbol: CCXT normalized symbol
            exchange: CCXT exchange instance
        """
        # Check if already set
        if self._margin_mode_cache.get(symbol) == self.margin_mode:
            return

        # Check if exchange supports setting margin mode
        if not exchange.has.get("setMarginMode"):
            return

        try:
            await exchange.set_margin_mode(self.margin_mode, symbol)
            self._margin_mode_cache[symbol] = self.margin_mode
        except Exception as e:
            # Log but don't fail
            print(f"Warning: Could not set margin mode for {symbol}: {e}")

    def _sanitize_client_order_id(self, raw_id: str) -> str:
        """Sanitize client order id to satisfy exchange constraints.

        Constraints:
        - Gate.io: max 28 chars, alphanumeric + '.-_'
        - OKX: max 32 chars, alphanumeric only
        - Binance: max 36 chars (typically), alphanumeric + '.-_:'
        - Others: default to 32 chars (MD5 length) for safety

        Strategy:
        1. Filter allowed characters based on exchange rules.
        2. Check length limit.
        3. If too long, use MD5 hash (32 chars) and truncate if necessary.
        """
        if not raw_id:
            return ""

        # 1. Determine allowed characters and max length
        # Default: alphanumeric + basic separators
        allowed_chars = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:"
        )
        max_len = 32

        if self.exchange_id == "gate":
            # Gate.io: max 28 chars, alphanumeric + .-_ (no colon)
            max_len = 28
            allowed_chars = set(
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
            )
        elif self.exchange_id == "okx":
            # OKX: max 32 chars, alphanumeric only
            max_len = 32
            allowed_chars = set(
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            )
        elif self.exchange_id == "binance":
            # Binance: max 36 chars
            max_len = 36
        elif self.exchange_id == "bybit":
            # Bybit: max 36 chars
            max_len = 36

        # Filter characters
        safe = "".join(ch for ch in raw_id if ch in allowed_chars)

        # 2. Check length
        if safe and len(safe) <= max_len:
            return safe

        # 3. Fallback: MD5 hash (32 chars)
        import hashlib

        hashed = hashlib.md5(raw_id.encode()).hexdigest()

        # If hash is still too long (e.g. Gate.io 28), truncate it
        return hashed[:max_len]

    def _normalize_reduce_only_meta(self, meta: Dict) -> Dict:
        """Normalize and apply reduceOnly parameter for exchange compatibility.

        Different exchanges use different parameter names and formats:
        - Gate.io, Bybit: use 'reduce_only' (snake_case)
        - Binance, OKX, Hyperliquid, MEXC, Coinbase, Blockchain: use 'reduceOnly' (camelCase)

        This function:
        1. Extracts reduceOnly value from input (supports both camelCase and snake_case)
        2. Sets appropriate default (False) for the exchange if not explicitly set
        3. Applies exchange-specific parameter name
        4. Ensures consistent boolean format across all exchanges

        Args:
            meta: Dictionary potentially containing reduceOnly parameters

        Returns:
            Dictionary with exchange-specific reduceOnly parameter name and value
        """
        result = dict(meta or {})
        exid = self.exchange_id.lower() if self.exchange_id else ""

        # Extract any existing reduceOnly value (supports both formats for flexibility)
        reduce_only_value = result.pop("reduceOnly", None)
        if reduce_only_value is None:
            reduce_only_value = result.pop("reduce_only", None)

        # Determine which parameter name to use based on exchange
        if exid in ("gate", "bybit"):
            param_name = "reduce_only"
        else:
            # All other exchanges (binance, okx, hyperliquid, mexc, coinbaseexchange, blockchaincom, etc.)
            param_name = "reduceOnly"

        # Set default to False if not explicitly provided by caller
        if reduce_only_value is None:
            result[param_name] = False
        else:
            result[param_name] = bool(reduce_only_value)

        return result

    def _build_order_params(self, inst: TradeInstruction, order_type: str) -> Dict:
        """Build exchange-specific order params with safe defaults.

        - Attach clientOrderId for idempotency where supported
        - Provide default time-in-force for limit orders
        - Provide reduceOnly defaults for derivatives
        - Provide tdMode for OKX if not specified
        """
        params: Dict = self._normalize_reduce_only_meta(inst.meta or {})

        exid = self.exchange_id

        # Idempotency / client order id
        # Hyperliquid doesn't support clientOrderId
        if exid != "hyperliquid":
            raw_client_id = params.get("clientOrderId", inst.instruction_id)
            if raw_client_id:
                client_id = self._sanitize_client_order_id(raw_client_id)
                params["clientOrderId"] = client_id

        # Default tdMode for OKX on all orders
        if exid == "okx":
            params.setdefault(
                "tdMode", "isolated" if self.margin_mode == "isolated" else "cross"
            )

        # Default time-in-force for limit orders
        if order_type == "limit":
            if exid == "binance":
                params.setdefault("timeInForce", "GTC")
            elif exid == "bybit":
                params.setdefault("time_in_force", "GoodTillCancel")

        # Enforce single-sided mode: strip positionSide/posSide if present
        try:
            mode = (self.position_mode or "oneway").lower()
            if mode in ("oneway", "single", "net"):
                removed = []
                if "positionSide" in params:
                    params.pop("positionSide", None)
                    removed.append("positionSide")
                if "posSide" in params:
                    params.pop("posSide", None)
                    removed.append("posSide")
                if removed:
                    logger.debug(
                        f"🧹 Oneway mode: stripped {removed} from order params"
                    )
        except Exception:
            pass

        return params

    async def _check_minimums(
        self,
        exchange: ccxt.Exchange,
        symbol: str,
        amount: float,
        price: Optional[float],
    ) -> Optional[str]:
        markets = getattr(exchange, "markets", {}) or {}
        market = markets.get(symbol, {})
        limits = market.get("limits") or {}

        # amount minimum
        min_amount = None
        amt_limits = limits.get("amount") or {}
        if amt_limits.get("min") is not None:
            try:
                min_amount = float(amt_limits["min"])
            except Exception:
                min_amount = None
        if min_amount is None:
            info = market.get("info") or {}
            min_sz = info.get("minSz")
            if min_sz is not None:
                try:
                    min_amount = float(min_sz)
                except Exception:
                    min_amount = None
        if min_amount is not None and amount < min_amount:
            return f"amount<{min_amount}"

        # notional minimum
        min_cost = None
        cost_limits = limits.get("cost") or {}
        if cost_limits.get("min") is not None:
            try:
                min_cost = float(cost_limits["min"])
            except Exception:
                min_cost = None
        if min_cost is not None:
            est_price = price
            if est_price is None and exchange.has.get("fetchTicker"):
                try:
                    ticker = await exchange.fetch_ticker(symbol)
                    est_price = float(
                        ticker.get("last")
                        or ticker.get("bid")
                        or ticker.get("ask")
                        or 0.0
                    )
                except Exception:
                    est_price = None
            if est_price and est_price > 0:
                notional = amount * est_price
                if notional < min_cost:
                    return f"notional<{min_cost}"
        return None

    async def _estimate_required_margin_okx(
        self,
        symbol: str,
        amount: float,
        price: Optional[float],
        leverage: Optional[float],
        exchange: ccxt.Exchange,
    ) -> Optional[float]:
        """Estimate initial margin required for an OKX derivatives open.

        If `symbol` is a derivatives contract and `amount` is in contracts (sz),
        multiply by the contract size (`contractSize` or `info.ctVal`) to convert
        to notional units before dividing by leverage.
        Falls back to ticker price when `price` is not provided.
        """
        try:
            lev = float(leverage or 1.0)
            if lev <= 0:
                lev = 1.0
            px = float(price or 0.0)
            if px <= 0:
                if exchange.has.get("fetchTicker"):
                    try:
                        ticker = await exchange.fetch_ticker(symbol)
                        px = float(
                            ticker.get("last")
                            or ticker.get("bid")
                            or ticker.get("ask")
                            or 0.0
                        )
                    except Exception:
                        px = 0.0
            if px <= 0:
                return None

            # Detect contract size if symbol is derivatives (OKX swap/futures)
            ct_val: Optional[float] = None
            try:
                market = (getattr(exchange, "markets", {}) or {}).get(symbol) or {}
                if market.get("contract"):
                    try:
                        ct_val = float(market.get("contractSize") or 0.0)
                    except Exception:
                        ct_val = None
                    if not ct_val:
                        info = market.get("info") or {}
                        try:
                            ct_val = float(info.get("ctVal") or 0.0)
                        except Exception:
                            ct_val = None
            except Exception:
                ct_val = None

            # If ct_val is present and amount is sz (contracts), convert to notional
            if ct_val and ct_val > 0:
                notional = amount * ct_val * px
            else:
                # Fallback: treat amount as base units
                notional = amount * px

            return notional / lev * 1.02
        except Exception:
            return None

    async def _get_free_usdt_okx(self, exchange: ccxt.Exchange) -> Optional[float]:
        """Read available USDT from OKX unified trading account.

        Explicitly queries trading balances and extracts free USDT.
        """
        try:
            bal = await exchange.fetch_balance({"type": "trading"})
            free = bal.get("free") or {}
            usdt = free.get("USDT")
            if usdt is None:
                # Fallback: some ccxt versions expose totals differently
                usdt = (bal.get("total") or {}).get("USDT")
            return float(usdt) if usdt is not None else 0.0
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch OKX trading balance: {e}")
            return None

    async def _estimate_required_margin_binance_linear(
        self,
        symbol: str,
        amount: float,
        price: Optional[float],
        leverage: Optional[float],
        exchange: ccxt.Exchange,
    ) -> Optional[float]:
        """Estimate initial margin for Binance USDT-M linear contracts.

        For USDT-M (linear), `amount` is base coin quantity.
        Approximation: notional = amount * price; initial_margin = notional / leverage.
        Adds a 2% buffer. If no price is provided, falls back to ticker last/bid/ask.
        """
        try:
            lev = float(leverage or 1.0)
            if lev <= 0:
                lev = 1.0
            px = float(price or 0.0)
            if px <= 0:
                if exchange.has.get("fetchTicker"):
                    try:
                        ticker = await exchange.fetch_ticker(symbol)
                        px = float(
                            ticker.get("last")
                            or ticker.get("bid")
                            or ticker.get("ask")
                            or 0.0
                        )
                    except Exception:
                        px = 0.0
            if px <= 0:
                return None
            notional = amount * px
            return notional / lev * 1.02
        except Exception:
            return None

    async def _get_free_usdt_binance(self, exchange: ccxt.Exchange) -> Optional[float]:
        """Fetch available USDT balance from Binance USDT-M futures account."""
        try:
            bal = await exchange.fetch_balance({"type": "future"})
            free = bal.get("free") or {}
            usdt = free.get("USDT")
            if usdt is None:
                usdt = (bal.get("total") or {}).get("USDT")
            return float(usdt) if usdt is not None else 0.0
        except Exception as e:
            logger.warning(f"Could not fetch Binance futures balance: {e}")
            return None

    def _extract_fee_from_order(
        self, order: Dict, symbol: str, filled_qty: float, avg_price: float
    ) -> float:
        """Extract fee cost from order response with exchange-specific fallbacks.

        Supports multiple exchange fee structures:
        - Standard CCXT unified 'fee' field
        - Binance 'fills' array with commission details
        - OKX 'info.fee' field
        - Bybit 'info.cumExecFee' field
        - Other exchange-specific formats

        Args:
            order: Order response from exchange
            symbol: Trading symbol
            filled_qty: Filled quantity
            avg_price: Average execution price

        Returns:
            Fee cost in quote currency (USDT/USD), or 0.0 if not available
        """
        fee_cost = 0.0

        try:
            # Method 1: Standard CCXT unified fee field
            if "fee" in order and order["fee"]:
                fee_info = order["fee"]
                cost = fee_info.get("cost")
                if cost is not None and cost > 0:
                    fee_cost = float(cost)
                    logger.debug(f"  💰 Fee from CCXT unified field: {fee_cost}")
                    return fee_cost

            # Method 2: Exchange-specific extraction from 'info' field
            info = order.get("info", {})

            if self.exchange_id == "binance":
                # Binance: Extract from 'fills' array
                fills = info.get("fills", [])
                if fills:
                    for fill in fills:
                        commission = float(fill.get("commission", 0.0))
                        commission_asset = fill.get("commissionAsset", "")

                        # If fee is in quote currency (USDT/BUSD/USD), add directly
                        if commission_asset in ("USDT", "BUSD", "USD", "USDC"):
                            fee_cost += commission
                        # If fee is in BNB or other asset, log it but don't convert
                        elif commission > 0:
                            logger.info(
                                f"  💰 Fee paid in {commission_asset}: {commission}"
                            )
                            # Could implement conversion logic here if needed

                    if fee_cost > 0:
                        logger.debug(f"  💰 Fee from Binance fills: {fee_cost}")
                        return fee_cost

            elif self.exchange_id == "okx":
                # OKX: fee is in 'info.fee' or 'info.fillFee'
                fee_str = info.get("fee") or info.get("fillFee")
                if fee_str:
                    fee_cost = abs(float(fee_str))  # OKX returns negative fee
                    logger.debug(f"  💰 Fee from OKX info: {fee_cost}")
                    return fee_cost

            elif self.exchange_id == "bybit":
                # Bybit: cumExecFee or execFee
                cum_fee = info.get("cumExecFee") or info.get("execFee")
                if cum_fee:
                    fee_cost = float(cum_fee)
                    logger.debug(f"  💰 Fee from Bybit info: {fee_cost}")
                    return fee_cost

            elif self.exchange_id in ("gate", "gateio"):
                # Gate.io: fee field in info
                fee_str = info.get("fee")
                if fee_str:
                    fee_cost = float(fee_str)
                    logger.debug(f"  💰 Fee from Gate.io info: {fee_cost}")
                    return fee_cost

            elif self.exchange_id == "kucoin":
                # KuCoin: fee in info
                fee_str = info.get("fee")
                if fee_str:
                    fee_cost = float(fee_str)
                    logger.debug(f"  💰 Fee from KuCoin info: {fee_cost}")
                    return fee_cost

            elif self.exchange_id == "mexc":
                # MEXC: commission in fills
                fills = info.get("fills", [])
                if fills:
                    for fill in fills:
                        commission = float(fill.get("commission", 0.0))
                        fee_cost += commission

                    if fee_cost > 0:
                        logger.debug(f"  💰 Fee from MEXC fills: {fee_cost}")
                        return fee_cost

            elif self.exchange_id == "bitget":
                # Bitget: fee in info.feeDetail
                fee_detail = info.get("feeDetail", {})
                if fee_detail:
                    total_fee = float(fee_detail.get("totalFee", 0.0))
                    if total_fee > 0:
                        fee_cost = total_fee
                        logger.debug(f"  💰 Fee from Bitget info: {fee_cost}")
                        return fee_cost

            elif self.exchange_id == "hyperliquid":
                # Hyperliquid: typically no fee field, might need special handling
                # Check if there's a fee in info
                fee_str = info.get("fee")
                if fee_str:
                    fee_cost = float(fee_str)
                    logger.debug(f"  💰 Fee from Hyperliquid info: {fee_cost}")
                    return fee_cost

            # Method 3: Estimate from trading fee rate if available (last resort)
            if fee_cost == 0.0 and filled_qty > 0 and avg_price > 0:
                # Don't estimate, just log that fee wasn't found
                logger.debug(
                    f"  💰 No fee information found for {symbol} on {self.exchange_id}"
                )

        except Exception as e:
            logger.warning(f"  ⚠️ Error extracting fee for {symbol}: {e}")

        return fee_cost

    async def execute(
        self,
        instructions: List[TradeInstruction],
        market_features: Optional[List[FeatureVector]] = None,
    ) -> List[TxResult]:
        """Execute trade instructions on the real exchange via CCXT.

        Args:
            instructions: List of trade instructions to execute
            market_features: Optional market features (not used for real execution)

        Returns:
            List of transaction results with fill details
        """
        if not instructions:
            logger.warning("⚠️ CCXTExecutionGateway: No instructions to execute")
            return []

        logger.info(
            f"💰 CCXTExecutionGateway: Executing {len(instructions)} instructions"
        )
        exchange = await self._get_exchange()
        results: List[TxResult] = []

        for inst in instructions:
            side = (
                getattr(inst, "side", None)
                or derive_side_from_action(getattr(inst, "action", None))
                or TradeSide.BUY
            )
            logger.info(
                f"  📤 Processing {inst.instrument.symbol} {side.value} qty={inst.quantity}"
            )
            try:
                result = await self._execute_single(inst, exchange)
                results.append(result)
            except Exception as e:
                # Create error result for failed instruction
                results.append(
                    TxResult(
                        instruction_id=inst.instruction_id,
                        instrument=inst.instrument,
                        side=side,
                        requested_qty=float(inst.quantity),
                        filled_qty=0.0,
                        status=TxStatus.ERROR,
                        reason=str(e),
                        meta=inst.meta,
                    )
                )

        return results

    async def _execute_single(
        self, inst: TradeInstruction, exchange: ccxt.Exchange
    ) -> TxResult:
        """Execute a single trade instruction.

        Args:
            inst: Trade instruction to execute
            exchange: CCXT exchange instance

        Returns:
            Transaction result with execution details
        """
        # Dispatch by high-level action if provided (prefer structured field)
        action = (inst.action.value if getattr(inst, "action", None) else None) or str(
            (inst.meta or {}).get("action") or ""
        ).lower()
        if action == "open_long":
            return await self._exec_open_long(inst, exchange)
        if action == "open_short":
            return await self._exec_open_short(inst, exchange)
        if action == "close_long":
            return await self._exec_close_long(inst, exchange)
        if action == "close_short":
            return await self._exec_close_short(inst, exchange)
        if action == "noop":
            return await self._exec_noop(inst)

        # Fallback to generic submission
        return await self._submit_order(inst, exchange)

    def _apply_exchange_specific_precision(
        self, symbol: str, amount: float, price: float | None, exchange: ccxt.Exchange
    ) -> tuple[float, float | None]:
        """Apply exchange-specific precision rules.

        Especially important for Hyperliquid (integers for some, decimals for others)
        and handling min/max constraints robustly.
        """
        try:
            # 1. Standard CCXT precision
            # Some exchanges raise errors if amount < precision (e.g. Binance)
            # We catch this and return 0.0 to signal invalid amount
            try:
                amount = float(exchange.amount_to_precision(symbol, amount))
            except Exception as e:
                # Catch generic errors from amount_to_precision, including precision violations
                # Log warning but return 0.0 to allow clean skipping downstream
                logger.warning(
                    f"  ⚠️ Amount {amount} failed precision check for {symbol}: {e}"
                )
                amount = 0.0

            if price is not None:
                price = float(exchange.price_to_precision(symbol, price))

            # 2. Hyperliquid specific handling
            if self.exchange_id == "hyperliquid":
                market = (getattr(exchange, "markets", {}) or {}).get(symbol) or {}
                price_precision = market.get("precision", {}).get("price")

                # If precision is 1.0 (integer only), force integer price
                if price is not None and price_precision == 1.0:
                    price = float(int(price))
                    logger.debug(
                        f"  🔢 Hyperliquid: Rounded price to integer {price} for {symbol}"
                    )

            return amount, price

        except Exception as e:
            logger.warning(f"  ⚠️ Precision application failed for {symbol}: {e}")
            # Return original values on error, but for 'amount too small' cases this might
            # just lead to downstream rejection. If we couldn't fix it here, we let it flow.
            return amount, price

    async def _submit_order(
        self,
        inst: TradeInstruction,
        exchange: ccxt.Exchange,
        params_override: Optional[Dict] = None,
    ) -> TxResult:
        # Normalize symbol for CCXT
        symbol = self._normalize_symbol(inst.instrument.symbol)

        # Resolve symbol against loaded markets with simple fallbacks
        markets = getattr(exchange, "markets", {}) or {}
        if symbol not in markets:
            # Try alternate format without/with colon
            if ":" in symbol:
                alt = symbol.split(":")[0]
                if alt in markets:
                    symbol = alt
            else:
                parts = symbol.split("/")
                if len(parts) == 2:
                    base, quote = parts
                    alt = f"{base}/{quote}:{quote}"
                    if alt in markets:
                        symbol = alt
                    else:
                        # Try USD<->USDT swap
                        if quote in ("USD", "USDT"):
                            alt_quote = "USDT" if quote == "USD" else "USD"
                            alt2 = f"{base}/{alt_quote}"
                            alt3 = f"{base}/{alt_quote}:{alt_quote}"
                            if alt2 in markets:
                                symbol = alt2
                            elif alt3 in markets:
                                symbol = alt3

        # Setup leverage and margin mode only for opening positions
        # For closing positions (reduceOnly), skip these as they are not needed
        action = (inst.action.value if getattr(inst, "action", None) else None) or str(
            (inst.meta or {}).get("action") or ""
        ).lower()
        is_opening = action in ("open_long", "open_short")

        if is_opening:
            await self._setup_leverage(symbol, inst.leverage, exchange)
            await self._setup_margin_mode(symbol, exchange)

        # Map instruction to CCXT parameters
        local_side = (
            getattr(inst, "side", None)
            or derive_side_from_action(getattr(inst, "action", None))
            or TradeSide.BUY
        )
        side = "buy" if local_side == TradeSide.BUY else "sell"
        order_type = "limit" if inst.price_mode == PriceMode.LIMIT else "market"
        amount = float(inst.quantity)
        price = float(inst.limit_price) if inst.limit_price else None

        # For OKX derivatives, amount must be in contracts; convert from base units if needed
        ct_val = None
        try:
            market = (getattr(exchange, "markets", {}) or {}).get(symbol) or {}
            if self.exchange_id == "okx" and market.get("contract"):
                try:
                    ct_val = float(market.get("contractSize") or 0.0)
                except Exception:
                    ct_val = None
                if not ct_val:
                    info = market.get("info") or {}
                    try:
                        ct_val = float(info.get("ctVal") or 0.0)
                    except Exception:
                        ct_val = None
                if ct_val and ct_val > 0:
                    amount = amount / ct_val
        except Exception:
            pass

        # Apply precision
        amount, price = self._apply_exchange_specific_precision(
            symbol, amount, price, exchange
        )

        # If amount became zero after precision/rounding (e.g. < min precision), skip order
        if amount <= 0:
            return TxResult(
                instruction_id=inst.instruction_id,
                instrument=inst.instrument,
                side=local_side,
                requested_qty=float(inst.quantity),
                filled_qty=0.0,
                status=TxStatus.REJECTED,
                reason="amount_too_small_for_precision",
                meta=inst.meta,
            )

        # Reject orders below exchange minimums (do not lift to min)
        try:
            reject_reason = await self._check_minimums(exchange, symbol, amount, price)
        except Exception as e:
            logger.warning(f"⚠️ Minimum check failed for {symbol}: {e}")
            reject_reason = f"minimum_check_failed:{e}"
        if reject_reason is not None:
            logger.warning(f"  🚫 Skipping order due to {reject_reason}")
            return TxResult(
                instruction_id=inst.instruction_id,
                instrument=inst.instrument,
                side=local_side,
                requested_qty=float(inst.quantity),
                filled_qty=0.0,
                status=TxStatus.REJECTED,
                reason=reject_reason,
                meta=inst.meta,
            )

        # OKX trading account margin precheck for open orders
        if self.exchange_id == "okx":
            try:
                # Determine open vs close intent from default reduceOnly flags
                provisional = self._build_order_params(inst, order_type)
                is_close = bool(
                    provisional.get("reduceOnly") or provisional.get("reduce_only")
                )
                if not is_close:
                    required = await self._estimate_required_margin_okx(
                        symbol, amount, price, inst.leverage, exchange
                    )
                    free_usdt = await self._get_free_usdt_okx(exchange)
                    if (
                        required is not None
                        and free_usdt is not None
                        and free_usdt < required
                    ):
                        reject_reason = f"insufficient_margin:need~{required:.6f}USDT,free~{free_usdt:.6f}USDT"
                        logger.warning(f"  🚫 Skipping order due to {reject_reason}")
                        return TxResult(
                            instruction_id=inst.instruction_id,
                            instrument=inst.instrument,
                            side=local_side,
                            requested_qty=float(inst.quantity),
                            filled_qty=0.0,
                            status=TxStatus.REJECTED,
                            reason=reject_reason,
                            meta=inst.meta,
                        )
            except Exception as e:
                logger.warning(
                    f"⚠️ OKX margin precheck failed, proceeding without precheck: {e}"
                )

        # Binance USDT-M linear futures margin precheck for open orders
        if self.exchange_id == "binance":
            try:
                provisional = self._build_order_params(inst, order_type)
                is_close = bool(
                    provisional.get("reduceOnly") or provisional.get("reduce_only")
                )
                if not is_close:
                    market = (getattr(exchange, "markets", {}) or {}).get(symbol) or {}
                    is_contract = bool(market.get("contract"))
                    is_linear = bool(market.get("linear"))
                    if not is_linear:
                        settle = str(market.get("settle") or "").upper()
                        is_linear = bool(is_contract and settle == "USDT")
                    if is_contract and is_linear:
                        required = await self._estimate_required_margin_binance_linear(
                            symbol, amount, price, inst.leverage, exchange
                        )
                        free_usdt = await self._get_free_usdt_binance(exchange)
                        if (
                            required is not None
                            and free_usdt is not None
                            and free_usdt < required
                        ):
                            reject_reason = f"insufficient_margin_binance_usdtm:need~{required:.6f}USDT,free~{free_usdt:.6f}USDT"
                            logger.warning(
                                f"  🚫 Skipping order due to {reject_reason}"
                            )
                            return TxResult(
                                instruction_id=inst.instruction_id,
                                instrument=inst.instrument,
                                side=local_side,
                                requested_qty=float(inst.quantity),
                                filled_qty=0.0,
                                status=TxStatus.REJECTED,
                                reason=reject_reason,
                                meta=inst.meta,
                            )
            except Exception as e:
                logger.warning(
                    f"⚠️ Binance USDT-M margin precheck failed, proceeding without precheck: {e}"
                )

        # Build order params with exchange-specific defaults
        params = self._build_order_params(inst, order_type)
        if params_override:
            try:
                params.update(params_override)
            except Exception:
                pass

        # Enforce single-sided mode again after overrides
        try:
            mode = (self.position_mode or "oneway").lower()
            if mode in ("oneway", "single", "net"):
                removed = []
                if "positionSide" in params:
                    params.pop("positionSide", None)
                    removed.append("positionSide")
                if "posSide" in params:
                    params.pop("posSide", None)
                    removed.append("posSide")
                if removed:
                    logger.debug(
                        f"🧹 Oneway mode (post-override): stripped {removed} from order params"
                    )
        except Exception:
            pass

        # Hyperliquid special handling for market orders
        # Hyperliquid doesn't have true market orders; use IoC (Immediate or Cancel) to simulate
        if self.exchange_id == "hyperliquid" and order_type == "market":
            try:
                logger.debug(
                    "  📊 Hyperliquid: Converting market order to IoC limit order"
                )

                # Fetch current market price
                if price is None:
                    ticker = await exchange.fetch_ticker(symbol)
                    price = float(ticker.get("last") or ticker.get("close") or 0.0)

                if price > 0:
                    # Calculate slippage price based on direction
                    slippage_pct = (
                        inst.max_slippage_bps or 50.0
                    ) / 10000.0  # default 50 bps = 0.5%
                    if side == "buy":
                        # For buy orders, set price higher to ensure execution
                        price = price * (1 + slippage_pct)
                    else:
                        # For sell orders, set price lower to ensure execution
                        price = price * (1 - slippage_pct)

                    # Apply precision again on the simulated price
                    _, price = self._apply_exchange_specific_precision(
                        symbol, amount, price, exchange
                    )

                    # Use IoC (Immediate or Cancel) to simulate market execution
                    params["timeInForce"] = "Ioc"
                    logger.debug(
                        f"  💰 Using IoC limit order: {side} @ {price} (slippage: {slippage_pct:.2%})"
                    )
                else:
                    logger.warning(
                        f"  ⚠️ Could not determine market price for {symbol}, will try without price"
                    )
            except Exception as e:
                logger.warning(f"  ⚠️ Could not setup Hyperliquid market order: {e}")
                # Fallback: let exchange handle it

        # Create order
        logger.info(
            "  Creating {order_type} order: {side} {amount} {symbol} @ {price}",
            order_type=order_type,
            side=side,
            amount=amount,
            symbol=symbol,
            price=price if price else "market",
        )
        logger.debug("  Order params: {params}", params=params)
        order: Optional[Dict] = None
        try:
            order = await exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                params=params,
            )
        except Exception as ccxt_err:
            if self.exchange_id != "binance":
                error_msg = str(ccxt_err)
                logger.error(
                    "  ERROR creating order for {symbol}: {err}",
                    symbol=symbol,
                    err=error_msg,
                )
                return TxResult(
                    instruction_id=inst.instruction_id,
                    instrument=inst.instrument,
                    side=local_side,
                    requested_qty=amount,
                    filled_qty=0.0,
                    status=TxStatus.ERROR,
                    reason=f"create_order_failed: {error_msg}",
                    meta=inst.meta,
                )
            logger.debug(
                "CCXT create_order failed for {exchange}, using urllib fallback: {err}",
                exchange=self.exchange_id,
                err=self._format_exchange_error(ccxt_err),
            )
            try:
                order = await self._create_binance_order_urllib(
                    symbol, side, order_type, amount, price, params, exchange
                )
            except Exception as urllib_err:
                error_msg = self._format_exchange_error(urllib_err)
                hint = self._binance_error_hint(urllib_err)
                if hint:
                    error_msg = f"{error_msg}.{hint}"
                logger.error(
                    "  ERROR creating order for {symbol} (urllib): {err}",
                    symbol=symbol,
                    err=error_msg,
                )
                return TxResult(
                    instruction_id=inst.instruction_id,
                    instrument=inst.instrument,
                    side=local_side,
                    requested_qty=amount,
                    filled_qty=0.0,
                    status=TxStatus.ERROR,
                    reason=f"create_order_failed: {error_msg}",
                    meta=inst.meta,
                )

        logger.info(
            "  Order created: id={order_id}, status={status}, filled={filled}",
            order_id=order.get("id"),
            status=order.get("status"),
            filled=order.get("filled"),
        )

        # For market orders, wait for fill and fetch final order status
        if order_type == "market":
            order_id = order.get("id")
            if order_id:
                try:
                    logger.info(
                        "  Waiting 0.5s for market order {order_id} to fill...",
                        order_id=order_id,
                    )
                    await asyncio.sleep(0.5)
                    if self.exchange_id == "binance":
                        order = await self._fetch_binance_order_urllib(
                            symbol, str(order_id), exchange
                        )
                    elif exchange.has.get("fetchOrder"):
                        order = await exchange.fetch_order(order_id, symbol)
                    logger.info(
                        "  Order status after fetch: filled={filled}, average={avg}, status={status}",
                        filled=order.get("filled"),
                        avg=order.get("average"),
                        status=order.get("status"),
                    )
                except Exception as e:
                    logger.warning(
                        "  Could not fetch order status for {symbol}: {err}",
                        symbol=symbol,
                        err=e,
                    )

        # Parse order response
        filled_qty = float(order.get("filled", 0.0))

        # For OKX derivatives, filled quantity is in contracts; convert back to base units
        if self.exchange_id == "okx" and ct_val and ct_val > 0 and filled_qty > 0:
            filled_qty = filled_qty * ct_val

        avg_price = float(order.get("average") or 0.0)
        fee_cost = 0.0

        logger.info(
            f"  📊 Final parsed: filled_qty={filled_qty}, avg_price={avg_price}"
        )

        fee_cost = self._extract_fee_from_order(order, symbol, filled_qty, avg_price)

        # Calculate slippage if applicable
        slippage_bps = None
        if avg_price and inst.limit_price and inst.price_mode == PriceMode.LIMIT:
            expected = float(inst.limit_price)
            slippage = abs(avg_price - expected) / expected * 10000.0
            slippage_bps = slippage

        # Determine status
        status = TxStatus.FILLED
        if filled_qty < amount * 0.99:  # Allow 1% tolerance
            status = TxStatus.PARTIAL
        if filled_qty == 0:
            status = TxStatus.REJECTED

        return TxResult(
            instruction_id=inst.instruction_id,
            instrument=inst.instrument,
            side=local_side,
            requested_qty=amount,
            filled_qty=filled_qty,
            avg_exec_price=avg_price if avg_price > 0 else None,
            slippage_bps=slippage_bps,
            fee_cost=fee_cost if fee_cost > 0 else None,
            leverage=inst.leverage,
            status=status,
            reason=order.get("status") if status != TxStatus.FILLED else None,
            meta=inst.meta,
        )

    async def _exec_open_long(
        self, inst: TradeInstruction, exchange: ccxt.Exchange
    ) -> TxResult:
        # Ensure we do not mark reduceOnly on open
        # Use exchange-specific param name
        if self.exchange_id == "bybit":
            overrides = {"reduce_only": False}
        else:
            overrides = {"reduceOnly": False}
        return await self._submit_order(inst, exchange, overrides)

    async def _exec_open_short(
        self, inst: TradeInstruction, exchange: ccxt.Exchange
    ) -> TxResult:
        # Use exchange-specific param name
        if self.exchange_id == "bybit":
            overrides = {"reduce_only": False}
        else:
            overrides = {"reduceOnly": False}
        return await self._submit_order(inst, exchange, overrides)

    async def _exec_close_long(
        self, inst: TradeInstruction, exchange: ccxt.Exchange
    ) -> TxResult:
        # Force reduceOnly flags for closes
        # Use exchange-specific param name
        if self.exchange_id == "bybit":
            overrides = {"reduce_only": True}
        else:
            overrides = {"reduceOnly": True}
        return await self._submit_order(inst, exchange, overrides)

    async def _exec_close_short(
        self, inst: TradeInstruction, exchange: ccxt.Exchange
    ) -> TxResult:
        # Use exchange-specific param name
        if self.exchange_id == "bybit":
            overrides = {"reduce_only": True}
        else:
            overrides = {"reduceOnly": True}
        return await self._submit_order(inst, exchange, overrides)

    async def _exec_noop(self, inst: TradeInstruction) -> TxResult:
        # No-op: return a rejected result with reason
        side = (
            getattr(inst, "side", None)
            or derive_side_from_action(getattr(inst, "action", None))
            or TradeSide.BUY
        )
        return TxResult(
            instruction_id=inst.instruction_id,
            instrument=inst.instrument,
            side=side,
            requested_qty=float(inst.quantity),
            filled_qty=0.0,
            status=TxStatus.REJECTED,
            reason="noop",
            meta=inst.meta,
        )

    def _binance_timestamp_ms(self, exchange: Optional[ccxt.Exchange] = None) -> int:
        """Binance request timestamp aligned with server clock when possible."""
        offset = 0
        if exchange is not None:
            offset = int(exchange.options.get("timeDifference") or 0)
        return int(time.time() * 1000) - offset

    @staticmethod
    def _format_exchange_error(exc: Exception) -> str:
        """Include Binance/CCXT response details when available."""
        parts = [str(exc).strip()]
        for attr in ("message", "body", "code"):
            val = getattr(exc, attr, None)
            if val is None:
                continue
            text = str(val).strip()
            if text and text not in parts[0]:
                parts.append(text)
        return " | ".join(parts)

    async def _binance_signed_get(
        self,
        host: str,
        path: str,
        exchange: Optional[ccxt.Exchange] = None,
        query_params: Optional[Dict] = None,
    ) -> dict | list:
        """Signed Binance GET via urllib (bypasses broken ccxt/aiohttp paths)."""

        def _call() -> dict | list:
            ts = self._binance_timestamp_ms(exchange)
            params: Dict = {"timestamp": ts, "recvWindow": 60_000}
            if query_params:
                params.update(query_params)
            query = urllib.parse.urlencode(params)
            signature = hmac.new(
                self.secret_key.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            url = f"{host}{path}?{query}&signature={signature}"
            req = urllib.request.Request(
                url,
                headers={
                    "X-MBX-APIKEY": self.api_key,
                    "User-Agent": "valuecell/1.0",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as http_err:
                raw = http_err.read().decode(errors="replace")
                raise RuntimeError(
                    f"HTTP {http_err.code} {path}: {raw}"
                ) from http_err

        return await asyncio.to_thread(_call)

    async def _binance_signed_post(
        self,
        host: str,
        path: str,
        payload: Dict,
        exchange: Optional[ccxt.Exchange] = None,
    ) -> dict:
        """Signed Binance POST via urllib (bypasses broken ccxt/aiohttp paths)."""

        def _call() -> dict:
            ts = self._binance_timestamp_ms(exchange)
            body_params: Dict[str, str | int | float] = {
                **{k: v for k, v in payload.items() if v is not None},
                "timestamp": ts,
                "recvWindow": 60_000,
            }
            query = urllib.parse.urlencode(
                [(k, str(v)) for k, v in body_params.items()]
            )
            signature = hmac.new(
                self.secret_key.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            body = f"{query}&signature={signature}"
            url = f"{host}{path}"
            req = urllib.request.Request(
                url,
                data=body.encode("utf-8"),
                headers={
                    "X-MBX-APIKEY": self.api_key,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "valuecell/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read().decode())
                    if not isinstance(result, dict):
                        raise RuntimeError(f"Unexpected POST response for {path}")
                    return result
            except urllib.error.HTTPError as http_err:
                raw = http_err.read().decode(errors="replace")
                raise RuntimeError(
                    f"HTTP {http_err.code} {path}: {raw}"
                ) from http_err

        return await asyncio.to_thread(_call)

    @staticmethod
    def _format_binance_decimal(value: float) -> str:
        text = f"{value:.8f}".rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _binance_raw_order_to_ccxt(raw: dict, symbol: str) -> Dict:
        """Map Binance order JSON to a minimal CCXT-like order dict."""
        status_map = {
            "NEW": "open",
            "PARTIALLY_FILLED": "open",
            "FILLED": "closed",
            "CANCELED": "canceled",
            "REJECTED": "rejected",
            "EXPIRED": "expired",
        }
        filled = float(raw.get("executedQty") or raw.get("cumQty") or 0)
        avg = float(raw.get("avgPrice") or 0)
        if avg <= 0 and filled > 0:
            cum_quote = float(
                raw.get("cumQuote")
                or raw.get("cummulativeQuoteQty")
                or raw.get("cumQuoteQty")
                or 0
            )
            if cum_quote > 0:
                avg = cum_quote / filled
        return {
            "id": str(raw.get("orderId", "")),
            "clientOrderId": raw.get("clientOrderId"),
            "symbol": symbol,
            "status": status_map.get(str(raw.get("status", "")), "open"),
            "filled": filled,
            "amount": float(raw.get("origQty") or 0),
            "average": avg,
            "price": float(raw.get("price") or 0),
            "info": raw,
        }

    async def _create_binance_order_urllib(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: Optional[float],
        params: Dict,
        exchange: Optional[ccxt.Exchange] = None,
    ) -> Dict:
        """Place a Binance order via urllib when ccxt/aiohttp cannot connect."""
        from valuecell.agents.common.trading.data import binance_http

        mtype = self._choose_default_type_for_exchange()
        sym_id = binance_http.symbol_to_binance_id(symbol)
        payload: Dict = {
            "symbol": sym_id,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": self._format_binance_decimal(amount),
        }
        if order_type == "limit" and price is not None:
            payload["price"] = self._format_binance_decimal(price)
            payload["timeInForce"] = str(params.get("timeInForce", "GTC"))
        reduce_only = params.get("reduceOnly")
        if reduce_only is None:
            reduce_only = params.get("reduce_only")
        if reduce_only:
            payload["reduceOnly"] = "true"
        client_id = params.get("clientOrderId") or params.get("newClientOrderId")
        if client_id:
            payload["newClientOrderId"] = str(client_id)

        if mtype in ("future", "swap"):
            host = "https://fapi.binance.com"
            path = "/fapi/v1/order"
        else:
            host = "https://api.binance.com"
            path = "/api/v3/order"

        raw = await self._binance_signed_post(host, path, payload, exchange)
        return self._binance_raw_order_to_ccxt(raw, symbol)

    async def _fetch_binance_order_urllib(
        self,
        symbol: str,
        order_id: str,
        exchange: Optional[ccxt.Exchange] = None,
    ) -> Dict:
        """Fetch order status via urllib."""
        from valuecell.agents.common.trading.data import binance_http

        mtype = self._choose_default_type_for_exchange()
        sym_id = binance_http.symbol_to_binance_id(symbol)
        if mtype in ("future", "swap"):
            host = "https://fapi.binance.com"
            path = "/fapi/v1/order"
        else:
            host = "https://api.binance.com"
            path = "/api/v3/order"
        raw = await self._binance_signed_get(
            host,
            path,
            exchange,
            {"symbol": sym_id, "orderId": order_id},
        )
        if not isinstance(raw, dict):
            raise RuntimeError("Unexpected Binance order query response")
        return self._binance_raw_order_to_ccxt(raw, symbol)

    async def _binance_signed_spot_get(
        self,
        path: str,
        exchange: Optional[ccxt.Exchange] = None,
    ) -> dict:
        result = await self._binance_signed_get(
            "https://api.binance.com", path, exchange
        )
        return result  # type: ignore[return-value]

    @staticmethod
    def _apply_binance_fapi_asset_rows(
        rows: list,
        free: Dict[str, float],
        used: Dict[str, float],
        total: Dict[str, float],
    ) -> None:
        for asset in rows:
            code = asset.get("asset")
            if not code:
                continue
            wallet = float(
                asset.get("balance") or asset.get("walletBalance") or 0.0
            )
            avail = float(asset.get("availableBalance") or wallet)
            total[str(code)] = wallet
            free[str(code)] = avail
            used[str(code)] = max(0.0, wallet - avail)

    async def _fetch_binance_futures_balance_rows(
        self, exchange: Optional[ccxt.Exchange] = None
    ) -> list:
        """Fetch USDT-M balance rows; try /fapi/v2/balance then /fapi/v2/account."""
        try:
            data = await self._binance_signed_get(
                "https://fapi.binance.com",
                "/fapi/v2/balance",
                exchange,
            )
            if isinstance(data, list):
                return data
        except Exception as balance_err:
            logger.debug(
                "Binance fapi/v2/balance failed, trying fapi/v2/account: {err}",
                err=self._format_exchange_error(balance_err),
            )

        account = await self._binance_signed_get(
            "https://fapi.binance.com",
            "/fapi/v2/account",
            exchange,
        )
        if isinstance(account, dict):
            assets = account.get("assets")
            if isinstance(assets, list):
                return assets
        raise RuntimeError("Unexpected fapi balance/account response")

    async def _fetch_binance_balance_urllib(
        self, exchange: Optional[ccxt.Exchange] = None
    ) -> Dict:
        """Fetch balance via urllib when ccxt/aiohttp cannot reach Binance."""
        mtype = self._choose_default_type_for_exchange()
        free: Dict[str, float] = {}
        used: Dict[str, float] = {}
        total: Dict[str, float] = {}

        if mtype in ("future", "swap"):
            rows = await self._fetch_binance_futures_balance_rows(exchange)
            self._apply_binance_fapi_asset_rows(rows, free, used, total)
        else:
            account = await self._binance_signed_spot_get(
                "/api/v3/account", exchange
            )
            for row in account.get("balances") or []:
                code = row.get("asset")
                if not code:
                    continue
                wallet = float(row.get("free", 0)) + float(row.get("locked", 0))
                avail = float(row.get("free", 0))
                locked = float(row.get("locked", 0))
                total[str(code)] = wallet
                free[str(code)] = avail
                used[str(code)] = locked

        return {"free": free, "used": used, "total": total}

    async def _fetch_binance_positions_urllib(
        self, exchange: Optional[ccxt.Exchange] = None
    ) -> List[Dict]:
        """Fetch USDT-M positions via urllib (avoids ccxt leverageBracket aiohttp call)."""
        from valuecell.agents.common.trading.data import binance_http

        rows = await self._binance_signed_get(
            "https://fapi.binance.com",
            "/fapi/v2/positionRisk",
            exchange,
        )
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected fapi positionRisk response")

        positions: List[Dict] = []
        for row in rows:
            amt = float(row.get("positionAmt") or 0)
            if amt == 0:
                continue
            mark = float(row.get("markPrice") or 0)
            symbol = binance_http.binance_id_to_symbol(str(row.get("symbol", "")))
            positions.append(
                {
                    "symbol": symbol,
                    "contracts": abs(amt),
                    "side": "long" if amt > 0 else "short",
                    "entryPrice": float(row.get("entryPrice") or 0),
                    "markPrice": mark,
                    "unrealizedPnl": float(row.get("unrealizedProfit") or 0),
                    "leverage": int(float(row.get("leverage") or 1)),
                    "notional": abs(amt) * mark if mark else 0.0,
                    "timestamp": row.get("updateTime"),
                }
            )
        return positions

    @staticmethod
    async def _fetch_binance_public_json(url: str, timeout_s: float = 30.0) -> dict:
        """Fetch a public Binance JSON endpoint without ccxt/aiohttp."""

        def _fetch_sync() -> dict:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "valuecell/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode())

        return await asyncio.to_thread(_fetch_sync)

    @staticmethod
    async def _fetch_binance_spot_server_time_ms() -> int:
        """Fetch Binance spot server time without ccxt/aiohttp (network fallback)."""
        payload = await CCXTExecutionGateway._fetch_binance_public_json(
            "https://api.binance.com/api/v3/time",
            timeout_s=15.0,
        )
        return int(payload["serverTime"])

    async def _load_binance_markets_urllib(self, exchange: ccxt.Exchange) -> None:
        """Load Binance markets via urllib when ccxt/aiohttp cannot reach the API."""
        endpoints = (
            "https://api.binance.com/api/v3/exchangeInfo",
            "https://fapi.binance.com/fapi/v1/exchangeInfo",
        )
        raw_symbols: List[dict] = []
        for url in endpoints:
            try:
                payload = await self._fetch_binance_public_json(url)
                raw_symbols.extend(payload.get("symbols") or [])
            except Exception as e:
                logger.warning(
                    "Skipped Binance market metadata from {url}: {err}",
                    url=url,
                    err=e,
                )

        if not raw_symbols:
            raise RuntimeError(
                "Could not load Binance markets from public exchangeInfo endpoints"
            )

        # parse_market() expects margin metadata normally filled by fetch_markets().
        exchange.options.setdefault("crossMarginPairsData", [])
        exchange.options.setdefault("isolatedMarginPairsData", [])

        parsed = [exchange.parse_market(symbol) for symbol in raw_symbols]
        exchange.set_markets(parsed, None)

    async def _load_markets(self, exchange: ccxt.Exchange) -> None:
        """Load exchange markets with urllib fallback for Binance."""
        if self._markets_loaded:
            return
        try:
            await exchange.load_markets()
        except Exception as ccxt_err:
            if self.exchange_id != "binance":
                raise RuntimeError(
                    f"Failed to load markets for {self.exchange_id}: {ccxt_err}"
                ) from ccxt_err
            logger.debug(
                "CCXT load_markets failed for {exchange}, using urllib fallback: {err}",
                exchange=self.exchange_id,
                err=self._format_exchange_error(ccxt_err),
            )
            await self._load_binance_markets_urllib(exchange)
        self._markets_loaded = True

    async def _sync_exchange_clock(self, exchange: ccxt.Exchange) -> None:
        """Align signed request timestamps with the exchange server clock."""
        if not hasattr(exchange, "milliseconds"):
            return
        server_time: Optional[int] = None
        try:
            if self.exchange_id == "binance":
                try:
                    response = await exchange.public_get_time()
                    server_time = int(response["serverTime"])
                except Exception as ccxt_err:
                    logger.debug(
                        "CCXT time sync failed for {exchange}, using urllib fallback: {err}",
                        exchange=self.exchange_id,
                        err=ccxt_err,
                    )
                    server_time = await self._fetch_binance_spot_server_time_ms()
            elif hasattr(exchange, "load_time_difference"):
                await exchange.load_time_difference()
                logger.debug(
                    "Synced {exchange} clock offset: {offset}ms",
                    exchange=self.exchange_id,
                    offset=exchange.options.get("timeDifference"),
                )
                return
            else:
                return
            after = exchange.milliseconds()
            exchange.options["timeDifference"] = after - server_time
            logger.debug(
                "Synced {exchange} clock offset: {offset}ms",
                exchange=self.exchange_id,
                offset=exchange.options["timeDifference"],
            )
        except Exception as e:
            logger.warning(
                "Could not sync clock with {exchange}: {err}",
                exchange=self.exchange_id,
                err=e,
            )

    @staticmethod
    def _binance_error_hint(exc: Optional[Exception]) -> str:
        """Map Binance API error text to actionable user guidance."""
        if exc is None:
            return ""
        text = str(exc)
        if "-2015" in text or "Invalid API-key, IP" in text:
            if "fapi" in text.lower():
                return (
                    " Enable Futures on your Binance API key (API Management → edit key → "
                    "Enable Futures). Grid/perpetual strategies use USDT-M futures "
                    "(fapi.binance.com), not spot-only keys. Also confirm your server's "
                    "public IP is on the key whitelist."
                )
            return (
                " Check API key permissions and IP whitelist: add this machine's public "
                "IP in Binance API settings, and enable Futures for perpetual strategies."
            )
        if "-1021" in text:
            return " Clock skew: enable automatic date/time sync on your computer."
        if "-1022" in text or "Signature for this request is not valid" in text:
            return " Secret key is incorrect or was copied with extra spaces."
        if "-2014" in text or "Invalid API-key" in text:
            return " API key or secret appears invalid."
        return ""

    async def _probe_binance_auth(
        self, exchange: ccxt.Exchange, account_kind: str
    ) -> None:
        """Verify Binance credentials without wallet/capital SAPI calls."""
        if account_kind == "future":
            await exchange.fapiprivatev2_get_account()
            return

        try:
            await exchange.private_get_account()
        except Exception as ccxt_err:
            logger.debug(
                "CCXT spot auth failed, trying urllib fallback: {err}",
                err=self._format_exchange_error(ccxt_err),
            )
            await self._binance_signed_spot_get("/api/v3/account", exchange)

    async def _probe_binance_futures_urllib(
        self, exchange: Optional[ccxt.Exchange] = None
    ) -> None:
        """Verify Binance USDT-M futures API access via urllib."""
        await self._binance_signed_get(
            "https://fapi.binance.com",
            "/fapi/v2/account",
            exchange,
        )

    def _balance_fetch_param_candidates(self) -> List[Optional[Dict[str, str]]]:
        """Ordered balance fetch params to try for connection tests."""
        primary = self._balance_fetch_params()
        candidates: List[Optional[Dict[str, str]]] = []
        if primary is not None:
            candidates.append(primary)
        if self.exchange_id == "binance":
            spot = {"type": "spot"}
            if primary != spot:
                candidates.append(spot)
        if self.exchange_id == "okx":
            trading = {"type": "trading"}
            if primary != trading:
                candidates.append(trading)
        candidates.append(None)
        return candidates

    async def test_connection(
        self, *, require_futures: bool = False
    ) -> tuple[bool, str]:
        """Test connectivity and authentication.

        Args:
            require_futures: When True, futures API access must work (swap strategies).

        Returns:
            (success, message) — message is user-facing on success or failure.
        """
        exchange = await self._get_exchange(
            load_markets=False,
            configure_position_mode=False,
        )
        spot_error: Optional[Exception] = None
        future_error: Optional[Exception] = None
        futures_ok = False
        spot_ok = False

        if self.exchange_id == "binance":
            # Spot first: works with typical trading keys; futures may be disabled.
            try:
                await self._probe_binance_auth(exchange, "spot")
                spot_ok = True
            except Exception as e:
                spot_error = e

            if not spot_ok:
                logger.warning(
                    "Binance spot auth failed: {err}",
                    err=self._format_exchange_error(spot_error)
                    if spot_error
                    else "unknown",
                )
            else:
                try:
                    await self._probe_binance_futures_urllib(exchange)
                    futures_ok = True
                except Exception as e:
                    future_error = e
                    logger.debug(
                        "Binance futures auth not available: {err}",
                        err=self._format_exchange_error(e),
                    )
        else:
            for params in self._balance_fetch_param_candidates():
                try:
                    if params:
                        await exchange.fetch_balance(params)
                    else:
                        await exchange.fetch_balance()
                    if params == {"type": "future"}:
                        futures_ok = True
                    elif params == {"type": "spot"}:
                        spot_ok = True
                    else:
                        futures_ok = True
                        spot_ok = True
                except Exception as e:
                    spot_error = e

        last_error = spot_error or future_error

        if futures_ok:
            return True, "Success! Spot and USDT-M futures API access verified."
        if spot_ok and require_futures and self.exchange_id == "binance":
            hint = self._binance_error_hint(future_error)
            detail = (
                self._format_exchange_error(future_error)
                if future_error
                else "Futures API not accessible"
            )
            return (
                False,
                "Spot API works, but USDT-M futures is not enabled for this key. "
                f"{detail}.{hint}",
            )
        if spot_ok and self.exchange_id == "binance":
            return (
                True,
                "Connected (spot only). Enable Futures on your Binance API key for "
                "perpetual/swap strategies.",
            )
        if spot_ok:
            return True, "Success!"

        err_detail = (
            self._format_exchange_error(last_error)
            if last_error
            else "unknown"
        )
        logger.warning(
            "Connection test failed for {exchange}: {err}",
            exchange=self.exchange_id,
            err=err_detail,
        )
        hint = "Check API key, secret, and exchange permissions."
        if self.exchange_id == "binance":
            hint += self._binance_error_hint(last_error)
            if spot_error:
                hint += (
                    " If spot fails, verify API key/secret and that Reading + "
                    "Spot/Margin trading are enabled."
                )
        else:
            hint += (
                " If IP restriction is enabled, whitelist this machine's public IP."
            )
        return False, f"Connection failed. {hint}"

    async def close(self) -> None:
        """Close the exchange connection and cleanup resources."""
        if self._exchange is None:
            return
        try:
            await self._exchange.close()
        except Exception as e:
            logger.warning(
                "Error closing {exchange} client: {err}",
                exchange=self.exchange_id,
                err=e,
            )
        finally:
            self._exchange = None

    async def fetch_balance(self) -> Dict:
        """Fetch account balance from exchange.

        Returns:
            Balance dictionary with free, used, and total amounts per currency
        """
        exchange = await self._get_exchange()
        try:
            params = self._balance_fetch_params()
            if params:
                return await exchange.fetch_balance(params)
            return await exchange.fetch_balance()
        except Exception as ccxt_err:
            if self.exchange_id != "binance":
                raise
            logger.debug(
                "CCXT fetch_balance failed for {exchange}, using urllib fallback: {err}",
                exchange=self.exchange_id,
                err=self._format_exchange_error(ccxt_err),
            )
            try:
                balance = await self._fetch_binance_balance_urllib(exchange)
            except Exception as urllib_err:
                detail = self._format_exchange_error(urllib_err)
                hint = self._binance_error_hint(urllib_err)
                logger.warning(
                    "Binance urllib balance failed: {detail}{hint}",
                    detail=detail,
                    hint=hint,
                )
                raise urllib_err from ccxt_err
            usdt_free = (balance.get("free") or {}).get("USDT")
            if usdt_free is not None:
                logger.info(
                    "Binance futures balance (urllib): USDT free={usdt}",
                    usdt=usdt_free,
                )
            return balance

    async def fetch_positions(self, symbols: Optional[List[str]] = None) -> List[Dict]:
        """Fetch current positions from exchange.

        Args:
            symbols: Optional list of symbols to fetch positions for

        Returns:
            List of position dictionaries
        """
        exchange = await self._get_exchange()

        # Check if exchange supports fetching positions
        if not exchange.has.get("fetchPositions"):
            return []

        mtype = self._choose_default_type_for_exchange()
        if self.exchange_id == "binance" and mtype == "spot":
            return []

        # Normalize symbols if provided
        normalized_symbols = None
        if symbols:
            normalized_symbols = [self._normalize_symbol(s) for s in symbols]

        try:
            positions = await exchange.fetch_positions(normalized_symbols)
            return positions
        except Exception as e:
            if self.exchange_id == "binance":
                logger.debug(
                    "CCXT fetch_positions failed for {exchange}, using urllib fallback: {err}",
                    exchange=self.exchange_id,
                    err=self._format_exchange_error(e),
                )
                positions = await self._fetch_binance_positions_urllib(exchange)
                if normalized_symbols:
                    allowed = {self._normalize_symbol(s) for s in symbols or []}
                    allowed_base = {s.split(":")[0] for s in allowed}
                    positions = [
                        p
                        for p in positions
                        if p.get("symbol") in allowed
                        or p.get("symbol") in allowed_base
                    ]
                return positions
            logger.warning(
                "Could not fetch positions for {exchange}: {err}",
                exchange=self.exchange_id,
                err=e,
            )
            raise

    async def cancel_order(self, order_id: str, symbol: str) -> Dict:
        """Cancel an open order.

        Args:
            order_id: Order ID to cancel
            symbol: Symbol of the order

        Returns:
            Cancellation result dictionary
        """
        exchange = await self._get_exchange()
        normalized_symbol = self._normalize_symbol(symbol)
        return await exchange.cancel_order(order_id, normalized_symbol)

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Fetch open orders from exchange.

        Args:
            symbol: Optional symbol to filter orders

        Returns:
            List of open order dictionaries
        """
        exchange = await self._get_exchange()
        normalized_symbol = self._normalize_symbol(symbol) if symbol else None
        return await exchange.fetch_open_orders(normalized_symbol)

    def __repr__(self) -> str:
        mode = "testnet" if self.testnet else "live"
        return (
            f"CCXTExecutionGateway(exchange={self.exchange_id}, "
            f"type={self.default_type}, margin={self.margin_mode}, mode={mode})"
        )


async def create_ccxt_gateway(
    exchange_id: str,
    api_key: str,
    secret_key: str,
    passphrase: Optional[str] = None,
    wallet_address: Optional[str] = None,
    private_key: Optional[str] = None,
    testnet: bool = False,
    market_type: str = "swap",
    margin_mode: str = "cross",
    position_mode: str = "oneway",
    preload_markets: bool = True,
    **ccxt_options,
) -> CCXTExecutionGateway:
    """Factory function to create and initialize a CCXT execution gateway.

    Args:
        exchange_id: Exchange identifier (e.g., 'binance', 'okx', 'bybit', 'hyperliquid')
        api_key: API key for authentication (not required for Hyperliquid)
        secret_key: Secret key for authentication (not required for Hyperliquid)
        passphrase: Optional passphrase (required for OKX)
        wallet_address: Wallet address (required for Hyperliquid)
        private_key: Private key (required for Hyperliquid)
        testnet: Whether to use testnet/sandbox mode
        market_type: Market type ('spot', 'future', 'swap')
        margin_mode: Margin mode ('isolated' or 'cross')
        position_mode: Optional position mode ('oneway' or 'hedged')
        preload_markets: Load markets on init (disable for lightweight connection tests)
        **ccxt_options: Additional CCXT exchange options

    Returns:
        Initialized CCXT execution gateway

    Example:
        >>> gateway = await create_ccxt_gateway(
        ...     exchange_id='binance',
        ...     api_key='YOUR_KEY',
        ...     secret_key='YOUR_SECRET',
        ...     market_type='swap',  # For perpetual futures
        ...     margin_mode='isolated',
        ...     position_mode='oneway',
        ...     testnet=True
        ... )
    """
    gateway = CCXTExecutionGateway(
        exchange_id=exchange_id,
        api_key=api_key,
        secret_key=secret_key,
        passphrase=passphrase,
        wallet_address=wallet_address,
        private_key=private_key,
        testnet=testnet,
        default_type=CCXTExecutionGateway._coerce_market_type(market_type),
        margin_mode=margin_mode,
        position_mode=position_mode,
        ccxt_options=ccxt_options,
    )

    if preload_markets:
        await gateway._get_exchange()

    return gateway
