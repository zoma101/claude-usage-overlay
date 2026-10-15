"""Optional helper: feeds live usage from Claude Code's status line to the overlay.

Claude Code sends your plan's rate limits (5-hour and 7-day usage) to the
status line script on every reply. Saving them to a file lets the overlay
update instantly without calling the usage API, which can rate limit.

Use it one of two ways:

1. No status line yet: point Claude Code's status line at this script.
   In ~/.claude/settings.json:
       "statusLine": {"type": "command", "command": "python /path/to/claude_usage_feed.py"}
   It prints a short status line (model + session/week usage).

2. You already have a status line script (Python): copy write_usage_feed()
   into it and call write_usage_feed(data) with the parsed stdin JSON.
"""

import json
import os
import sys
import time
from pathlib import Path

FEED = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "usage-overlay-feed.json"


def write_usage_feed(data):
    """Save data["rate_limits"] for the usage overlay. Never raises."""
    try:
        limits = data.get("rate_limits")
        if not limits:
            return
        tmp = FEED.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rate_limits": limits, "written_at": time.time()}), encoding="utf-8")
        os.replace(tmp, FEED)
    except Exception:
        pass


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    write_usage_feed(data)
    model = (data.get("model") or {}).get("display_name") or "Claude"
    limits = data.get("rate_limits") or {}
    parts = [model]
    for key, label in (("five_hour", "session"), ("seven_day", "week")):
        pct = (limits.get(key) or {}).get("used_percentage")
        if pct is not None:
            parts.append(f"{label} {round(pct)}%")
    print(" | ".join(parts))


if __name__ == "__main__":
    main()
