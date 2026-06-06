# PTOS — Hyperliquid Edition

A TradingView webhook signal sequencer that executes perpetual trades directly
on **Hyperliquid** using a 2-step confirmation system. Trades only fire when a
directional signal is confirmed by a trend change — filtering noise and reducing
false entries.

> ⚠️ **Always start on testnet (`HL_TESTNET=true`) before using real funds.**

---

## Features

- **2-step confirmation** — buy/sell signal arms the bot; trend change pulls the trigger
- **Multi-signal support** — repeated signals refresh the window, never cancel it
- **Independent per-coin tracking** — SOL and BTC sequences never interfere
- **Trailing stop** — rides winners, exits automatically on meaningful reversals
- **Cooldown after close** — prevents whipsaw re-entries after a stop or exit
- **Position size safety cap** — hard ceiling on notional exposure per trade
- **SELL_MODE toggle** — flip to short, exit to USDC, or pyramid — your choice
- **Retry logic** — all Hyperliquid API calls retry up to 3× with back-off
- **Rate limiting** — webhook endpoints capped at 30 req/min per IP
- **Telegram notifications** — every signal step and trade execution
- **Single Docker container** — no Freqtrade, no exchange API keys needed

---

## How it works

**Buy flow:**
```
/buy-signal   ← your buy indicator fires (repeats refresh the window)
     ↓  bot armed, waiting...
/trend-up     ← trend confirms upward → LONG opened
```

**Sell flow:**
```
/sell-signal  ← your sell indicator fires (repeats refresh the window)
     ↓  bot armed, waiting...
/trend-down   ← trend confirms downward → long closed / short opened
```

The **trailing stop** runs in the background at all times. Once in a position
it tracks the best price reached and closes automatically if price pulls back
by `TRAILING_STOP_PCT`. No upside cap — it lets winners run and only exits on
a meaningful reversal. After closing, a configurable cooldown prevents
immediate re-entry.

### Sell behaviour

| `SELL_MODE`  | What happens at `/trend-down`                     |
|--------------|---------------------------------------------------|
| `short`      | Close any open long, then open a short (default)  |
| `close_long` | Exit the long to USDC — no short exposure          |
| `open_short` | Open a short without touching the long (advanced) |

---

## Endpoints

| Endpoint       | Method | Description                                    |
|----------------|--------|------------------------------------------------|
| `/buy-signal`  | POST   | Arm buy watch (each fire refreshes the timer)  |
| `/trend-up`    | POST   | Trend confirmed up → open long                 |
| `/sell-signal` | POST   | Arm sell watch (each fire refreshes the timer) |
| `/trend-down`  | POST   | Trend confirmed down → sell / short            |
| `/status`      | GET    | Armed state, cooldowns, config                 |
| `/positions`   | GET    | Live Hyperliquid positions                     |
| `/health`      | GET    | Health check (used by Docker)                  |

---

## Configuration

All settings live in `.env`. Copy the example and fill in your values:

```bash
cp .env.example .env
```

### Required

| Setting          | Description                                           |
|------------------|-------------------------------------------------------|
| `HL_PRIVATE_KEY` | Your Hyperliquid wallet private key                   |
| `SECRET_TOKEN`   | Random string — paste into every TradingView alert    |

### Network

| Setting      | Default | Description                                 |
|--------------|---------|---------------------------------------------|
| `HL_TESTNET` | `true`  | `true` = paper trading · `false` = mainnet  |

### Position sizing

| Setting              | Default | Description                                    |
|----------------------|---------|------------------------------------------------|
| `POSITION_SIZE_PCT`  | `0.10`  | Fraction of equity per trade (0.10 = 10%)      |
| `LEVERAGE`           | `3`     | Leverage multiplier                            |
| `LEVERAGE_MODE`      | `cross` | `cross` or `isolated`                         |
| `MAX_POSITION_SIZE_PCT` | `0.5` | Hard cap on notional exposure (safety ceiling) |

### Sell behaviour

