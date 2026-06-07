"""
Hyperliquid trade execution layer.
Uses the official hyperliquid-python-sdk to open/close perp positions.

Environment variables:
  HL_PRIVATE_KEY      Ethereum private key of your Hyperliquid wallet (required)
  HL_WALLET_ADDRESS   Optional -- if omitted, derived from private key
  HL_TESTNET          "true" (default) or "false"
  POSITION_SIZE_PCT   Fraction of account equity per trade, e.g. "0.1" = 10%
  LEVERAGE            Integer leverage, e.g. "3"
  LEVERAGE_MODE       "cross" (default) or "isolated"
"""

import os
import logging
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

SLIPPAGE    = 0.005   # 0.5% slippage tolerance on market orders
MIN_SIZE    = 0.001   # absolute minimum order size (coins)
MAX_RETRIES = 3


def _retryable(fn):
    """Decorator: retry up to MAX_RETRIES times with exponential back-off."""
    return retry(
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )(fn)


def _round_price(price: float) -> float:
    """Round price to appropriate precision for HL order placement."""
    if price >= 10000:
        return round(price, 1)
    elif price >= 1000:
        return round(price, 2)
    elif price >= 100:
        return round(price, 3)
    elif price >= 1:
        return round(price, 3)
    else:
        return round(price, 6)


