# PTOS -- Hyperliquid Edition

A TradingView webhook signal sequencer that executes perpetual trades directly
on **Hyperliquid** using a 2-step confirmation system. Trades only fire when a
directional signal is confirmed by a trend change -- filtering noise and reducing
false entries.

> **Always start on testnet (`HL_TESTNET=true`) before using real funds.**

---

## Features

- **2-step confirmation** -- buy/sell signal arms the bot; trend change pulls the trigger
- **Multi-signal support** -- repeated signals refresh the window, never cancel it
- **No signal expiry** -- signals stay armed until trend confirms (configurable)
- **Independent per-coin tracking** -- SOL and BTC sequences never interfere
- **Dual-layer trailing stop** -- native resting stop on HL (survives container downtime) + polling backstop
- **Per-coin trailing stop override** -- set a different trailing stop % per coin (e.g. HYPE wider than BTC)
- **Stop loss from entry** -- hard exit if price drops X% from your entry price (catches fakeouts before trailing stop moves)
- **Max hold time** -- auto-close any position after N seconds regardless of price
- **Flip detection** -- if a short is open when a buy confirms, closes the short and opens a long in one step (and vice versa)
- **Async trade execution** -- webhooks return 202 immediately; trade executes in background with Telegram confirmation
- **Cooldown after close** -- prevents whipsaw re-entries after a stop or exit
- **Position size safety cap** -- hard ceiling on notional exposure per trade
- **SELL_MODE toggle** -- flip to short, exit to USDC, or open short alongside long -- your choice
- **Manual overrides** -- `/emergency-close` and `/reset` endpoints for instant control
- **Retry logic** -- all Hyperliquid API calls retry up to 3x with back-off
- **Rate limiting** -- webhook endpoints capped at 30 req/min per IP
- **Telegram notifications** -- every signal step and trade execution
- **Single Docker container** -- no Freqtrade, no exchange API keys needed

---

## How it works

**Buy flow:**
```
/buy-signal   <- your buy indicator fires (repeats refresh the window)
     |  bot armed, waiting...
/trend-up     <- trend confirms upward -> LONG opened (or SHORT flipped to LONG)
                 returns 202 immediately -- watch Telegram for trade confirmation
```

**Sell flow:**
```
/sell-signal  <- your sell indicator fires (repeats refresh the window)
     |  bot armed, waiting...
/trend-down   <- trend confirms downward -> long closed / short opened
                 returns 202 immediately -- watch Telegram for trade confirmation
```

> `/trend-up` and `/trend-down` execute trades asynchronously to avoid TradingView
> webhook timeouts. The endpoint returns `202 Accepted` instantly; the trade runs in
> the background and Telegram sends a confirmation (or error) when complete.

### Trailing stop (dual-layer)

Once in a position, PTOS places a **native reduce-only stop-loss order directly
on Hyperliquid**. This order:

- Executes instantly when price hits it (no polling delay)
- **Survives container restarts and downtime** -- it lives on the exchange
- Updates automatically (cancel + replace) each time the trailing peak moves

A **polling backstop loop** (every `MONITOR_INTERVAL_SECONDS`) also runs as a
safety net in case the native stop fails to place or gets missed. Both layers
track the same trailing peak -- long positions trail the highest price reached,
shorts trail the lowest. After closing, a configurable cooldown prevents
immediate re-entry.

### Sell behaviour

| `SELL_MODE`  | What happens at `/trend-down`                     |
|--------------|---------------------------------------------------|
| `short`      | Close any open long, then open a short (default)  |
| `close_long` | Exit the long to USDC -- no short exposure        |
| `open_short` | Open a short without touching the long (advanced) |

---

## Endpoints

### TradingView webhooks

| Endpoint        | Method | Description                                    |
|-----------------|--------|------------------------------------------------|
| `/buy-signal`   | POST   | Arm buy watch (each fire refreshes the window) |
| `/trend-up`     | POST   | Trend confirmed up -- open long                |
| `/sell-signal`  | POST   | Arm sell watch (each fire refreshes the window)|
| `/trend-down`   | POST   | Trend confirmed down -- sell / short           |

### Manual overrides

These require your `SECRET_TOKEN` (via `X-Webhook-Secret` header or `token` in JSON body).

| Endpoint           | Method | Description                                                   |
|--------------------|--------|---------------------------------------------------------------|
| `/emergency-close` | POST   | Close ALL positions to USDC, disarm all signals, set cooldown |
| `/reset`           | POST   | Disarm all signals only -- positions and trailing stop stay   |

