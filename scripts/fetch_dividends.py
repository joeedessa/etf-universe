#!/usr/bin/env python3
"""Trailing-twelve-month distributions for US-listed ETFs, from Yahoo's chart feed.

Neither existing source has them: the Nasdaq screener carries price and fee
only, and Nasdaq's own ETF dividend endpoint returns nothing for ETFs. Yahoo's
chart endpoint (query2.finance.yahoo.com/v8/finance/chart/{symbol}?events=div)
returns every cash distribution with its ex-date, keyed only by symbol, no
account. It is unofficial: it can rate-limit, and it can change. So this
script is built to degrade rather than break:

  * data/dividends.json is a per-symbol cache. A run refreshes the OLDEST
    records first, up to --limit, so a nightly pass of a few hundred cycles
    the universe in about a week while a failed night costs nothing.
  * A symbol that answers with no events is a non-payer and is recorded as
    such (count 0); a symbol that fails to fetch keeps its previous record.
  * If more than MAX_FAIL_RATE of the attempted symbols fail, the run is
    treated as a feed outage: the cache is left untouched and the exit code
    is non-zero, so the workflow does not commit a half-refreshed file.

What is recorded, per symbol:

  ttm         sum of cash distributions with an ex-date in the last 365 days
  count12m    number of those payments
  lastExDate  most recent ex-date seen (may be older than a year)
  lastAmount  that payment
  updated     when this record was last refreshed

The yield written to etfs.json is ttm / last screener price, recomputed on
every run from the cached ttm and that night's price, so it is never staler
than the price it is shown against. It is a trailing distribution yield --
what the fund actually paid over the past year against today's price -- not
a forward or SEC yield. Return-of-capital and capital-gain distributions are
included; Yahoo does not separate them.

  python scripts/fetch_dividends.py               # oldest 600 records
  python scripts/fetch_dividends.py --all         # every symbol (first run)
  python scripts/fetch_dividends.py --symbols SPY JEPI
"""

import argparse
import concurrent.futures
import http.cookiejar
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "dividends.json"

CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=3mo&events=div"
CRUMB = "https://query2.finance.yahoo.com/v1/test/getcrumb"
COOKIE_SEED = "https://fc.yahoo.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}

WINDOW_DAYS = 365
DEFAULT_LIMIT = 600
WORKERS = 3
PAUSE = 0.35          # seconds between requests per worker; ~8/s total is polite
ATTEMPTS = 4
BACKOFF = (4, 12, 30) # after a 429 or 5xx
MAX_FAIL_RATE = 0.30  # more than this of attempted symbols failing = outage

_lock = threading.Lock()
_session = {"opener": None, "crumb": ""}


def opener():
    with _lock:
        if _session["opener"] is None:
            jar = http.cookiejar.CookieJar()
            _session["opener"] = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return _session["opener"]


def get_crumb():
    """Yahoo sometimes wants a session cookie + crumb; harmless when it does not."""
    with _lock:
        if _session["crumb"]:
            return _session["crumb"]
        op = opener()
        try:
            op.open(urllib.request.Request(COOKIE_SEED, headers=HEADERS), timeout=20).read()
        except urllib.error.URLError:
            pass  # the 404 still sets the cookie
        try:
            _session["crumb"] = op.open(urllib.request.Request(CRUMB, headers=HEADERS), timeout=20).read().decode()
        except urllib.error.URLError:
            _session["crumb"] = ""
        return _session["crumb"]


def fetch_events(sym):
    """-> (price, [(ex_date, amount)]) or raise."""
    url = CHART.format(sym=urllib.parse.quote(sym))
    last = None
    for i in range(ATTEMPTS):
        try:
            crumb = _session["crumb"]
            req = urllib.request.Request(url + (f"&crumb={urllib.parse.quote(crumb)}" if crumb else ""), headers=HEADERS)
            with opener().open(req, timeout=30) as resp:
                body = json.loads(resp.read())
            result = (body.get("chart") or {}).get("result") or []
            if not result:
                err = (body.get("chart") or {}).get("error") or {}
                raise ValueError(err.get("code") or "empty")
            r = result[0]
            price = (r.get("meta") or {}).get("regularMarketPrice")
            events = (r.get("events") or {}).get("dividends") or {}
            out = []
            for v in events.values():
                try:
                    out.append((date.fromtimestamp(int(v["date"])), float(v["amount"])))
                except (KeyError, TypeError, ValueError):
                    continue
            return price, sorted(out)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                raise
            if exc.code in (401, 403):
                get_crumb()
            if exc.code in (429, 401, 403) or exc.code >= 500:
                time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            last = exc
            time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
    raise last


