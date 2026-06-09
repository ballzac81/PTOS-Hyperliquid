"""
PTOS Signal Tracker -- Hyperliquid Edition (2-step, multi-signal)
Listens for TradingView webhooks and executes perp trades on Hyperliquid.

BUY flow:
  POST /buy-signal       Arm (or refresh) the buy watch. Can fire multiple times.
  POST /trend-up         Trend confirmed up -> open long (or flip short to long)

SELL flow:
  POST /sell-signal      Arm (or refresh) the sell watch. Can fire multiple times.
  POST /trend-down       Trend confirmed down -> close long / open short

Manual overrides (require X-Webhook-Secret header or token in JSON body):
  POST /emergency-close  Close ALL open positions to USDC + disarm all signals + cooldown
  POST /reset            Disarm all signals only -- positions and trailing stop stay active

  GET  /status           Current armed state per coin
  GET  /positions        Live Hyperliquid positions
  GET  /health           Health check
"""

import os
import sys
import time
import signal
import logging
import threading
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from hyperliquid_trader import HyperliquidTrader
from telegram_notify import TelegramNotifier
from position_monitor import PositionMonitor

# -- Config -------------------------------------------------------------------
SECRET_TOKEN         = os.environ.get("SECRET_TOKEN", "")
WINDOW_SECONDS       = int(os.environ.get("WINDOW_SECONDS", 0))
SELL_MODE            = os.environ.get("SELL_MODE", "short")
SELL_PCT             = float(os.environ.get("SELL_PCT", "1.0"))
REARM_AFTER_STOP     = os.environ.get("REARM_AFTER_STOP", "false").lower() == "true"
REARM_DELAY_SECONDS  = int(os.environ.get("REARM_DELAY_SECONDS", "3600"))

# -- Startup validation -------------------------------------------------------
if SELL_MODE not in ("short", "close_long", "open_short"):
    print(f"ERROR: Invalid SELL_MODE '{SELL_MODE}'. Must be: short, close_long, open_short", flush=True)
    sys.exit(1)

if not SECRET_TOKEN:
    print("WARNING: SECRET_TOKEN is not set -- all webhook endpoints are unprotected", flush=True)

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# -- App + rate limiter -------------------------------------------------------
app     = Flask(__name__)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["60 per minute"],
    storage_uri="memory://",
)

# -- Core services ------------------------------------------------------------
trader   = HyperliquidTrader()
notifier = TelegramNotifier()
lock     = threading.Lock()

# -- State --------------------------------------------------------------------
armed: dict = {}
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "0"))
cooldown_until: dict = {}

# Start trailing stop monitor
monitor = PositionMonitor(
    trader,
    notifier,
    cooldown_until=cooldown_until,
    cooldown_seconds=COOLDOWN_SECONDS,
    armed=armed,
    armed_lock=lock,
    rearm_after_stop=REARM_AFTER_STOP,
    rearm_delay_seconds=REARM_DELAY_SECONDS,
)
monitor.start()

# -- SIGTERM handler ----------------------------------------------------------
def _shutdown_handler(signum, frame):
    notifier.send("PTOS container shutting down -- no stop monitoring until restart!")
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown_handler)

# -- Startup Telegram notification --------------------------------------------
net_label   = "MAINNET" if os.environ.get("HL_TESTNET", "true").lower() == "false" else "TESTNET"
rearm_label = f" | rearm={REARM_DELAY_SECONDS}s" if REARM_AFTER_STOP else ""
notifier.send(
    f"PTOS started ({net_label}) | "
    f"trailing stop={float(os.environ.get('TRAILING_STOP_PCT', '0.05')) * 100:.0f}% | "
    f"size={float(os.environ.get('POSITION_SIZE_PCT', '0.1')) * 100:.0f}% | "
    f"lev={os.environ.get('LEVERAGE', '3')}x"
    f"{rearm_label}"
)

# -- Helpers ------------------------------------------------------------------

def normalize_coin(raw: str) -> str:
    coin = raw.upper().strip()
    for suffix in [
        "/USD", "/USDC", "/USDT", "/BUSD", "/PERP",
        "-USD", "-USDC", "-USDT", "-PERP",
        ".P",
    ]:
        if coin.endswith(suffix):
            coin = coin[: -len(suffix)]
            break
    return coin