```bash
# Panic button -- close everything
curl -X POST http://YOUR_IP:5001/emergency-close \
  -H "X-Webhook-Secret: YOUR_SECRET_TOKEN"

# Disarm only -- stay in trade
curl -X POST http://YOUR_IP:5001/reset \
  -H "X-Webhook-Secret: YOUR_SECRET_TOKEN"
```

### Status / info

| Endpoint      | Method | Description                        |
|---------------|--------|------------------------------------|
| `/status`     | GET    | Armed state, cooldowns, config     |
| `/positions`  | GET    | Live Hyperliquid positions         |
| `/trades`     | GET    | Bot trade log (only trades PTOS executed, persisted across restarts) |
| `/health`     | GET    | HL connectivity + equity check (used by Docker). Returns `{"status":"ok","hl_connected":true,"equity_usdc":123.45}` or `503` if HL unreachable |
| `/dashboard`  | GET    | Web dashboard — equity, bot state, open positions, trade log |

---

## Configuration

All settings live in `.env`. Copy the example and fill in your values:

```bash
cp .env.example .env
```

### Required

| Setting              | Description                                                        |
|----------------------|--------------------------------------------------------------------|
| `HL_PRIVATE_KEY`     | Private key of your **API sub-wallet** (generated in HL Settings)  |
| `HL_WALLET_ADDRESS`  | Public address of your **main wallet** (where your USDC lives)     |
| `SECRET_TOKEN`       | Random string -- paste into every TradingView alert                |

### Network

| Setting      | Default | Description                                 |
|--------------|---------|---------------------------------------------|
| `HL_TESTNET` | `true`  | `true` = paper trading · `false` = mainnet  |

### Position sizing

| Setting                 | Default | Description                                    |
|-------------------------|---------|------------------------------------------------|
| `POSITION_SIZE_PCT`     | `0.10`  | Fraction of equity per trade (0.10 = 10%)      |
| `LEVERAGE`              | `3`     | Leverage multiplier (1 = no leverage)          |
| `LEVERAGE_MODE`         | `cross` | `cross` or `isolated`                          |
| `MAX_POSITION_SIZE_PCT` | `0.5`   | Hard cap on notional exposure (safety ceiling) |

### Sell behaviour

| Setting     | Default | Description                                            |
|-------------|---------|--------------------------------------------------------|
| `SELL_MODE` | `short` | `short` · `close_long` · `open_short`                  |
| `SELL_PCT`  | `1.0`   | Fraction of long to close (only for `close_long` mode) |

### Trailing stop

| Setting                    | Default | Description                                           |
|----------------------------|---------|-------------------------------------------------------|
| `TRAILING_STOP_PCT`        | `0.05`  | Close if price pulls back this % from peak (0 = off)  |
| `{COIN}_TRAILING_STOP_PCT` | —       | Per-coin override (e.g. `HYPE_TRAILING_STOP_PCT=0.07`)|
| `MONITOR_INTERVAL_SECONDS` | `30`    | Polling backstop check interval (seconds)             |
| `REARM_AFTER_STOP`         | `false` | Auto re-arm signal after stop-out (see below)         |
| `REARM_DELAY_SECONDS`      | `3600`  | Seconds to wait before re-arming (default 1 hour)     |

The trailing stop places a native reduce-only order on HL and updates it as
the peak moves. Set `TRAILING_STOP_PCT=0` to disable both layers entirely.

Per-coin overrides let volatile coins use a wider stop without affecting others:
```
# .env
TRAILING_STOP_PCT=0.05        # default for all coins
HYPE_TRAILING_STOP_PCT=0.08   # HYPE gets a wider stop
BTC_TRAILING_STOP_PCT=0.03    # BTC gets a tighter stop
```

### Stop loss from entry

| Setting          | Default | Description                                                     |
|------------------|---------|-----------------------------------------------------------------|
| `STOP_LOSS_PCT`  | `0`     | Close if price drops this % from entry price (0 = disabled)    |

Fires on immediate losing trades before the trailing stop has moved far enough
to protect you. Good for catching fakeout signals.

Example: enter long at $100 with `STOP_LOSS_PCT=0.05` → exits at $95 regardless
of where the trailing stop peak is.

### Max hold time

| Setting            | Default | Description                                                   |
|--------------------|---------|---------------------------------------------------------------|
| `MAX_HOLD_SECONDS` | `0`     | Auto-close position after this many seconds (0 = disabled)   |

