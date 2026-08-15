#!/usr/bin/env python3
"""Fill in each fund's first trading day, and merge it into data/etfs.json.

The screener does not publish inception dates, so this reads the first bar of
each fund's price history from Nasdaq's chart endpoint. That is the LISTING
date -- the day the fund began trading -- which is what the dashboard claims.
Prospectus inception can precede it by a few days and is not published in bulk
anywhere free; spot checks against ten well-known funds (SPY 1993-01-29, QQQ
1999-03-10, GLD 2004-11-18, IBIT 2024-01-11) match to the day.

A first trading day never changes, so data/inception.json is a permanent cache:
the expensive pass happens once and later runs look up only newly listed funds.
Symbols with no history resolve to null and are not retried unless asked, since
the usual cause is a fund too new to have printed a bar.

  python scripts/fetch_inception.py            # top up (bounded, for CI)
  python scripts/fetch_inception.py --all      # no cap, for the initial fill
  python scripts/fetch_inception.py --retry-null
"""

import argparse
import json
import pathlib
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "inception.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# One request per fund against a free public endpoint. Ten concurrent measured
# clean (~3.5 req/s, no 429s) while six left throughput on the table; the
# initial fill is thousands of calls, so the difference is half an hour. After
# that a run touches only the week's new listings.
WORKERS = 10
DEFAULT_LIMIT = 400
ATTEMPTS = 3

# SPY, the first US-listed ETF, began trading on this date, so no fund in this
# universe can predate it. A handful of tickers collide with a longer-running
# index or predecessor security and come back pinned to the 1990 window floor
# -- DJIA returns the Dow's own history, not the 2022 Global X fund's. Rejecting
# anything older turns a confidently wrong date into an honest blank.
EARLIEST_POSSIBLE = "1993-01-29"

_lock = threading.Lock()


def chart_url(symbol: str) -> str:
    return (
        f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}/chart"
        f"?assetclass=etf&fromdate=1990-01-01&todate={date.today().isoformat()}"
    )


def first_trade(symbol: str):
    """Earliest bar as YYYY-MM-DD, or None when the fund has no history."""
    for attempt in range(ATTEMPTS):
        try:
            req = urllib.request.Request(chart_url(symbol), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.load(resp)
            bars = (payload.get("data") or {}).get("chart") or []
            if not bars:
                return None
            raw = (bars[0].get("z") or {}).get("dateTime")
            if not raw:
                return None
            got = datetime.strptime(raw, "%m/%d/%Y").date().isoformat()
            return got if got >= EARLIEST_POSSIBLE else None
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < ATTEMPTS - 1:
                time.sleep((2 ** attempt) * 2 + random.random())
                continue
            return None
        except (urllib.error.URLError, OSError, ValueError):
            if attempt < ATTEMPTS - 1:
                time.sleep((2 ** attempt) + random.random())
                continue
            return None
    return None


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="no cap on lookups (use for the initial fill)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--retry-null", action="store_true",
                    help="re-check symbols previously found to have no history")
    args = ap.parse_args()

    etfs = load_json(DATA / "etfs.json", None)
    if not etfs:
        print("data/etfs.json missing — run fetch_etfs.py first", file=sys.stderr)
        return 1

    cache = load_json(CACHE, {})
    symbols = [e["symbol"] for e in etfs]

    todo = [s for s in symbols
            if s not in cache or (args.retry_null and cache.get(s) is None)]
    capped = len(todo)
    if not args.all:
        todo = todo[:args.limit]

    if todo:
        print(f"looking up {len(todo)} of {capped} missing "
              f"({len(cache)} already cached)", flush=True)
        done = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(first_trade, s): s for s in todo}
            for fut in as_completed(futures):
                sym = futures[fut]
                with _lock:
                    cache[sym] = fut.result()
                    done += 1
                    # Checkpoint: the initial fill runs for tens of minutes and
                    # must not lose everything if it is interrupted.
                    if done % 250 == 0:
                        save_cache(cache)
                        print(f"  {done}/{len(todo)}", flush=True)
        save_cache(cache)
    else:
        print(f"nothing to look up ({len(cache)} cached)")

    # Merge into the published records.
    hits = 0
    for e in etfs:
        got = cache.get(e["symbol"])
        e["inception"] = got
        if got:
            hits += 1
    (DATA / "etfs.json").write_text(
        json.dumps(etfs, separators=(",", ":")) + "\n")

    meta = load_json(DATA / "meta.json", {})
    meta["inceptionKnown"] = hits
    meta["inceptionSource"] = "First trading day, from the Nasdaq chart endpoint"
    if hits:
        years = sorted(e["inception"][:4] for e in etfs if e.get("inception"))
        meta["oldestFund"] = years[0]
        meta["launchedThisYear"] = sum(
            1 for y in years if y == str(date.today().year))
    (DATA / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    missing = len(symbols) - hits
    print(f"inception known for {hits}/{len(symbols)} funds ({missing} without)")
    return 0


def save_cache(cache):
    CACHE.write_text(json.dumps(dict(sorted(cache.items())), indent=0) + "\n")


if __name__ == "__main__":
    sys.exit(main())