def summarise(events, today):
    cutoff = today - timedelta(days=WINDOW_DAYS)
    window = [(d, a) for d, a in events if d > cutoff and d <= today]
    last = events[-1] if events else None
    return {
        "ttm": round(sum(a for _, a in window), 6),
        "count12m": len(window),
        "lastExDate": last[0].isoformat() if last else None,
        "lastAmount": round(last[1], 6) if last else None,
    }


def frequency(count):
    return {0: "none", 1: "annual", 2: "semi-annual", 4: "quarterly", 12: "monthly"}.get(
        count, "irregular" if count < 12 else "monthly+")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="symbols to refresh, oldest first")
    ap.add_argument("--all", action="store_true", help="refresh every symbol")
    ap.add_argument("--symbols", nargs="*", help="only these (debug; still writes)")
    args = ap.parse_args()

    etfs_path = DATA / "etfs.json"
    etfs = json.loads(etfs_path.read_text())
    symbols = [e["symbol"] for e in etfs]
    price_of = {e["symbol"]: e.get("price") for e in etfs}

    cache = {"records": {}}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text())
        except ValueError:
            pass
    records = cache.get("records", {})

    if args.symbols:
        todo = [s for s in args.symbols if s in price_of]
    else:
        # Never-seen first, then stalest; symbols that left the universe are dropped.
        order = sorted(symbols, key=lambda s: records.get(s, {}).get("updated", ""))
        todo = order if args.all else order[:args.limit]
    today = date.today()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def work(sym):
        try:
            price, events = fetch_events(sym)
            time.sleep(PAUSE)
            rec = summarise(events, today)
            rec.update({"updated": now, "yahooPrice": price})
            return sym, rec, None
        except Exception as exc:  # noqa: BLE001 - classify every failure the same way
            return sym, None, f"{type(exc).__name__}:{getattr(exc, 'code', '')}"

    print(f"refreshing {len(todo)} of {len(symbols)} symbols ({len(records)} cached)", flush=True)
    done, failed, errors = 0, [], {}
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        for sym, rec, err in ex.map(work, todo):
            if rec is not None:
                records[sym] = rec
                done += 1
            else:
                failed.append(sym)
                errors[err] = errors.get(err, 0) + 1
            if (done + len(failed)) % 250 == 0:
                print(f"  {done + len(failed)}/{len(todo)} ({len(failed)} failed)", flush=True)

    print(f"refreshed {done}, failed {len(failed)} {dict(sorted(errors.items(), key=lambda kv: -kv[1])[:4])}")
    if todo and len(failed) > MAX_FAIL_RATE * len(todo):
        print(f"{len(failed)}/{len(todo)} failed — treating as a feed outage, cache untouched",
              file=sys.stderr)
        return 1

    records = {s: r for s, r in records.items() if s in price_of}
    CACHE.write_text(json.dumps({
        "source": "Yahoo Finance chart feed, cash distributions by ex-date",
        "windowDays": WINDOW_DAYS,
        "records": dict(sorted(records.items())),
    }, separators=(",", ":")) + "\n")

    # Merge: yield against THIS run's screener price, never against a stale one.
    known, payers = 0, 0
    for e in etfs:
        rec = records.get(e["symbol"])
        price = e.get("price")
        if rec is None:
            e["distTtm"] = e["distCount12m"] = e["lastExDate"] = e["lastDist"] = e["yieldTtm"] = e["distFreq"] = None
            continue
        known += 1
        e["distTtm"] = rec["ttm"]
        e["distCount12m"] = rec["count12m"]
        e["lastExDate"] = rec["lastExDate"]
        e["lastDist"] = rec["lastAmount"]
        e["distFreq"] = frequency(rec["count12m"])
        e["yieldTtm"] = round(rec["ttm"] / price * 100, 3) if price and rec["ttm"] else (0.0 if rec["ttm"] == 0 else None)
        if rec["count12m"]:
            payers += 1
    etfs_path.write_text(json.dumps(etfs, separators=(",", ":")) + "\n")

    meta = json.loads((DATA / "meta.json").read_text())
    meta["yieldKnown"] = known
    meta["yieldPayers"] = payers
    meta["yieldSource"] = "Yahoo Finance chart feed; trailing 12-month cash distributions over last price"
    ys = sorted(e["yieldTtm"] for e in etfs if e.get("yieldTtm"))
    meta["yieldMedianPayers"] = round(ys[len(ys) // 2], 3) if ys else None
    (DATA / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"distribution records for {known}/{len(etfs)} funds; {payers} paid in the last year"
          + (f"; median yield among payers {meta['yieldMedianPayers']:.2f}%" if ys else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