Useful when you don't want to hold a short over a weekend or a long through
a major scheduled event.

| Duration  | `MAX_HOLD_SECONDS` |
|-----------|--------------------|
| 1 day     | `86400`            |
| 3 days    | `259200`           |
| 7 days    | `604800`           |

#### Re-arm after stop-out (`REARM_AFTER_STOP`)

When enabled, if a trailing stop closes a position the bot automatically
re-arms the same signal direction after `REARM_DELAY_SECONDS`. If the trend
confirms again within `WINDOW_SECONDS`, it re-enters without needing a new
TradingView signal.

Example with a long position:
```
Trailing stop fires → long closed
  → wait REARM_DELAY_SECONDS (e.g. 1 hour)
  → buy signal auto re-armed
  → next /trend-up → re-enters long
  → if trend-up never fires within WINDOW_SECONDS → signal expires, stays idle
```

**Recommended settings by timeframe:**

| Timeframe | `REARM_DELAY_SECONDS` | `WINDOW_SECONDS` |
|-----------|-----------------------|-----------------|
| 30m       | `3600` (2 candles)    | `7200`          |
| 4h        | `14400` (1 candle)    | `28800`         |
| 1d        | `86400` (1 candle)    | `172800`        |

> **Note:** On lower timeframes (30m), a wider `TRAILING_STOP_PCT` is usually
> safer than enabling re-arm. Re-arm is better suited to 4h+ charts where
> trends are cleaner and fakeouts are rarer.

### Cooldown & safety

| Setting            | Default | Description                                                |
|--------------------|---------|------------------------------------------------------------|
| `COOLDOWN_SECONDS` | `0`     | Wait this long after any close before accepting new trades |

### Signal window

| Setting          | Default | Description                                               |
|------------------|---------|-----------------------------------------------------------|
| `WINDOW_SECONDS` | `0`     | Max seconds a signal stays armed. `0` = no expiry (default) |

With `WINDOW_SECONDS=0` (default), a signal stays armed indefinitely until the
trend confirms. Set a value if you want signals to expire:

| Timeframe | Candles | `WINDOW_SECONDS` |
|-----------|---------|-----------------|
| 1h        | 10      | 36000           |
| 4h        | 10      | 144000          |
| 1d        | 5       | 432000          |

### Telegram (optional)

| Setting             | Description                  |
|---------------------|------------------------------|
| `TELEGRAM_TOKEN`    | Bot token from @BotFather    |
| `TELEGRAM_CHAT_ID`  | Your chat/channel ID         |

---

## Setup

### 1. Get your Hyperliquid keys

Hyperliquid uses two separate values that work together:

**Step 1 -- Generate an API wallet**

In the Hyperliquid app: **Settings -> API -> Generate API wallet**

This creates a sub-wallet specifically for trading. Copy its **private key**
into `HL_PRIVATE_KEY` in your `.env`. This key has limited permissions --
it can place and cancel orders but **cannot withdraw funds**. It is safe to
use in the bot.

> Never put your main wallet's private key into the bot. If the server is
> ever compromised, a main wallet key gives full withdrawal access.

**Step 2 -- Add your main wallet address**

Your USDC balance lives on your **main wallet**, not the API sub-wallet.
Copy your main wallet's public address (shown in the top-right of
app.hyperliquid.xyz) into `HL_WALLET_ADDRESS` in your `.env`.

The bot signs orders with the API key but executes them against your main
wallet's balance -- this is how Hyperliquid's API wallet system is designed
to work.

```
HL_PRIVATE_KEY=0xYOUR_API_WALLET_PRIVATE_KEY
HL_WALLET_ADDRESS=0xYOUR_MAIN_WALLET_PUBLIC_ADDRESS
```

> If `HL_WALLET_ADDRESS` is left blank, the bot trades against the API
> wallet's own balance (which is $0) and every trade will fail with a
> "size below minimum" error.

**Step 3 -- Set your account type to Manual**

Go to **app.hyperliquid.xyz/portfolio** and click the **Account Type** button.

> This is the critical step most people miss.

Hyperliquid has three account types. The bot requires **Manual** mode:

| Account Type     | What it does                                                     |
|------------------|------------------------------------------------------------------|
| Unified          | Spot and perp balances are separate -- perp API shows $0 even if you have USDC in spot |
| Portfolio Margin | Spot assets used as perp collateral -- not compatible with this bot |
| **Manual**       | **Separate perp balance the API can read correctly -- use this** |

