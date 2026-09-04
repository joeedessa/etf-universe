#!/usr/bin/env python3
"""Record when each ticker first appeared in our universe, and tag new ones.

The nightly screener pull picks up new listings automatically, but it says
nothing about WHICH funds are new — the file just gets longer. This keeps a
first-seen date per ticker so additions are detectable after the fact.

Seeding matters: on the first run every ticker is "first seen today", which
would tag all 5,000 as new. So the initial backfill uses each fund's listing
date instead, which is what we actually want the tag to mean, and only genuine
later arrivals get today's date.

A fund is tagged new when the EARLIER of (first seen, listed) is within
NEW_DAYS. Using the earlier of the two stops a fund that was listed years ago
but only just entered the screener — a re-listing, a ticker change, a data
correction — from being announced as a new launch.

  python scripts/track_new.py
"""

import json
import pathlib
import sys
from datetime import date, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SEEN = DATA / "first_seen.json"

NEW_DAYS = 90


def load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return default


def main():
    etfs = load(DATA / "etfs.json", None)
    if not etfs:
        print("data/etfs.json missing — run fetch_etfs.py first", file=sys.stderr)
        return 1

    seen = load(SEEN, {})
    today = date.today().isoformat()
    first_run = not seen
    added = 0

    for e in etfs:
        sym = e["symbol"]
        if sym in seen:
            continue
        # First run: fall back to the listing date so the backfill does not
        # declare the entire universe new. Afterwards, an unseen ticker really
        # did arrive today.
        seen[sym] = (e.get("inception") or today) if first_run else today
        added += 1

    SEEN.write_text(json.dumps(dict(sorted(seen.items())), indent=0) + "\n")

    cutoff = (date.today() - timedelta(days=NEW_DAYS)).isoformat()
    fresh = 0
    for e in etfs:
        first = seen.get(e["symbol"])
        listed = e.get("inception")
        # Earlier of the two: a 2019 fund that only just entered the screener
        # is not a new launch.
        ref = min(d for d in (first, listed) if d) if (first or listed) else None
        e["isNew"] = bool(ref and ref >= cutoff)
        e["firstSeen"] = first
        if e["isNew"]:
            fresh += 1

    (DATA / "etfs.json").write_text(json.dumps(etfs, separators=(",", ":")) + "\n")

    meta = load(DATA / "meta.json", {})
    meta["newCount"] = fresh
    meta["newWindowDays"] = NEW_DAYS
    (DATA / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    print(f"{'seeded' if first_run else 'tracked'} {added} ticker(s); "
          f"{fresh} tagged new (listed or first seen since {cutoff})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
