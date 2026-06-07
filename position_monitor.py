"""
Position Monitor -- trailing stop for all open Hyperliquid positions.

Dual-layer protection:
  1. Native resting stop-loss order placed directly on Hyperliquid.
     Survives container downtime. Executes instantly on price touch.
     Updated (cancel + replace) each time the trailing peak moves.

  2. Polling backstop loop (every MONITOR_INTERVAL_SECONDS).
     Catches any edge cases the native order misses and acts as a
     safety net if the exchange order placement fails.

For longs:  tracks the highest price reached. Stop trails TRAILING_STOP_PCT
            below that peak.
For shorts: tracks the lowest price reached. Stop trails TRAILING_STOP_PCT
            above that trough.

Set TRAILING_STOP_PCT=0 in .env to disable entirely.
"""

import os
import time
import logging
import threading

logger = logging.getLogger(__name__)


class PositionMonitor:
    def __init__(self, trader, notifier, cooldown_until: dict = None, cooldown_seconds: int = 0):
        self.trader   = trader
        self.notifier = notifier
        self.trail_pct = float(os.environ.get("TRAILING_STOP_PCT", "0.05"))
        self.interval  = int(os.environ.get("MONITOR_INTERVAL_SECONDS", "30"))

        # best[(coin, side)] = best price seen since position first detected
        #   long  -> highest price (stop fires if price drops trail_pct below this)
        #   short -> lowest price  (stop fires if price rises trail_pct above this)
        self._best: dict = {}

        # Native resting stop orders on HL: (coin, side) -> HL order ID
        # These survive container restarts on the exchange side.
        self._stop_orders: dict = {}

        self._cooldown_until   = cooldown_until  # shared ref from signal_tracker
        self._cooldown_seconds = cooldown_seconds

        # Tracks coins currently being closed to prevent double-close race condition
        self._closing: set = set()
        self._stop         = threading.Event()

    def start(self):
        if self.trail_pct <= 0:
            logger.info("Trailing stop disabled (TRAILING_STOP_PCT=0)")
            return
        logger.info(
            f"Position monitor started | "
            f"trailing stop={self.trail_pct * 100:.1f}% | "
            f"check every {self.interval}s | "
            f"native HL stop orders: enabled"
        )
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self._check_all()
            except Exception as e:
                logger.error(f"Monitor error: {e}")

    # -- Native stop order helpers --------------------------------------------

    def _place_native_stop(self, coin: str, side: str, stop_price: float, size: float) -> int | None:
        """
        Place a reduce-only stop-market on HL. Returns order ID or None on failure.
        Failure is non-fatal -- the polling loop still acts as backstop.
        """
        try:
            oid = self.trader.place_stop_loss(
                coin,
                is_long=(side == "long"),
                stop_price=stop_price,
                size=size,
            )
            return oid
        except Exception as e:
            logger.warning(
                f"[{coin}] Could not place native stop -- polling loop still active: {e}"
            )
            return None

    def _cancel_native_stop(self, coin: str, side: str):
        """
        Cancel the tracked resting stop order for this position.
        Silently ignores errors (order may have already filled or been cancelled).
        """
        key = (coin, side)
        oid = self._stop_orders.pop(key, None)
        if oid is None:
            return
        try:
            self.trader.cancel_order(coin, oid)
            logger.debug(f"[{coin}] Native stop cancelled | oid={oid}")
        except Exception as e:
            # This is expected if the native stop already triggered
            logger.debug(f"[{coin}] Native stop cancel failed (may have filled): {e}")

    def _update_native_stop(self, coin: str, side: str, new_stop_px: float, size: float):
        """Cancel old native stop and place a new one at the updated trailing level."""
        self._cancel_native_stop(coin, side)
        oid = self._place_native_stop(coin, side, new_stop_px, size)
        if oid is not None:
            self._stop_orders[(coin, side)] = oid

    # -- Main check loop ------------------------------------------------------

    def _check_all(self):
        try:
            positions = self.trader.get_positions()
        except Exception as e:
            logger.error(f"Monitor: failed to fetch positions (will retry next cycle): {e}")
            return
        active_keys = set()

        for pos in positions:
            coin = pos["coin"]
            side = pos["side"]
            size = pos["size"]
            key  = (coin, side)
            active_keys.add(key)

            # Skip if a close is already in flight for this position
            if key in self._closing:
                continue

            try:
                price = self.trader.get_price(coin)
            except Exception as e:
                logger.warning(f"[{coin}] Could not fetch price: {e}")
                continue

            # First time we see this position -- initialise and place native stop
            if key not in self._best:
                self._best[key] = price
                stop_px = (
                    price * (1 - self.trail_pct) if side == "long"
                    else price * (1 + self.trail_pct)
                )
                logger.info(
                    f"[{coin}] {side.upper()} detected -- trailing stop initialised | "
                    f"ref={price:.4f} | "
                    f"stop={'below' if side == 'long' else 'above'} {stop_px:.4f}"
                )
                # Cancel any orphaned stop orders left from a previous container run
                orphans = self.trader.get_open_stop_orders(coin)
                for orphan_oid in orphans:
                    try:
                        self.trader.cancel_order(coin, orphan_oid)
                        logger.info(f"[{coin}] Orphaned stop cancelled | oid={orphan_oid}")
                    except Exception as e:
                        logger.debug(f"[{coin}] Could not cancel orphan oid={orphan_oid}: {e}")
                oid = self._place_native_stop(coin, side, stop_px, size)
                if oid is not None:
                    self._stop_orders[key] = oid
                continue

            # Check for new peak -- update native stop if so
            new_peak = False
            if side == "long" and price > self._best[key]:
                self._best[key] = price
                new_peak = True
                logger.debug(f"[{coin}] Long peak -> {price:.4f}")
            elif side == "short" and price < self._best[key]:
                self._best[key] = price
                new_peak = True
                logger.debug(f"[{coin}] Short trough -> {price:.4f}")

            stop_px = (
                self._best[key] * (1 - self.trail_pct) if side == "long"
                else self._best[key] * (1 + self.trail_pct)
            )

            if new_peak:
                self._update_native_stop(coin, side, stop_px, size)

            # Polling backstop -- catches wicks the native stop may have missed
            if (side == "long" and price <= stop_px) or (side == "short" and price >= stop_px):
                self._trigger(coin, side, price, self._best[key], stop_px)

        # Clean up tracking for positions that no longer exist
        for key in list(self._best.keys()):
            if key not in active_keys and key not in self._closing:
                coin, side = key
                logger.info(f"[{coin}] {side} position gone -- removing from monitor")
                self._cancel_native_stop(coin, side)
                del self._best[key]

    def _trigger(self, coin: str, side: str, price: float, best: float, stop_px: float):
        """Polling backstop fired. Cancel native stop to prevent double-fill, then market close."""
        key = (coin, side)

        # Guard against double-close if two checks overlap
        if key in self._closing:
            logger.debug(f"[{coin}] Close already in progress for {side} -- skipping")
            return
        self._closing.add(key)

        # Cancel native resting stop first to prevent double-fill on exchange
        self._cancel_native_stop(coin, side)

        pct_move = abs(price - best) / best * 100
        logger.warning(
            f"[{coin}] Trailing stop hit (polling backstop) | side={side} | "
            f"price={price:.4f} | best={best:.4f} | "
            f"stop={stop_px:.4f} | pullback={pct_move:.1f}%"
        )
        self.notifier.send(
            f"[{coin}] Trailing stop hit -- closing {side}\n"
            f"Price: {price:.4f} | Best: {best:.4f} | Pulled back {pct_move:.1f}%"
        )

        try:
            if side == "long":
                result = self.trader.close_long(coin)
            else:
                result = self.trader.close_short(coin)

            logger.info(f"[{coin}] Trailing stop closed {side}: {result}")
            if self._cooldown_until is not None and self._cooldown_seconds > 0:
                self._cooldown_until[coin] = time.time() + self._cooldown_seconds
                logger.info(f"[{coin}] Cooldown set for {self._cooldown_seconds}s")