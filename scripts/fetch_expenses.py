#!/usr/bin/env python3
"""Collect expense ratios from SEC prospectus risk/return summary XBRL.

Funds tag their fee table in XBRL when they file a prospectus, and the SEC
republishes those tags quarterly. This is the only free bulk source for expense
ratios -- N-PORT carries holdings but no fee data, and the Nasdaq screener
carries neither.

A prospectus is filed roughly ANNUALLY per fund, so any single quarter holds
only a quarter or so of the universe (2026q2 alone covers ~940 of our 5,261).
Coverage therefore comes from accumulating quarters, and data/expenses.json is
a cumulative cache: each run pulls only quarters it has not already processed
and keeps the most recently dated figure per ticker.

Net expense (after fee waivers) is preferred over gross, because net is what a
holder actually pays and what fund marketing quotes.

  python scripts/fetch_expenses.py               # new quarters only
  python scripts/fetch_expenses.py --quarters 8  # backfill further
"""

import argparse
import csv
import gzip
import io
import json
import pathlib
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "expenses.json"

SEC_HEADERS = {
    "User-Agent": "ETF Universe dashboard joe.edessa@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}
# The literal slash inside "risk/return" is part of the published path.
RR_URL = ("https://www.sec.gov/files/dera/data/mutual-fund-prospectus-risk/"
          "return-summary-data-sets/{quarter}_rr1.zip")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_mf.json"

GROSS_TAG = "ExpensesOverAssets"
NET_TAG = "NetExpensesOverAssets"

# Values arrive as decimal fractions (0.0003 = 0.03%). Anything above this after
# conversion is a mis-tagged figure rather than a fee -- the raw data contains
# values as absurd as 85%, which would otherwise land in the table as fact.
MAX_PLAUSIBLE_PCT = 10.0

CLASS_DIM = re.compile(r"Class=(C\d+)")


def sec_get(url):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=SEC_HEADERS), timeout=180)


def recent_quarters(n):
    y, q = date.today().year, (date.today().month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append(f"{y}q{q}")
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def class_to_ticker(wanted):
    with sec_get(TICKER_MAP_URL) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    out = {}
    for cik, series_id, class_id, symbol in json.loads(raw)["data"]:
        if symbol in wanted:
            out[class_id] = symbol
    return out


def scan_quarter(quarter, cls2sym, found):
    """Merge one quarter's fee tags into `found`, newest ddate winning."""
    url = RR_URL.format(quarter=quarter)
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / f"{quarter}.zip"
        try:
            with sec_get(url) as resp, open(path, "wb") as fh:
                shutil.copyfileobj(resp, fh, length=1 << 20)
        except urllib.error.HTTPError as exc:
            print(f"  {quarter}: HTTP {exc.code} — skipped", flush=True)
            return 0

        added = 0
        with zipfile.ZipFile(path) as z:
            if "num.tsv" not in z.namelist():
                print(f"  {quarter}: no num.tsv — skipped", flush=True)
                return 0
            with z.open("num.tsv") as f:
                reader = csv.DictReader(
                    io.TextIOWrapper(f, encoding="latin-1", newline=""),
                    delimiter="\t")
                for row in reader:
                    tag = row["tag"]
                    if tag != GROSS_TAG and tag != NET_TAG:
                        continue
                    # The class id lives in otherdims ("Class=C000038531;"),
                    # not in the class column, which is blank on these rows.
                    m = CLASS_DIM.search(row.get("otherdims") or "")
                    symbol = cls2sym.get(m.group(1) if m else row.get("class"))
                    if not symbol:
                        continue
                    try:
                        pct = float(row["value"]) * 100
                    except (TypeError, ValueError):
                        continue
                    if pct < 0 or pct > MAX_PLAUSIBLE_PCT:
                        continue
                    ddate = row.get("ddate") or ""
                    basis = "net" if tag == NET_TAG else "gross"
                    prev = found.get(symbol)
                    # Newer filing wins; within the same filing, net beats gross.
                    if (prev is None
                            or ddate > prev["asOf"]
                            or (ddate == prev["asOf"]
                                and basis == "net" and prev["basis"] == "gross")):
                        if prev is None:
                            added += 1
                        found[symbol] = {"pct": round(pct, 4),
                                         "asOf": ddate, "basis": basis}
        print(f"  {quarter}: +{added} new tickers "
              f"({len(found)} total)", flush=True)
        return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=8,
                    help="how many recent quarters to consider")
    ap.add_argument("--force", action="store_true",
                    help="re-scan quarters already processed")
    args = ap.parse_args()

    etfs_path = DATA / "etfs.json"
    if not etfs_path.exists():
        print("data/etfs.json missing — run fetch_etfs.py first", file=sys.stderr)
        return 1
    etfs = json.loads(etfs_path.read_text())
    wanted = {e["symbol"] for e in etfs}

    cache = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text())
        except ValueError:
            cache = {}
    found = cache.get("ratios", {})
    done = set() if args.force else set(cache.get("quarters", []))

    todo = [q for q in recent_quarters(args.quarters) if q not in done]
    if not todo:
        print(f"no new quarters ({len(found)} tickers cached)")
    else:
        cls2sym = class_to_ticker(wanted)
        print(f"scanning {len(todo)} quarter(s): {', '.join(todo)}", flush=True)
        for quarter in todo:
            scan_quarter(quarter, cls2sym, found)
            done.add(quarter)
        CACHE.write_text(json.dumps(
            {"quarters": sorted(done), "ratios": dict(sorted(found.items()))},
            indent=0) + "\n")

    # Merge into the published records.
    hits = 0
    for e in etfs:
        rec = found.get(e["symbol"])
        e["expense"] = rec["pct"] if rec else None
        e["expenseBasis"] = rec["basis"] if rec else None
        if rec:
            hits += 1
    etfs_path.write_text(json.dumps(etfs, separators=(",", ":")) + "\n")

    meta = json.loads((DATA / "meta.json").read_text())
    meta["expenseKnown"] = hits
    meta["expenseSource"] = "SEC prospectus risk/return summary XBRL"
    meta["expenseQuarters"] = sorted(done)
    if hits:
        vals = sorted(e["expense"] for e in etfs if e.get("expense") is not None)
        meta["expenseMedian"] = round(vals[len(vals) // 2], 3)
    (DATA / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    print(f"\nexpense ratio known for {hits}/{len(wanted)} funds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