| Setting     | Default | Description                                          |
|-------------|---------|------------------------------------------------------|
| `SELL_MODE` | `short` | `short` · `close_long` · `open_short`                |
| `SELL_PCT`  | `1.0`   | Fraction of long to close (only for `close_long` mode) |

### Trailing stop

| Setting                   | Default | Description                                             |
|---------------------------|---------|----------------------------------------------------------|
| `TRAILING_STOP_PCT`       | `0.05`  | Close if price pulls back this % from peak (0 = off)    |
| `MONITOR_INTERVAL_SECONDS`| `30`    | How often to check positions (seconds)                  |

### Cooldown & safety

| Setting           | Default | Description                                                   |
|-------------------|---------|---------------------------------------------------------------|
| `COOLDOWN_SECONDS`| `0`     | Wait this long after any close before accepting new trades    |

### Signal window

| Setting          | Default   | Description                                              |
|------------------|-----------|----------------------------------------------------------|
| `WINDOW_SECONDS` | `144000`  | Max seconds between signal and trend confirmation        |

**Window sizing guide:**

| Timeframe | Candles | `WINDOW_SECONDS` |
|-----------|---------|-----------------|
| 1h        | 10      | 36000           |
| 4h        | 10      | 144000          |
| 1d        | 5       | 432000          |

### Telegram (optional)

| Setting           | Description                  |
|-------------------|------------------------------|
| `TELEGRAM_TOKEN`  | Bot token from @BotFather    |
| `TELEGRAM_CHAT_ID`| Your chat/channel ID         |

---

## Setup

### 1. Get your Hyperliquid private key

> ⚠️ Use a **dedicated trading wallet** with only the funds you intend to
> trade. Never use your main wallet.

In the Hyperliquid app: **Settings → API → Generate API wallet**, then copy
the private key. Or export from MetaMask if that's what you connected.

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

> ℹ️ If you get `429 Too Many Requests` on a webhook, flask-limiter is working
> correctly — TradingView retries are being rate-limited.

### 4. Set up TradingView alerts

Create **4 alerts** on your chart. Same JSON body for all, different URL each.

**Message body:**
```json
{"coin": "SOL", "token": "YOUR_SECRET_TOKEN"}
```

| Alert             | Webhook URL                            |
|-------------------|----------------------------------------|
| Buy indicator     | `http://YOUR_IP:5001/buy-signal`       |
| Trend turns up    | `http://YOUR_IP:5001/trend-up`         |
| Sell indicator    | `http://YOUR_IP:5001/sell-signal`      |
| Trend turns down  | `http://YOUR_IP:5001/trend-down`       |

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

---

## Multi-pair

Add separate TradingView alerts per coin. The bot tracks each coin independently:

```json
{"coin": "BTC", "token": "YOUR_SECRET_TOKEN"}
```

---

## Monitoring

```bash
# Check signal state, cooldowns, and config
curl http://localhost:5001/status

# Check open positions with PnL
curl http://localhost:5001/positions

# Live logs
docker compose logs -f
```

The `/status` endpoint shows which coins are armed, how much window is
remaining, and any active cooldowns.

---

## Architecture note

PTOS runs as a single Gunicorn worker with multiple threads. The trailing stop
monitor is a background daemon thread started at module load time. If you
increase `--workers` beyond 1 in the Dockerfile, the monitor would start once
per worker — keep it at 1.

---

## Security

- **Never commit `.env`** — it's in `.gitignore` and `.dockerignore`
- **Use a dedicated wallet** with only trading funds
- **Use HTTPS** in production — run behind nginx or Caddy with a domain
- **Never grant withdrawal permissions** on any API key
- **`SECRET_TOKEN`** prevents unauthorised webhooks — use a long random string

---

## ⚠️ Disclaimer

This software is provided for educational and informational purposes only.
It is not financial advice. Trading perpetual futures involves significant
risk of loss including total loss of capital. Past performance is not
indicative of future results. You are solely responsible for your trading
decisions. The authors accept no liability for losses incurred through use
of this software. Never trade with funds you cannot afford to lose.

Use at your own risk.
