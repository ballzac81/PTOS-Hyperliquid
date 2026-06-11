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

Per-coin overrides: set {COIN}_TRAILING_STOP_PCT=0.04 to override the global
trailing stop for a specific coin. E.g. HYPE_TRAILING_STOP_PCT=0.07

Additional protections:
  STOP_LOSS_PCT    -- close if price drops X% from entry price (not peak)
  MAX_HOLD_SECONDS -- auto-close after this many seconds regardless of price

Set TRAILING_STOP_PCT=0 in .env to disable entirely.
"""

import os
import time
import logging
import threading

logger = logging.getLogger(__name__)

STOP_LOSS_PCT    = float(os.environ.get("STOP_LOSS_PCT", "0"))
MAX_HOLD_SECONDS = int(os.environ.get("MAX_HOLD_SECONDS", "0"))


class PositionMonitor:
    def __init__(self, trader, notifier, cooldown_until: dict = None, cooldown_seconds: int = 0,
                 armed: dict = None, armed_lock=None,
                 rearm_after_stop: bool = False, rearm_delay_seconds: int = 3600,
                 trade_log_callback=None):
        self.trader   = trader
        self.notifier = notifier
        self.trail_pct = float(os.environ.get("TRAILING_STOP_PCT", "0.05"))
        self.interval  = int(os.environ.get("MONITOR_INTERVAL_SECONDS", "30"))

        # best[(coin, side)] = best price seen since position first detected
        self._best: dict = {}

        # Entry tracking
        self._entry_price: dict = {}
        self._entry_time:  dict = {}

        # Native resting stop orders on HL: (coin, side) -> HL order ID
        self._stop_orders: dict = {}

        self._cooldown_until   = cooldown_until
        self._cooldown_seconds = cooldown_seconds

        # Re-arm after stop-out -- shared refs from signal_tracker
        self._armed               = armed
        self._armed_lock          = armed_lock
        self._rearm_after_stop    = rearm_after_stop
        self._rearm_delay_seconds = rearm_delay_seconds

        # Optional callback to log stop-triggered closes to the trade log
        self._trade_log_callback = trade_log_callback

        # Tracks coins currently being closed to prevent double-close race condition
        self._closing: set = set()
        self._stop         = threading.Event()

    def start(self):
        if self.trail_pct <= 0 and STOP_LOSS_PCT <= 0 and MAX_HOLD_SECONDS <= 0:
            logger.info("Position monitor disabled (no trailing stop, stop loss, or max hold configured)")
            return
        logger.info(
            f"Position monitor started | "
            f"trailing stop={self.trail_pct * 100:.1f}% | "
            f"stop loss from entry={STOP_LOSS_PCT * 100:.1f}% | "
            f"max hold={'off' if MAX_HOLD_SECONDS == 0 else f'{MAX_HOLD_SECONDS}s'} | "
            f"check every {self.interval}s"
        )
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _trail_pct_for(self, coin: str) -> float:
        """Return per-coin trailing stop if configured, else global."""
        val = os.environ.get(f"{coin}_TRAILING_STOP_PCT")
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return self.trail_pct

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self._check_all()
            except Exception as e:
                logger.error(f"Monitor error: {e}")

    # -- Native stop order helpers --------------------------------------------

    def _place_native_stop(self, coin: str, side: str, stop_price: float, size: float):
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
        key = (coin, side)
        oid = self._stop_orders.pop(key, None)
        if oid is None:
            return
        try:
            self.trader.cancel_order(coin, oid)
            logger.debug(f"[{coin}] Native stop cancelled | oid={oid}")
        except Exception as e:
            logger.debug(f"[{coin}] Native stop cancel failed (may have filled): {e}")

    def _update_native_stop(self, coin: str, side: str, new_stop_px: float, size: float):
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
            coin       = pos["coin"]
            side       = pos["side"]
            size       = pos["size"]
            entry_px   = pos.get("entry_price", 0)
            key        = (coin, side)
            active_keys.add(key)
            trail_pct  = self._trail_pct_for(coin)

            if key in self._closing:
                continue

            try:
                price = self.trader.get_price(coin)
            except Exception as e:
                logger.warning(f"[{coin}] Could not fetch price: {e}")
                continue

            if key not in self._best:
                self._best[key]         = price
                self._entry_price[key]  = entry_px if entry_px else price
                self._entry_time[key]   = time.time()

                if trail_pct > 0:
                    stop_px = (
                        price * (1 - trail_pct) if side == "long"
                        else price * (1 + trail_pct)
                    )
                    logger.info(
                        f"[{coin}] {side.upper()} detected -- trailing stop={trail_pct*100:.1f}% | "
                        f"ref={price:.4f} | "
                        f"stop={'below' if side == 'long' else 'above'} {stop_px:.4f}"
                    )
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
                else:
                    logger.info(
                        f"[{coin}] {side.upper()} detected | "
                        f"trailing stop disabled for this coin | "
                        f"entry={self._entry_price[key]:.4f}"
                    )
                continue

            # -- Max hold time check ------------------------------------------
            if MAX_HOLD_SECONDS > 0:
                held = time.time() - self._entry_time.get(key, time.time())
                if held > MAX_HOLD_SECONDS:
                    hours = held / 3600
                    logger.warning(
                        f"[{coin}] Max hold time reached | "
                        f"held={hours:.1f}h | limit={MAX_HOLD_SECONDS/3600:.1f}h"
                    )
                    self._trigger(coin, side, price, self._best[key],
                                  price, reason="max_hold_time")
                    continue

            # -- Fixed stop loss from entry ------------------------------------
            if STOP_LOSS_PCT > 0:
                ep = self._entry_price.get(key, price)
                if ep > 0:
                    loss_from_entry = (
                        (ep - price) / ep if side == "long"
                        else (price - ep) / ep
                    )
                    if loss_from_entry >= STOP_LOSS_PCT:
                        logger.warning(
                            f"[{coin}] Stop loss from entry hit | "
                            f"side={side} | entry={ep:.4f} | price={price:.4f} | "
                            f"loss={loss_from_entry*100:.1f}%"
                        )
                        self._trigger(coin, side, price, self._best[key],
                                      price, reason="stop_loss_from_entry")
                        continue

            # -- Trailing stop ------------------------------------------------
            if trail_pct <= 0:
                continue

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
                self._best[key] * (1 - trail_pct) if side == "long"
                else self._best[key] * (1 + trail_pct)
            )

            if new_peak:
                self._update_native_stop(coin, side, stop_px, size)

            if (side == "long" and price <= stop_px) or (side == "short" and price >= stop_px):
                self._trigger(coin, side, price, self._best[key], stop_px,
                              reason="trailing_stop")

        # -- Detect positions closed externally (native stop, manual close) ---
        for key in list(self._best.keys()):
            if key not in active_keys and key not in self._closing:
                coin, side = key
                logger.info(f"[{coin}] {side} position gone -- closed externally (stop order or manual)")
                self._cancel_native_stop(coin, side)
                self._notify_external_close(coin, side)
                del self._best[key]
                self._entry_price.pop(key, None)
                self._entry_time.pop(key, None)

    def _notify_external_close(self, coin: str, side: str):
        """
        Called when a tracked position disappears without _trigger() firing.
        This means the native HL stop order executed, or the position was manually closed.
        """
        key = (coin, side)
        ep  = self._entry_price.get(key)

        # Try to get the close price from recent fills
        close_price = None
        pnl_pct     = None
        try:
            fills = self.trader.get_fills(limit=10)
            for f in fills:
                if f.get("coin") == coin:
                    close_price = float(f.get("px", 0))
                    break
        except Exception:
            pass

        if ep and close_price:
            pnl_pct = (
                (close_price - ep) / ep * 100 if side == "long"
                else (ep - close_price) / ep * 100
            )

        lines = [f"[{coin}] {side.capitalize()} closed (stop order or manual)"]
        if ep:
            lines.append(f"Entry: {ep:.4f}")
        if close_price:
            lines.append(
                f"Close: {close_price:.4f}"
                + (f" | PnL: {pnl_pct:+.1f}%" if pnl_pct is not None else "")
            )

        self.notifier.send("\n".join(lines))

        if self._trade_log_callback is not None:
            try:
                result = (
                    f"close_px={close_price:.4f} | pnl={pnl_pct:+.1f}%"
                    if close_price and pnl_pct is not None
                    else f"close_px={close_price:.4f}" if close_price
                    else "close_px=unknown"
                )
                self._trade_log_callback(coin, f"native_stop_{side}", result)
            except Exception:
                pass

        if self._cooldown_until is not None and self._cooldown_seconds > 0:
            self._cooldown_until[coin] = time.time() + self._cooldown_seconds
            logger.info(f"[{coin}] Cooldown set for {self._cooldown_seconds}s after external close")

        if self._rearm_after_stop and self._armed is not None and self._armed_lock is not None:
            rearm_side  = "buy" if side == "long" else "sell"
            delay       = self._rearm_delay_seconds
            waiting_for = "trend-up" if rearm_side == "buy" else "trend-down"

            def _do_rearm(c=coin, s=rearm_side, d=delay, wf=waiting_for):
                time.sleep(d)
                with self._armed_lock:
                    if c not in self._armed:
                        self._armed[c] = {"buy": None, "sell": None}
                    self._armed[c][s] = time.time()
                logger.info(
                    f"[{c}] Auto re-armed {s} signal after external close "
                    f"(delay={d}s) -- waiting for {wf}"
                )
                self.notifier.send(
                    f"[{c}] Auto re-armed after stop-out -- waiting for {wf}\n"
                    f"(Signal expires in {self._rearm_delay_seconds}s if no confirmation)"
                )

            threading.Thread(target=_do_rearm, daemon=True).start()

    def _trigger(self, coin: str, side: str, price: float, best: float, stop_px: float,
                 reason: str = "trailing_stop"):
        key = (coin, side)

        if key in self._closing:
            logger.debug(f"[{coin}] Close already in progress for {side} -- skipping")
            return
        self._closing.add(key)

        self._cancel_native_stop(coin, side)

        if reason == "trailing_stop":
            pct_move = abs(price - best) / best * 100
            logger.warning(
                f"[{coin}] Trailing stop hit | side={side} | "
                f"price={price:.4f} | best={best:.4f} | "
                f"stop={stop_px:.4f} | pullback={pct_move:.1f}%"
            )
            self.notifier.send(
                f"[{coin}] Trailing stop hit -- closing {side}\n"
                f"Price: {price:.4f} | Best: {best:.4f} | Pulled back {pct_move:.1f}%"
            )
        elif reason == "stop_loss_from_entry":
            ep = self._entry_price.get(key, price)
            loss_pct = abs(price - ep) / ep * 100
            logger.warning(
                f"[{coin}] Stop loss from entry hit | side={side} | "
                f"entry={ep:.4f} | price={price:.4f} | loss={loss_pct:.1f}%"
            )
            self.notifier.send(
                f"[{coin}] Stop loss hit -- closing {side}\n"
                f"Entry: {ep:.4f} | Price: {price:.4f} | Loss: {loss_pct:.1f}%"
            )
        elif reason == "max_hold_time":
            held_h = (time.time() - self._entry_time.get(key, time.time())) / 3600
            logger.warning(
                f"[{coin}] Max hold time reached | side={side} | "
                f"held={held_h:.1f}h"
            )
            self.notifier.send(
                f"[{coin}] Max hold time reached -- closing {side}\n"
                f"Held: {held_h:.1f}h | Price: {price:.4f}"
            )

        try:
            if side == "long":
                result = self.trader.close_long(coin)
            else:
                result = self.trader.close_short(coin)

            logger.info(f"[{coin}] Position closed ({reason}): {result}")
            if self._cooldown_until is not None and self._cooldown_seconds > 0:
                self._cooldown_until[coin] = time.time() + self._cooldown_seconds
                logger.info(f"[{coin}] Cooldown set for {self._cooldown_seconds}s")

            self.notifier.send(
                f"[{coin}] {side.capitalize()} closed -- back to idle\n"
                f"Result: {result}"
            )

            # Log to dashboard trade log
            if self._trade_log_callback is not None:
                try:
                    self._trade_log_callback(coin, reason, result)
                except Exception:
                    pass

            # Auto re-arm if enabled
            if self._rearm_after_stop and self._armed is not None and self._armed_lock is not None:
                rearm_side  = "buy" if side == "long" else "sell"
                delay       = self._rearm_delay_seconds
                waiting_for = "trend-up" if rearm_side == "buy" else "trend-down"

                def _do_rearm(c=coin, s=rearm_side, d=delay, wf=waiting_for):
                    time.sleep(d)
                    with self._armed_lock:
                        if c not in self._armed:
                            self._armed[c] = {"buy": None, "sell": None}
                        self._armed[c][s] = time.time()
                    logger.info(
                        f"[{c}] Auto re-armed {s} signal after stop-out "
                        f"(delay={d}s) -- waiting for {wf}"
                    )
                    self.notifier.send(
                        f"[{c}] Auto re-armed after stop-out -- waiting for {wf}\n"
                        f"(Signal expires in {self._rearm_delay_seconds}s if no confirmation)"
                    )

                threading.Thread(target=_do_rearm, daemon=True).start()
                logger.info(f"[{coin}] Re-arm scheduled in {delay}s (REARM_AFTER_STOP=true)")

        except Exception as e:
            logger.error(f"[{coin}] Failed to close {side}: {e}")
            self.notifier.send(f"[{coin}] Close FAILED ({reason}): {e}")
        finally:
            self._best.pop(key, None)
            self._entry_price.pop(key, None)
            self._entry_time.pop(key, None)
            self._closing.discard(key)
