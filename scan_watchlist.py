"""
scan_watchlist.py
------------------
Standalone entry point for running a watchlist scan WITHOUT opening the chat
UI - this is what a real scheduled job (cron, GitHub Actions, Windows Task
Scheduler, APScheduler, etc.) would invoke automatically.

This deliberately does NOT go through the LLM at all - a scan is a
deterministic data-gathering job (search + sentiment scoring + storage), so
there's no reason to pay for/depend on an LLM call just to trigger it. The
LLM only gets involved when a human asks a question about the results.

Usage:
    python scan_watchlist.py

Example cron entry (run every day at 8am):
    0 8 * * * cd /path/to/trend-agent && /usr/bin/python3 scan_watchlist.py >> scan.log 2>&1

Example GitHub Actions workflow (.github/workflows/scan.yml):
    on:
      schedule:
        - cron: '0 8 * * *'
    jobs:
      scan:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - uses: actions/setup-python@v5
            with: {python-version: '3.11'}
          - run: pip install -r requirements.txt
          - run: python scan_watchlist.py
            env:
              TAVILY_API_KEY: ${{ secrets.TAVILY_API_KEY }}
"""

import sys
from datetime import datetime, timezone

from tools import run_watchlist_scan


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting watchlist scan...")
    try:
        result = run_watchlist_scan()
    except Exception as e:
        print(f"Scan failed: {e}", file=sys.stderr)
        sys.exit(1)

    if result["scanned"] == 0:
        print("Watchlist is empty - nothing scanned.")
        return

    print(f"Scanned {result['scanned']} topic(s):")
    for r in result["results"]:
        if r["success"]:
            print(f"  ✓ {r['topic']}: {r['summary']}")
        else:
            print(f"  ✗ {r['topic']}: {r['error']}")


if __name__ == "__main__":
    main()