def verify_token(data: dict) -> bool:
    if not SECRET_TOKEN:
        return True
    return (
        data.get("token") == SECRET_TOKEN
        or request.headers.get("X-Webhook-Secret") == SECRET_TOKEN
    )


def _is_armed(coin: str, side: str) -> bool:
    ts = armed.get(coin, {}).get(side)
    if ts is None:
        return False
    if WINDOW_SECONDS == 0:
        return True
    return time.time() - ts <= WINDOW_SECONDS


def _arm(coin: str, side: str) -> bool:
    if coin not in armed:
        armed[coin] = {"buy": None, "sell": None}
    was_armed = _is_armed(coin, side)
    armed[coin][side] = time.time()
    return was_armed


def _disarm(coin: str, side: str):
    if coin in armed:
        armed[coin][side] = None


def _window_remaining(coin: str, side: str) -> int:
    ts = armed.get(coin, {}).get(side)
    if ts is None:
        return 0
    if WINDOW_SECONDS == 0:
        return -1
    return max(0, int(WINDOW_SECONDS - (time.time() - ts)))


# -- BUY flow -----------------------------------------------------------------

@app.route("/buy-signal", methods=["POST"])
@limiter.limit("30 per minute")
def buy_signal():
    data = request.get_json(silent=True) or {}
    if not verify_token(data):
        return jsonify({"error": "Unauthorized"}), 401

    coin = normalize_coin(data.get("coin") or data.get("pair", "SOL"))
    with lock:
        refreshed = _arm(coin, "buy")

    if refreshed:
        logger.info(f"[{coin}] BUY signal refreshed -- still waiting for trend-up")
        notifier.send(f"[{coin}] BUY signal refreshed -- still waiting for trend turn up")
    else:
        logger.info(f"[{coin}] BUY signal armed -- waiting for trend-up")
        notifier.send(f"[{coin}] BUY signal armed -- waiting for trend turn up")

    return jsonify({
        "status":             "refreshed" if refreshed else "armed",
        "coin":               coin,
        "next":               "/trend-up",
        "window_remaining_s": _window_remaining(coin, "buy"),
    })


@app.route("/trend-up", methods=["POST"])
@limiter.limit("30 per minute")
def trend_up():
    data = request.get_json(silent=True) or {}
    if not verify_token(data):
        return jsonify({"error": "Unauthorized"}), 401

    coin = normalize_coin(data.get("coin") or data.get("pair", "SOL"))
    with lock:
        if not _is_armed(coin, "buy"):
            logger.info(f"[{coin}] trend-up ignored -- no active buy signal")
            return jsonify({"status": "skip", "message": f"No active buy signal for {coin}"}), 200
        _disarm(coin, "buy")

    def _execute_long(c=coin):
        if COOLDOWN_SECONDS > 0 and time.time() < cooldown_until.get(c, 0):
            remaining = int(cooldown_until[c] - time.time())
            logger.info(f"[{c}] Trend-up ignored -- cooldown active ({remaining}s remaining)")
            notifier.send(f"[{c}] Trend-up ignored -- cooldown active ({remaining}s remaining)")
            return
        try:
            positions = trader.get_positions()
            has_short = any(p["coin"] == c and p["side"] == "short" for p in positions)
            if has_short:
                logger.info(f"[{c}] Trend UP confirmed -- flipping SHORT to LONG")
                notifier.send(f"[{c}] Trend up! Closing SHORT and opening LONG...")
                result = trader.flip_to_long(c)
                action = "flip_to_long"
            else:
                logger.info(f"[{c}] Trend UP confirmed -- executing LONG")
                notifier.send(f"[{c}] Trend up! Opening LONG on Hyperliquid...")
                result = trader.open_long(c)
                action = "open_long"
            if COOLDOWN_SECONDS > 0:
                cooldown_until[c] = time.time() + COOLDOWN_SECONDS
            notifier.send(f"[{c}] ✅ Trade confirmed: {action} | {result}")
        except Exception as e:
            logger.error(f"[{c}] Background trade execution failed: {e}")
            notifier.send(f"[{c}] ❌ Trade FAILED: {e}")

    threading.Thread(target=_execute_long, daemon=True).start()
    return jsonify({"status": "acknowledged", "coin": coin, "message": "Trade queued -- watch Telegram for confirmation"}), 202


