# HTF bias filter

Trade the lower timeframe (e.g. 2H). Send bias from a higher timeframe (e.g. 4H) to `/htf-trend`, or use the dashboard HTF buttons.

## Behaviour

| HTF bias | LTF signal | Result |
|----------|------------|--------|
| bull | trend-up | Open / flip long |
| bull | trend-down | Close long only (no short). SELL stays pending |
| bear | trend-down | Open / flip short (per `SELL_MODE`) |
| bear | trend-up | Close short only (no long). BUY stays pending |

Pending signals stay armed until HTF matches and a trend confirm fires, or `HTF_PENDING_SECONDS` expires (`0` = no expiry).

## Env

```env
HTF_FILTER_ENABLED=true
HTF_PENDING_SECONDS=43200
```

No extra env var is required for armed-state persist. Arms are written to `/app/data/ptos_armed.json` on arm/disarm/reset and on container SIGTERM (Unraid backup stop).

## TradingView

```json
{"coin": "HYPE", "token": "YOUR_SECRET_TOKEN", "bias": "bull"}
```

| Alert | URL | Body |
|-------|-----|------|
| HTF turns bull | `/htf-trend` | `bias: bull` |
| HTF turns bear | `/htf-trend` | `bias: bear` |

Bias file: `/app/data/ptos_htf_bias.json`.

## Dashboard

- **HTF Bull / Bear / Neutral** — set bias for the coin in the box
- **Trend Up / Trend Down** — fire `/trend-up` or `/trend-down` (can execute a trade)
- Armed + blocked by HTF shows yellow **PENDING HTF** and time left