class HyperliquidTrader:
    def __init__(self):
        # Validate required env vars on startup
        private_key = os.environ.get("HL_PRIVATE_KEY", "")
        if not private_key or private_key.startswith("0xYOUR"):
            raise ValueError(
                "HL_PRIVATE_KEY is not set. "
                "Copy .env.example to .env and fill in your private key."
            )

        self.account = eth_account.Account.from_key(private_key)
        self.address = os.environ.get("HL_WALLET_ADDRESS") or self.account.address

        use_testnet   = os.environ.get("HL_TESTNET", "true").lower() == "true"
        self.base_url = constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL
        net_label     = "TESTNET" if use_testnet else "MAINNET"

        self.info     = Info(self.base_url, skip_ws=True)
        self.exchange = Exchange(self.account, self.base_url, account_address=self.address)

        self.size_pct         = float(os.environ.get("POSITION_SIZE_PCT", "0.1"))
        self.leverage         = int(os.environ.get("LEVERAGE", "3"))
        self.leverage_mode    = os.environ.get("LEVERAGE_MODE", "cross")
        self.max_position_pct = float(os.environ.get("MAX_POSITION_SIZE_PCT", "0.5"))

        logger.info(
            f"HyperliquidTrader ready | {net_label} | "
            f"addr={self.address[:8]}... | "
            f"size={self.size_pct * 100:.0f}% | "
            f"lev={self.leverage}x {self.leverage_mode}"
        )

    # -- Internal helpers (with retry) ----------------------------------------

    @_retryable
    def _equity(self) -> float:
        state = self.info.user_state(self.address)
        return float(state["marginSummary"]["accountValue"])

    @_retryable
    def _mid(self, coin: str) -> float:
        mids = self.info.all_mids()
        if coin not in mids:
            raise ValueError(f"Coin '{coin}' not found on Hyperliquid")
        return float(mids[coin])

    def _set_leverage(self, coin: str):
        is_cross = self.leverage_mode == "cross"
        try:
            self.exchange.update_leverage(self.leverage, coin, is_cross=is_cross)
            logger.debug(f"[{coin}] Leverage set to {self.leverage}x ({self.leverage_mode})")
        except Exception as e:
            logger.warning(f"[{coin}] Could not set leverage (continuing): {e}")

    def _calc_size(self, coin: str) -> float:
        equity   = self._equity()          # single API call, reused below
        price    = self._mid(coin)
        notional = equity * self.size_pct * self.leverage
        sz       = notional / price

        # Round to reasonable precision based on price magnitude
        if price > 10000:
            sz = round(sz, 4)
        elif price > 1000:
            sz = round(sz, 3)
        elif price > 1:
            sz = round(sz, 2)
        else:
            sz = round(sz, 1)

        if sz < MIN_SIZE:
            raise ValueError(
                f"[{coin}] Calculated size {sz} is below minimum {MIN_SIZE}. "
                f"Increase POSITION_SIZE_PCT or add more equity."
            )

        # Safety cap -- hard ceiling regardless of POSITION_SIZE_PCT
        max_sz = round((equity * self.max_position_pct * self.leverage) / price, 4)
        if sz > max_sz:
            logger.warning(
                f"[{coin}] Size {sz:.4f} capped to {max_sz:.4f} "
                f"(MAX_POSITION_SIZE_PCT={self.max_position_pct})"
            )
            sz = max_sz

        logger.info(
            f"[{coin}] Size: equity={equity:.2f} USDC x "
            f"{self.size_pct * 100:.0f}% x {self.leverage}x / {price:.4f} = {sz}"
        )
        return sz

    @_retryable
    def _position(self, coin: str, side: str) -> dict | None:
        state     = self.info.user_state(self.address)
        positions = state.get("assetPositions", [])
        for entry in positions:
            pos = entry.get("position", {})
            szi = float(pos.get("szi", 0))
            if pos.get("coin") == coin:
                if side == "long"  and szi > 0:
                    return pos
                if side == "short" and szi < 0:
                    return pos
        return None

    # -- Public API -----------------------------------------------------------

    def get_price(self, coin: str) -> float:
        """Return the current mid price for a coin."""
        return self._mid(coin)

    def open_long(self, coin: str) -> str:
        try:
            self._set_leverage(coin)
            sz     = self._calc_size(coin)
            result = self.exchange.market_open(coin, is_buy=True, sz=sz, slippage=SLIPPAGE)
            logger.info(f"[{coin}] Long opened | sz={sz} | result={result}")
            return self._format_result(result)
        except Exception as e:
            logger.error(f"[{coin}] open_long failed: {e}")
            return f"ERROR: {e}"

    def close_long(self, coin: str, pct: float = 1.0) -> str:
        try:
            pos = self._position(coin, "long")
            if not pos:
                logger.warning(f"[{coin}] close_long called but no open long found")
                return f"No open long on {coin}"
            sz     = round(float(pos["szi"]) * pct, 4)
            result = self.exchange.market_close(coin, sz=sz, slippage=SLIPPAGE)
            logger.info(f"[{coin}] Long closed {pct * 100:.0f}% | sz={sz} | result={result}")
            return self._format_result(result)
        except Exception as e:
            logger.error(f"[{coin}] close_long failed: {e}")
            return f"ERROR: {e}"

    def open_short(self, coin: str) -> str:
        try:
            self._set_leverage(coin)
            sz     = self._calc_size(coin)
            result = self.exchange.market_open(coin, is_buy=False, sz=sz, slippage=SLIPPAGE)
            logger.info(f"[{coin}] Short opened | sz={sz} | result={result}")
            return self._format_result(result)
        except Exception as e:
            logger.error(f"[{coin}] open_short failed: {e}")
            return f"ERROR: {e}"

    def close_short(self, coin: str, pct: float = 1.0) -> str:
        try:
            pos = self._position(coin, "short")
            if not pos:
                logger.warning(f"[{coin}] close_short called but no open short found")
                return f"No open short on {coin}"
            sz     = round(abs(float(pos["szi"])) * pct, 4)
            result = self.exchange.market_close(coin, sz=sz, slippage=SLIPPAGE)
            logger.info(f"[{coin}] Short closed {pct * 100:.0f}% | sz={sz} | result={result}")
            return self._format_result(result)
        except Exception as e:
            logger.error(f"[{coin}] close_short failed: {e}")
            return f"ERROR: {e}"

    def flip_to_short(self, coin: str) -> str:
        """Close any open long on coin, then open a short."""
        messages = []
        try:
            pos = self._position(coin, "long")
            if pos:
                sz           = float(pos["szi"])
                close_result = self.exchange.market_close(coin, sz=sz, slippage=SLIPPAGE)
                msg          = f"long closed ({sz}): {self._format_result(close_result)}"
                messages.append(msg)
                logger.info(f"[{coin}] Flip: {msg}")

            self._set_leverage(coin)
            sz     = self._calc_size(coin)
            result = self.exchange.market_open(coin, is_buy=False, sz=sz, slippage=SLIPPAGE)
            msg    = f"short opened ({sz}): {self._format_result(result)}"
            messages.append(msg)
            logger.info(f"[{coin}] Flip: {msg}")
            return " | ".join(messages)
        except Exception as e:
            logger.error(f"[{coin}] flip_to_short failed: {e}")
            return f"ERROR: {e}"

    def place_stop_loss(self, coin: str, is_long: bool, stop_price: float, size: float) -> int:
        """
        Place a reduce-only stop-market order on Hyperliquid.
        Survives container restarts -- lives on the exchange until cancelled or filled.
        Returns the HL order ID (oid).
        """
        is_buy    = not is_long  # closing long = sell order, closing short = buy order
        stop_price = _round_price(stop_price)

        result = self.exchange.order(
            coin,
            is_buy,
            size,
            stop_price,
            {"trigger": {"triggerPx": stop_price, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True,
        )

        if result.get("status") != "ok":
            raise RuntimeError(f"place_stop_loss rejected: {result}")

        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            raise RuntimeError(f"place_stop_loss: empty statuses in response: {result}")

        status = statuses[0]
        if "resting" in status:
            oid = status["resting"]["oid"]
            logger.info(
                f"[{coin}] Native stop-loss placed | "
                f"{'long' if is_long else 'short'} | "
                f"stop={stop_price} | size={size} | oid={oid}"
            )
            return oid

        raise RuntimeError(f"place_stop_loss: unexpected status format: {status}")

    def cancel_order(self, coin: str, oid: int):
        """Cancel an open order by ID. Raises on failure."""
        result = self.exchange.cancel(coin, oid)
        if result.get("status") != "ok":
            raise RuntimeError(f"cancel_order failed: {result}")
        logger.debug(f"[{coin}] Order {oid} cancelled")

    def get_open_stop_orders(self, coin: str) -> list[int]:
        """
        Return order IDs of any resting trigger/stop orders for a coin.
        Used on startup to cancel orphaned stops from a previous container run.
        """
        try:
            orders = self.info.open_orders(self.address)
            oids = []
            for o in orders:
                if o.get("coin") != coin:
                    continue
                # Trigger orders have an 'orderType' containing 'Stop' or a triggerCondition
                order_type = o.get("orderType", "")
                if "Stop" in order_type or "Trigger" in order_type:
                    oids.append(o["oid"])
            return oids
        except Exception as e:
            logger.warning(f"[{coin}] Could not fetch open orders: {e}")
            return []

    def get_positions(self) -> list:
        state     = self.info.user_state(self.address)
        positions = []
        for entry in state.get("assetPositions", []):
            pos = entry.get("position", {})
            szi = float(pos.get("szi", 0))
            if szi == 0:
                continue
            positions.append({
                "coin":           pos.get("coin"),
                "side":           "long" if szi > 0 else "short",
                "size":           abs(szi),
                "entry_price":    float(pos.get("en