# -- SELL flow ----------------------------------------------------------------

@app.route("/sell-signal", methods=["POST"])
@limiter.limit("30 per minute")
def sell_signal():
    data = request.get_json(silent=True) or {}
    if not verify_token(data):
        return jsonify({"error": "Unauthorized"}), 401

    coin = normalize_coin(data.get("coin") or data.get("pair", "SOL"))
    with lock:
        refreshed = _arm(coin, "sell")

    if refreshed:
        logger.info(f"[{coin}] SELL signal refreshed -- still waiting for trend-down")
        notifier.send(f"[{coin}] SELL signal refreshed -- still waiting for trend turn down")
    else:
        logger.info(f"[{coin}] SELL signal armed -- waiting for trend-down")
        notifier.send(f"[{coin}] SELL signal armed -- waiting for trend turn down")

    return jsonify({
        "status":             "refreshed" if refreshed else "armed",
        "coin":               coin,
        "next":               "/trend-down",
        "window_remaining_s": _window_remaining(coin, "sell"),
    })


@app.route("/trend-down", methods=["POST"])
@limiter.limit("30 per minute")
def trend_down():
    data = request.get_json(silent=True) or {}
    if not verify_token(data):
        return jsonify({"error": "Unauthorized"}), 401

    coin = normalize_coin(data.get("coin") or data.get("pair", "SOL"))
    with lock:
        if not _is_armed(coin, "sell"):
            logger.info(f"[{coin}] trend-down ignored -- no active sell signal")
            return jsonify({"status": "skip", "message": f"No active sell signal for {coin}"}), 200
        _disarm(coin, "sell")

    def _execute_short(c=coin):
        if COOLDOWN_SECONDS > 0 and time.time() < cooldown_until.get(c, 0):
            remaining = int(cooldown_until[c] - time.time())
            logger.info(f"[{c}] Trend-down ignored -- cooldown active ({remaining}s remaining)")
            notifier.send(f"[{c}] Trend-down ignored -- cooldown active ({remaining}s remaining)")
            return
        try:
            if SELL_MODE == "short":
                logger.info(f"[{c}] Trend DOWN confirmed -- flipping to SHORT")
                notifier.send(f"[{c}] Trend down! Closing long and opening SHORT...")
                result = trader.flip_to_short(c)
                action = "flip_to_short"
            elif SELL_MODE == "open_short":
                logger.info(f"[{c}] Trend DOWN confirmed -- opening SHORT")
                notifier.send(f"[{c}] Trend down! Opening SHORT...")
                result = trader.open_short(c)
                action = "open_short"
            else:
                pct_label = f"{int(SELL_PCT * 100)}%"
                logger.info(f"[{c}] Trend DOWN confirmed -- closing {pct_label} of long")
                notifier.send(f"[{c}] Trend down! Closing {pct_label} long...")
                result = trader.close_long(c, pct=SELL_PCT)
                action = f"close_long_{pct_label}"
            if COOLDOWN_SECONDS > 0:
                cooldown_until[c] = time.time() + COOLDOWN_SECONDS
            notifier.send(f"[{c}] ✅ Trade confirmed: {action} | {result}")
        except Exception as e:
            logger.error(f"[{c}] Background trade execution failed: {e}")
            notifier.send(f"[{c}] ❌ Trade FAILED: {e}")

    threading.Thread(target=_execute_short, daemon=True).start()
    return jsonify({"status": "acknowledged", "coin": coin, "message": "Trade queued -- watch Telegram for confirmation"}), 202


# -- Manual overrides ---------------------------------------------------------

