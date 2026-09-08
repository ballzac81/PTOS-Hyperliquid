#!/usr/bin/env python3
"""Add HTF pending countdown to /status and the dashboard."""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "signal_tracker.py")
s = path.read_text()

if "pending_remaining_s" in s and "PENDING HTF" in s:
    print("Dashboard pending timer already present.")
    sys.exit(0)

# 1) env var
old_env = 'HTF_FILTER_ENABLED   = os.environ.get("HTF_FILTER_ENABLED", "false").lower() == "true"\n'
new_env = (
    'HTF_FILTER_ENABLED   = os.environ.get("HTF_FILTER_ENABLED", "false").lower() == "true"\n'
    'HTF_PENDING_SECONDS  = int(os.environ.get("HTF_PENDING_SECONDS", "43200"))\n'
)
if "HTF_PENDING_SECONDS" not in s:
    if old_env not in s:
        print("FAILED: HTF_FILTER_ENABLED line not found")
        sys.exit(1)
    s = s.replace(old_env, new_env, 1)
    print("OK: env")
else:
    print("OK: env already present")

old_status_arm = '''                    coin_status[side] = {
                        "armed":              True,
                        "age_s":              age,
                        "window_remaining_s": -1 if WINDOW_SECONDS == 0 else max(0, WINDOW_SECONDS - age),
                        "waiting_for":        "trend-up" if side == "buy" else "trend-down",
                    }
'''
new_status_arm = '''                    need = "bull" if side == "buy" else "bear"
                    bias = htf_bias.get(coin)
                    htf_blocks = bool(HTF_FILTER_ENABLED and bias != need)
                    pending_left = -1
                    if htf_blocks and HTF_PENDING_SECONDS > 0:
                        pending_left = max(0, HTF_PENDING_SECONDS - age)
                    coin_status[side] = {
                        "armed":               True,
                        "age_s":               age,
                        "window_remaining_s":  -1 if WINDOW_SECONDS == 0 else max(0, WINDOW_SECONDS - age),
                        "waiting_for":         "trend-up" if side == "buy" else "trend-down",
                        "htf_blocks":          htf_blocks,
                        "htf_need":            need,
                        "pending_remaining_s": pending_left,
                    }
'''
if old_status_arm not in s:
    print("FAILED: status armed block not found")
    sys.exit(1)
s = s.replace(old_status_arm, new_status_arm, 1)
print("OK: status armed fields")

old_status_json = '''        "htf_bias":            dict(htf_bias),
        "htf_filter_enabled":  HTF_FILTER_ENABLED,
        "window_seconds":      WINDOW_SECONDS,
'''
new_status_json = '''        "htf_bias":            dict(htf_bias),
        "htf_filter_enabled":  HTF_FILTER_ENABLED,
        "htf_pending_seconds": HTF_PENDING_SECONDS,
        "window_seconds":      WINDOW_SECONDS,
'''
if old_status_json in s:
    s = s.replace(old_status_json, new_status_json, 1)
    print("OK: status json")
elif "htf_pending_seconds" in s:
    print("OK: status json already present")
else:
    print("FAILED: status json block not found")
    sys.exit(1)

old_css = ".badge-idle { background: rgba(139,148,158,0.08); color: var(--muted); border: 1px solid var(--border); }"
new_css = (
    ".badge-idle { background: rgba(139,148,158,0.08); color: var(--muted); border: 1px solid var(--border); }\n"
    ".badge-pending { background: rgba(210,153,34,0.12); color: var(--yellow); border: 1px solid rgba(210,153,34,0.35); }"
)
if old_css not in s:
    print("FAILED: badge-idle css not found")
    sys.exit(1)
if "badge-pending" not in s:
    s = s.replace(old_css, new_css, 1)
    print("OK: css")
else:
    print("OK: css already present")

old_badge = '''function renderBadge(state, side) {
  if (!state || state === 'idle' || state === 'expired') {
    return '<span class="badge badge-idle">' + (state || 'idle') + '</span>';
  }
  if (state.armed) {
    var age = state.age_s < 60 ? state.age_s + 's' : Math.round(state.age_s / 60) + 'm';
    var waiting = state.waiting_for || (side === 'buy' ? 'trend-up' : 'trend-down');
    var cls = side === 'buy' ? 'badge-buy' : 'badge-sell';
    return '<span class="badge ' + cls + '">\u25cf ARMED</span>' +
           '<span class="badge-detail"> waiting for ' + waiting + ' \u00b7 ' + age + ' ago</span>';
  }
  return '<span class="badge badge-idle">idle</span>';
}
'''
# Use the exact source punctuation from the repo (middle dot as ' · ')
print("WARN: if badge replace fails, the apply file on disk is preferred")

compile(s, str(path), "exec")
path.write_text(s)
print("partial write - badge/header must match live file")
