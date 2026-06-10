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
  GET  /trades           Recent trade fill history
  GET  /health           Health check
  GET  /dashboard        Web dashboard
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

# -- Dashboard HTML -----------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PTOS Dashboard</title>
<style>
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --border: #30363d;
  --text: #e6edf3;
  --muted: #8b949e;
  --green: #3fb950;
  --red: #f85149;
  --blue: #58a6ff;
  --yellow: #d29922;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; font-size: 14px; }
.container { max-width: 1300px; margin: 0 auto; padding: 20px; }
header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
header h1 { font-size: 20px; font-weight: 600; color: var(--blue); }
.header-meta { display: flex; gap: 16px; align-items: center; color: var(--muted); font-size: 12px; }
.refresh-btn { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; }
.refresh-btn:hover { border-color: var(--blue); color: var(--blue); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px; margin-bottom: 20px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.card-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 8px; }
.card-value { font-size: 22px; font-weight: 600; }
.green { color: var(--green); }
.red { color: var(--red); }
.blue { color: var(--blue); }
.muted { color: var(--muted); }
.section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 16px; overflow: hidden; }
.section-header { padding: 10px 16px; border-bottom: 1px solid var(--border); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted); display: flex; justify-content: space-between; align-items: center; }
table { width: 100%; border-collapse: collapse; }
th { padding: 10px 16px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 10px 16px; border-bottom: 1px solid rgba(48,54,61,0.6); font-size: 13px; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255,255,255,0.02); }
.empty { padding: 28px 16px; text-align: center; color: var(--muted); font-size: 13px; }
.coin-row { display: flex; align-items: center; gap: 16px; padding: 12px 16px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.coin-row:last-child { border-bottom: none; }
.coin-name { font-weight: 700; font-size: 15px; min-width: 60px; }
.badge { padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.badge-buy { background: rgba(63,185,80,0.12); color: var(--green); border: 1px solid rgba(63,185,80,0.3); }
.badge-sell { background: rgba(248,81,73,0.12); color: var(--red); border: 1px solid rgba(248,81,73,0.3); }
.badge-idle { background: rgba(139,148,158,0.08); color: var(--muted); border: 1px solid var(--border); }
.badge-detail { color: var(--muted); font-size: 11px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot-green { background: var(--green); box-shadow: 0 0 5px var(--green); }
.dot-red { background: var(--red); }
.pnl-pos { color: var(--green); }
.pnl-neg { color: var(--red); }
.pnl-zero { color: var(--muted); }
.side-long { color: var(--green); font-weight: 600; }
.side-short { color: var(--red); font-weight: 600; }
.dir-open-long, .dir-buy { color: var(--green); }
.dir-open-short, .dir-sell { color: var(--red); }
.dir-close-long { color: var(--muted); }
.dir-close-short { color: var(--muted); }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>&#9889; PTOS Dashboard</h1>
    <div class="header-meta">
      <span id="last-updated">Loading...</span>
      <span id="countdown"></span>
      <button class="refresh-btn" onclick="loadAll()">&#8635; Refresh</button>
    </div>
  </header>

  <div class="cards">
    <div class="card">
      <div class="card-label">Equity</div>
      <div class="card-value blue" id="equity">—</div>
    </div>
    <div class="card">
      <div class="card-label">HL Status</div>
      <div class="card-value" id="hl-status">—</div>
    </div>
    <div class="card">
      <div class="card-label">Open Positions</div>
      <div class="card-value" id="pos-count">—</div>
    </div>
    <div class="card">
      <div class="card-label">Unrealized PnL</div>
      <div class="card-value" id="unrealized-pnl">—</div>
    </div>
    <div class="card">
      <div class="card-label">Realized PnL (all)</div>
      <div class="card-value" id="total-pnl">—</div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><span>Bot State</span></div>
    <div id="bot-state"><div class="empty">Loading...</div></div>
  </div>

  <div class="section">
    <div class="section-header"><span>Open Positions</span></div>
    <table>
      <thead><tr>
        <th>Coin</th><th>Side</th><th>Size</th><th>Entry Price</th>
        <th>Unrealized PnL</th><th>Liq Price</th><th>Margin Used</th>
      </tr></thead>
      <tbody id="positions-body"><tr><td colspan="7" class="empty">Loading...</td></tr></tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-header">
      <span>Trade History</span>
      <span id="trade-count" style="color:var(--muted);font-size:11px;"></span>
    </div>
    <table>
      <thead><tr>
        <th>Time</th><th>Coin</th><th>Direction</th><th>Fill Price</th>
        <th>Size</th><th>Realized PnL</th><th>Fee</th><th>Net PnL</th>
      </tr></thead>
      <tbody id="trades-body"><tr><td colspan="8" class="empty">Loading...</td></tr></tbody>
    </table>
  </div>
</div>

<script>
var refreshSecs = 30;
var timerHandle = null;

function fmt(n, dec) {
  dec = (dec === undefined) ? 2 : dec;
  if (n === null || n === undefined || isNaN(Number(n))) return '—';
  return Number(n).toLocaleString('en-US', {minimumFractionDigits: dec, maximumFractionDigits: dec});
}

function fmtUsd(n, dec) {
  var v = Number(n);
  if (isNaN(v)) return '—';
  return '$' + fmt(v, dec === undefined ? 2 : dec);
}

function fmtPnl(n) {
  var v = Number(n);
  if (isNaN(v)) return '<span class="pnl-zero">—</span>';
  if (v === 0) return '<span class="pnl-zero">$0.00</span>';
  var sign = v > 0 ? '+' : '';
  var cls = v > 0 ? 'pnl-pos' : 'pnl-neg';
  return '<span class="' + cls + '">' + sign + '$' + fmt(Math.abs(v)) + '</span>';
}

function fmtTime(ms) {
  var d = new Date(ms);
  var dd = String(d.getDate()).padStart(2,'0');
  var mm = String(d.getMonth()+1).padStart(2,'0');
  var hh = String(d.getHours()).padStart(2,'0');
  var mi = String(d.getMinutes()).padStart(2,'0');
  var ss = String(d.getSeconds()).padStart(2,'0');
  return dd + '/' + mm + ' ' + hh + ':' + mi + ':' + ss;
}

function dirClass(dir) {
  if (!dir) return '';
  var d = dir.toLowerCase();
  if (d.indexOf('open long') >= 0 || d === 'buy') return 'dir-open-long';
  if (d.indexOf('open short') >= 0 || d === 'sell') return 'dir-open-short';
  if (d.indexOf('close long') >= 0) return 'dir-close-long';
  if (d.indexOf('close short') >= 0) return 'dir-close-short';
  return '';
}

function isClose(dir) {
  if (!dir) return false;
  return dir.toLowerCase().indexOf('close') >= 0;
}

async function loadHealth() {
  try {
    var r = await fetch('/health');
    var d = await r.json();
    document.getElementById('equity').textContent = d.equity_usdc ? fmtUsd(d.equity_usdc) : '—';
    var el = document.getElementById('hl-status');
    if (d.hl_connected) {
      el.innerHTML = '<span class="dot dot-green" style="margin-right:6px"></span>Connected';
      el.className = 'card-value green';
    } else {
      el.innerHTML = '<span class="dot dot-red" style="margin-right:6px"></span>Offline';
      el.className = 'card-value red';
    }
  } catch(e) {
    document.getElementById('hl-status').innerHTML = '<span class="red">Error</span>';
  }
}

async function loadStatus() {
  try {
    var r = await fetch('/status');
    var d = await r.json();
    var coins = d.coins || {};
    var keys = Object.keys(coins);
    var el = document.getElementById('bot-state');
    if (keys.length === 0) {
      el.innerHTML = '<div class="empty">No signals armed — bot idle</div>';
      return;
    }
    el.innerHTML = keys.map(function(coin) {
      var s = coins[coin];
      return '<div class="coin-row"><span class="coin-name">' + coin + '</span>' +
             '<span>BUY: ' + renderBadge(s.buy, 'buy') + '</span>' +
             '<span>SELL: ' + renderBadge(s.sell, 'sell') + '</span></div>';
    }).join('');
  } catch(e) {}
}

function renderBadge(state, side) {
  if (!state || state === 'idle' || state === 'expired') {
    return '<span class="badge badge-idle">' + (state || 'idle') + '</span>';
  }
  if (state.armed) {
    var age = state.age_s < 60 ? state.age_s + 's' : Math.round(state.age_s / 60) + 'm';
    var waiting = state.waiting_for || (side === 'buy' ? 'trend-up' : 'trend-down');
    var cls = side === 'buy' ? 'badge-buy' : 'badge-sell';
    return '<span class="badge ' + cls + '">● ARMED</span>' +
           '<span class="badge-detail"> waiting for ' + waiting + ' · ' + age + ' ago</span>';
  }
  return '<span class="badge badge-idle">idle</span>';
}

async function loadPositions() {
  try {
    var r = await fetch('/positions');
    var d = await r.json();
    var positions = d.positions || [];
    document.getElementById('pos-count').textContent = positions.length;
    var totalUpnl = positions.reduce(function(s, p) { return s + (p.unrealized_pnl || 0); }, 0);
    var upnlEl = document.getElementById('unrealized-pnl');
    upnlEl.innerHTML = fmtPnl(totalUpnl);
    upnlEl.className = 'card-value';
    var tbody = document.getElementById('positions-body');
    if (positions.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No open positions</td></tr>';
      return;
    }
    tbody.innerHTML = positions.map(function(p) {
      var sideCls = p.side === 'long' ? 'side-long' : 'side-short';
      var liq = p.liquidation_px ? fmtUsd(p.liquidation_px) : '—';
      return '<tr><td><strong>' + p.coin + '</strong></td>' +
             '<td class="' + sideCls + '">' + p.side.toUpperCase() + '</td>' +
             '<td>' + fmt(p.size, 4) + '</td>' +
             '<td>' + fmtUsd(p.entry_price) + '</td>' +
             '<td>' + fmtPnl(p.unrealized_pnl) + '</td>' +
             '<td>' + liq + '</td>' +
             '<td>' + fmtUsd(p.margin_used) + '</td></tr>';
    }).join('');
  } catch(e) {}
}

async function loadTrades() {
  try {
    var r = await fetch('/trades?limit=200');
    var d = await r.json();
    var fills = d.fills || [];
    var tbody = document.getElementById('trades-body');
    if (fills.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No trade history</td></tr>';
      document.getElementById('total-pnl').textContent = '—';
      return;
    }
    var totalPnl = 0;
    fills.forEach(function(f) { totalPnl += parseFloat(f.closedPnl || 0); });
    var pnlEl = document.getElementById('total-pnl');
    pnlEl.innerHTML = fmtPnl(totalPnl);
    pnlEl.className = 'card-value';
    document.getElementById('trade-count').textContent = fills.length + ' fills';
    tbody.innerHTML = fills.map(function(f) {
      var pnl = parseFloat(f.closedPnl || 0);
      var fee = parseFloat(f.fee || 0);
      var net = pnl - fee;
      var dir = f.dir || (f.side === 'B' ? 'Buy' : 'Sell');
      var close = isClose(dir);
      var dCls = dirClass(dir);
      return '<tr>' +
             '<td style="white-space:nowrap;color:var(--muted)">' + fmtTime(f.time) + '</td>' +
             '<td><strong>' + f.coin + '</strong></td>' +
             '<td class="' + dCls + '">' + dir + '</td>' +
             '<td>' + fmtUsd(parseFloat(f.px)) + '</td>' +
             '<td>' + fmt(parseFloat(f.sz), 4) + '</td>' +
             '<td>' + (close ? fmtPnl(pnl) : '<span class="muted">—</span>') + '</td>' +
             '<td class="pnl-neg">-$' + fmt(Math.abs(fee)) + '</td>' +
             '<td>' + (close ? fmtPnl(net) : '<span class="muted">—</span>') + '</td>' +
             '</tr>';
    }).join('');
  } catch(e) {}
}

async function loadAll() {
  document.getElementById('last-updated').textContent = 'Refreshing...';
  refreshSecs = 30;
  await Promise.all([loadHealth(), loadStatus(), loadPositions(), loadTrades()]);
  document.getElementById('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

function startTimer() {
  if (timerHandle) clearInterval(timerHandle);
  timerHandle = setInterval(function() {
    refreshSecs--;
    document.getElementById('countdown').textContent = 'Auto-refresh in ' + refreshSecs + 's';
    if (refreshSecs <= 0) { loadAll(); }
  }, 1000);
}

loadAll();
startTimer();
</script>
</body>
</html>"""

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


@app.route("/trades", methods=["GET"])
def trades():
    try:
        limit = min(int(request.args.get("limit", 200)), 500)
        fills = trader.get_fills(limit=limit)
        return jsonify({"fills": fills, "count": len(fills)})
    except Exception as e:
        logger.error(f"Failed to fetch trade history: {e}")
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


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


# -- Entrypoint (dev only -- Gunicorn ignores this) ---------------------------
if __name__ == "__main__":
    logger.info("PTOS Signal Tracker (Hyperliquid) starting in dev mode...")
    app.run(host="0.0.0.0", port=5000, debug=False)