@app.route("/emergency-close", methods=["POST"])
@limiter.limit("10 per minute")
def emergency_close():
    data = request.get_json(silent=True) or {}
    if not verify_token(data):
        return jsonify({"error": "Unauthorized"}), 401

    logger.warning("EMERGENCY CLOSE triggered -- closing all positions")
    notifier.send("EMERGENCY CLOSE triggered -- closing all positions to USDC")

    results = {}
    errors  = {}

    try:
        positions = trader.get_positions()
    except Exception as e:
        logger.error(f"Emergency close: failed to fetch positions: {e}")
        return jsonify({"error": f"Could not fetch positions: {e}"}), 500

    for pos in positions:
        coin = pos.get("coin")
        side = pos.get("side", "").lower()
        if not coin:
            continue
        try:
            if side == "long":
                results[coin] = trader.close_long(coin)
            elif side == "short":
                results[coin] = trader.close_short(coin)
            else:
                results[coin] = "skipped (unknown side)"
        except Exception as e:
            logger.error(f"Emergency close: failed to close {coin} {side}: {e}")
            errors[coin] = str(e)

    with lock:
        for coin in armed:
            armed[coin] = {"buy": None, "sell": None}

    emergency_cooldown = max(COOLDOWN_SECONDS, 60)
    for coin in results:
        cooldown_until[coin] = time.time() + emergency_cooldown

    summary = f"Closed: {list(results.keys())} | Errors: {errors or 'none'}"
    logger.warning(f"Emergency close complete -- {summary}")
    notifier.send(f"Emergency close complete -- {summary}")

    return jsonify({
        "status":             "emergency_close_executed",
        "closed":             results,
        "errors":             errors,
        "cooldown_applied_s": emergency_cooldown,
    })


@app.route("/reset", methods=["POST"])
@limiter.limit("10 per minute")
def reset_signals():
    data = request.get_json(silent=True) or {}
    if not verify_token(data):
        return jsonify({"error": "Unauthorized"}), 401

    with lock:
        disarmed = {
            coin: [side for side, ts in sides.items() if ts is not None]
            for coin, sides in armed.items()
        }
        for coin in armed:
            armed[coin] = {"buy": None, "sell": None}

    logger.info(f"Manual RESET -- signals disarmed: {disarmed}")
    notifier.send("Manual reset -- all signals disarmed. Open positions and trailing stop unaffected.")

    return jsonify({
        "status":   "reset",
        "disarmed": {coin: sides for coin, sides in disarmed.items() if sides},
        "message":  "Signals cleared. Positions and trailing stop still active.",
    })


# -- Status / info ------------------------------------------------------------

@app.route("/status", methods=["GET"])
def status():
    now = time.time()
    out = {}
    with lock:
        for coin, sides in armed.items():
            coin_status = {}
            for side, ts in sides.items():
                if ts is None:
                    coin_status[side] = "idle"
                elif WINDOW_SECONDS > 0 and now - ts > WINDOW_SECONDS:
                    coin_status[side] = "expired"
                else:
                    age = int(now - ts)
                    coin_status[side] = {
                        "armed":              True,
                        "age_s":              age,
                        "window_remaining_s": -1 if WINDOW_SECONDS == 0 else max(0, WINDOW_SECONDS - age),
                        "waiting_for":        "trend-up" if side == "buy" else "trend-down",
                    }
            out[coin] = coin_status
    cooldowns = {
        coin: max(0, int(ts - now))
        for coin, ts in cooldown_until.items()
        if ts > now
    }
    return jsonify({
        "coins":               out,
        "cooldowns":           cooldowns,
        "window_seconds":      WINDOW_SECONDS,
        "sell_mode":           SELL_MODE,
        "sell_pct":            SELL_PCT,
        "cooldown_seconds":    COOLDOWN_SECONDS,
        "rearm_after_stop":    REARM_AFTER_STOP,
        "rearm_delay_seconds": REARM_DELAY_SECONDS,
    })


@app.route("/positions", methods=["GET"])
def positions():
    try:
        return jsonify({"positions": trader.get_positions()})
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    try:
        equity = trader._equity()
        return jsonify({
            "status":       "ok",
            "hl_connected": True,
            "equity_usdc":  round(equity, 2),
        })
    except Exception as e:
        logger.warning(f"Health check: HL API unreachable: {e}")
        return jsonify({
            "status":       "degraded",
            "hl_connected": False,
            "error":        str(e),
        }), 503


# -- Entrypoint (dev only -- Gunicorn ignores this) ---------------------------
if __name__ == "__main__":
    logger.info("PTOS Signal Tracker (Hyperliquid) starting in dev mode...")
    app.run(host="0.0.0.0", port=5000, debug=False)
