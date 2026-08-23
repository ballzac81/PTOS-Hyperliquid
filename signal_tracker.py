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
  GET  /trades           Bot trade log (only trades PTOS executed)
  GET  /health           Health check
  GET  /dashboard        Web dashboard
"""

# RESTORE IN PROGRESS - if you see this the full file failed to push.
# Run: git checkout 1873e5c21263822c60c2da310355e62cad192b4c -- signal_tracker.py
raise SystemExit('signal_tracker.py restore incomplete - checkout commit 1873e5c')
