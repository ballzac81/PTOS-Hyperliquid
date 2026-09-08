# HTF bias filter

Trade the lower timeframe (e.g. 2H). Send bias from a higher timeframe (e.g. 4H) to `/htf-trend`.

## Behaviour (after `htf_exit.patch` + `htf_pending.patch`)

| HTF bias | LTF signal | Result |
|----------|------------|--------|
| bull | trend-up | Open / flip long |
| bull | trend-down | Close long only (no short). SELL stays pending |
| bear | trend-down | Open / flip short (per `SELL_MODE`) |
| bear | trend-up | Close short only (no long). BUY stays pending |
| flips to match a pending signal | `/htf-trend` | Pending trade fires if still inside `HTF_PENDING_SECONDS` |

## Env

```env
HTF_FILTER_ENABLED=true
HTF_PENDING_SECONDS=43200   # 12h — good for 4H bias + 2H entries
HTF_STALE_SECONDS=0         # 0 = off. 129600 = 36h freshness
```

- **Pending:** blocked LTF signal stays armed until HTF matches or the window expires.
- **Freshness:** if the last `/htf-trend` webhook is older than `HTF_STALE_SECONDS`, treat bias as neutral for *new* confirmations. Does not auto-fire both sides.
- On 4H, keep freshness **off** if your TradingView alert only fires when the trend changes.

## TradingView

Same JSON as other alerts, plus `bias`:

```json
{"coin": "HYPE", "token": "YOUR_SECRET_TOKEN", "bias": "bull"}
```

| Alert | URL | Body |
|-------|-----|------|
| 4H turns bull | `https://ptos.ballzac.uk/htf-trend` | `bias: bull` |
| 4H turns bear | `https://ptos.ballzac.uk/htf-trend` | `bias: bear` |

Bias is saved to `/app/data/ptos_htf_bias.json` with a timestamp.

## Apply patches on an existing install

`main` may still ship the stock `signal_tracker.py`. After `git pull`:

```bash
cd /mnt/user/appdata/ptos/PTOS-Hyperliquid
patch -p1 < htf_exit.patch      # if not already applied
patch -p1 < htf_pending.patch
docker compose up -d --build
```