After switching to Manual, go to **Balances -> Transfer** and move your USDC
from spot into your perp account. The bot reads perp margin balance only --
if it shows $0 in the API, your trades will fail with "size below minimum".

You can verify the bot sees your balance with:
```bash
curl -X POST https://api.hyperliquid.xyz/info \
  -H "Content-Type: application/json" \
  -d '{"type": "clearinghouseState", "user": "0xYOUR_MAIN_WALLET_ADDRESS"}'
```

Look for `"accountValue"` -- it should match your perp balance.

### 2. Configure

```bash
cp .env.example .env
# edit .env with your values
```

Generate a random secret token:
```bash
openssl rand -hex 24
```

### 3. Start on testnet first

```bash
docker compose up -d
docker compose logs -f
```

Verify it's running:
```bash
curl http://localhost:5001/health
curl http://localhost:5001/status
curl http://localhost:5001/positions
```

> If you get `429 Too Many Requests` on a webhook, flask-limiter is working
> correctly -- TradingView retries are being rate-limited.

### 4. Set up TradingView alerts

Create **4 alerts** on your chart. Same JSON body for all, different URL each.

**Message body:**
```json
{"coin": "SOL", "token": "YOUR_SECRET_TOKEN"}
```

| Alert            | Webhook URL                             |
|------------------|-----------------------------------------|
| Buy indicator    | `http://YOUR_IP:5001/buy-signal`        |
| Trend turns up   | `http://YOUR_IP:5001/trend-up`          |
| Sell indicator   | `http://YOUR_IP:5001/sell-signal`       |
| Trend turns down | `http://YOUR_IP:5001/trend-down`        |

Set alert trigger to **"Once per bar close"**.

> Hyperliquid uses coin symbols: `"SOL"`, `"BTC"`, `"ETH"`.
> `"SOL/USD"` and `"SOL/USDC"` are also accepted and stripped automatically.

### 5. Go live

```bash
# In .env:
HL_TESTNET=false

docker compose restart
```

### Unraid / self-hosted

```bash
mkdir -p /mnt/user/appdata/ptos
cd /mnt/user/appdata/ptos
# Copy all project files here, then:
cp .env.example .env
nano .env          # fill in your values
docker compose up -d
```

To update after code changes:
```bash
docker compose down && docker compose up -d --build
```

---

## Multi-pair

Add separate TradingView alerts per coin. The bot tracks each coin independently:

```json
{"coin": "BTC", "token": "YOUR_SECRET_TOKEN"}
```

---

## Monitoring

### Web dashboard

Open in any browser — auto-refreshes every 30 seconds:

```
http://YOUR_IP:5001/dashboard
```

Shows equity, HL connectivity, bot armed state per coin, open positions with
unrealized PnL, and a full log of every trade PTOS has executed.

### CLI

```bash
# Check signal state, cooldowns, and config
curl http://localhost:5001/status

# Check open positions with PnL
curl http://localhost:5001/positions

# Bot trade log (JSON)
curl http://localhost:5001/trades

# Live logs
docker compose logs -f
```

The `/status` endpoint shows which coins are armed, window remaining,
active cooldowns, and current config.

### Trade log

Every trade PTOS executes is written to `/mnt/user/appdata/ptos/data/ptos_trades.json`
(via the volume mount in `docker-compose.yml`). This file persists across container
rebuilds so trade history is never lost. Only trades the bot executed are logged —
manual trades made directly on Hyperliquid are excluded.

---

## Architecture note

PTOS runs as a single Gunicorn worker with multiple threads. The trailing stop
monitor is a background daemon thread started at module load time. If you
increase `--workers` beyond 1 in the Dockerfile, the monitor would start once
per worker -- keep it at 1.

The native HL stop orders are placed on the exchange, not in memory. They
persist across container restarts. On restart, the monitor detects existing
positions on the first poll and places fresh stop orders from the current price.

---

## Security

- **Never commit `.env`** -- it's in `.gitignore` and `.dockerignore`
- **Use a dedicated wallet** with only trading funds
- **Use HTTPS** in production -- run behind nginx or Caddy with a domain
- **Never grant withdrawal permissions** on any API key
- **`SECRET_TOKEN`** prevents unauthorised webhooks -- use a long random string

---

## Disclaimer

This software is provided for educational and informational purposes only.
It is not financial advice. Trading perpetual futures involves significant
risk of loss including total loss of capital. Past performance is not
indicative of future results. You are solely responsible for your trading
decisions. The authors accept no liability for losses incurred through use
of this software. Never trade with funds you cannot afford to lose.

Use at your own risk.
