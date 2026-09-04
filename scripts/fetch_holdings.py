#!/usr/bin/env python3
"""Extract each fund's largest positions from SEC Form N-PORT.

N-PORT is the only free source that covers the whole universe in one download:
every registered investment company files its full portfolio quarterly. The
quarterly ZIP is ~420 MB and the holdings table inside it is ~870 MB, so this
streams rather than loads, keeping only a 10-position heap per fund.

What N-PORT does NOT cover is funds that are not registered investment
companies. Commodity and crypto grantor trusts (GLD, SLV, IBIT, USO) file
10-Ks instead, and unit investment trusts (SPY, DIA, MDY) file nothing of this
shape. That is ~1,000 of 5,261 funds -- and mostly funds holding a single
asset, where "top holdings" would say nothing anyway.

Holdings are as of the filing's report date, which lags by a quarter or so.
The dashboard labels them with that date rather than implying they are current.

  python scripts/fetch_holdings.py                 # latest quarter
  python scripts/fetch_holdings.py --zip FILE.zip  # reuse a local download
  python scripts/fetch_holdings.py --quarter 2026q2
"""

import argparse
import csv
import heapq
import io
import json
import pathlib
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# The SEC requires a descriptive User-Agent with contact details; a browser UA
# gets a 403 here. See https://www.sec.gov/os/webmaster-faq#developers
SEC_HEADERS = {
    "User-Agent": "ETF Universe dashboard joe.edessa@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}
NPORT_URL = ("https://www.sec.gov/files/dera/data/form-n-port-data-sets/"
             "{quarter}_nport.zip")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_mf.json"

TOP_N = 10
# Positions below this are noise in a top-10 list and mostly reflect rounding.
MIN_PCT = 0.01

# Rollups are computed over EVERY position, not just the top ten. A top-10 list
# covers about a third of a broad fund, which is fine for "what does it hold"
# and useless for "how concentrated is it" -- the question a portfolio actually
# turns on. Aggregating at fetch time keeps the published file small while
# describing 100% of each fund.
ROLLUP_KEEP = 8          # entries per dimension before the tail becomes "Other"
ROLLUP_MIN_PCT = 0.5

# N-PORT ships these as codes.
ASSET_LABEL = {
    "EC": "Equity (common)", "EP": "Equity (preferred)", "DBT": "Debt",
    "RE": "Real estate", "LON": "Loan", "COMM": "Commodity",
    "STIV": "Short-term / cash", "SN": "Structured note",
    "ABS-MBS": "Mortgage-backed", "ABS-ABCP": "Asset-backed CP",
    "ABS-O": "Asset-backed (other)", "DE": "Derivative",
    "DFE": "Derivative (equity)", "DIR": "Derivative (rates)",
    "DCR": "Derivative (credit)", "DFC": "Derivative (FX)",
    "DCO": "Derivative (commodity)", "RA": "Repo", "UST": "US Treasury",
}
ISSUER_LABEL = {
    "CORP": "Corporate", "MUN": "Municipal", "UST": "US Treasury",
    "USGA": "US govt agency", "USGSE": "US govt-sponsored",
    "NUSS": "Non-US sovereign", "RGS": "Registered fund", "PS": "Private fund",
}


def sec_get(url):
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    return urllib.request.urlopen(req, timeout=120)


def recent_quarters(n=5):
    y, q = date.today().year, (date.today().month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append(f"{y}q{q}")
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def latest_available():
    for quarter in recent_quarters():
        try:
            req = urllib.request.Request(NPORT_URL.format(quarter=quarter),
                                         headers=SEC_HEADERS, method="HEAD")
            urllib.request.urlopen(req, timeout=60)
            return quarter
        except urllib.error.HTTPError:
            continue
    return None


def download(quarter, dest):
    url = NPORT_URL.format(quarter=quarter)
    print(f"downloading {url}", flush=True)
    with sec_get(url) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh, length=1 << 20)
    print(f"  {dest.stat().st_size / 1024 / 1024:.0f} MB", flush=True)


def series_to_tickers(wanted):
    """SEC series id -> tickers, restricted to the funds we publish."""
    with sec_get(TICKER_MAP_URL) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    payload = json.loads(raw)
    idx = {}
    for cik, series_id, class_id, symbol in payload["data"]:
        if symbol in wanted:
            idx.setdefault(series_id, set()).add(symbol)
    return idx


def rows(zf, member):
    """Stream a TSV member as dicts without holding it in memory."""
    with zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="latin-1", newline="")
        for row in csv.DictReader(text, delimiter="\t"):
            yield row


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_sec_date(v):
    try:
        return datetime.strptime(v, "%d-%b-%Y").date()
    except (TypeError, ValueError):
        return None


def condense(d, labels=None):
    """Largest slices by weight, with the tail summed into "Other".

    Sorted by ABSOLUTE weight so a large short position is not buried below
    trivial longs, but the reported value keeps its sign.
    """
    if not d:
        return {}
    items = sorted(d.items(), key=lambda kv: -abs(kv[1]))
    out, tail = {}, 0.0
    for key, pct in items:
        if len(out) < ROLLUP_KEEP and abs(pct) >= ROLLUP_MIN_PCT:
            out[(labels or {}).get(key, key)] = round(pct, 2)
        else:
            tail += pct
    if abs(tail) >= 0.05:
        out["Other"] = round(tail, 2)
    return out


def build(zip_path, quarter):
    etfs = json.loads((DATA / "etfs.json").read_text())
    wanted = {e["symbol"] for e in etfs}
    by_series = series_to_tickers(wanted)
    print(f"{len(by_series)} of our funds map to an SEC series", flush=True)

    zf = zipfile.ZipFile(zip_path)

    # accession -> report date, so we can keep only each series' latest filing.
    report_date = {}
    for r in rows(zf, "SUBMISSION.tsv"):
        d = parse_sec_date(r.get("REPORT_DATE"))
        if d:
            report_date[r["ACCESSION_NUMBER"]] = d

    # Pick the newest filing per series. A quarterly file carries several
    # months of filings, and older amendments, so taking any one would mix
    # as-of dates across funds.
    best = {}
    for r in rows(zf, "FUND_REPORTED_INFO.tsv"):
        sid = r.get("SERIES_ID")
        if sid not in by_series:
            continue
        acc = r["ACCESSION_NUMBER"]
        d = report_date.get(acc)
        if not d:
            continue
        prev = best.get(sid)
        if prev is None or d > prev[1]:
            best[sid] = (acc, d, to_float(r.get("NET_ASSETS")))

    keep = {acc: sid for sid, (acc, _, _) in best.items()}
    print(f"{len(keep)} filings selected; streaming holdings…", flush=True)

    # Top-N heap per accession. Only the funds we care about are tracked, so
    # this stays small even though the source table is ~870 MB.
    heaps = {acc: [] for acc in keep}
    roll = {acc: {"country": {}, "asset": {}, "issuer": {}, "n": 0, "pct": 0.0}
            for acc in keep}
    scanned = 0
    for r in rows(zf, "FUND_REPORTED_HOLDING.tsv"):
        scanned += 1
        acc = r.get("ACCESSION_NUMBER")
        h = heaps.get(acc)
        if h is None:
            continue
        pct = to_float(r.get("PERCENTAGE"))
        if pct is None:
            continue

        # Every position counts toward the rollups, including the small ones a
        # top-10 list drops and the negative ones a short position contributes.
        agg = roll[acc]
        agg["n"] += 1
        agg["pct"] += pct
        for dim, col in (("country", "INVESTMENT_COUNTRY"),
                         ("asset", "ASSET_CAT"),
                         ("issuer", "ISSUER_TYPE")):
            key = (r.get(col) or "").strip() or "Unknown"
            agg[dim][key] = agg[dim].get(key, 0.0) + pct

        if pct < MIN_PCT:
            continue
        name = (r.get("ISSUER_NAME") or "").strip()
        if not name:
            continue
        entry = (pct, name, (r.get("ASSET_CAT") or "").strip())
        if len(h) < TOP_N:
            heapq.heappush(h, entry)
        elif pct > h[0][0]:
            heapq.heapreplace(h, entry)
        if scanned % 2_000_000 == 0:
            print(f"  {scanned:,} rows", flush=True)
    print(f"  {scanned:,} rows scanned", flush=True)

    out = {}
    for sid, (acc, d, net) in best.items():
        top = sorted(heaps.get(acc, []), key=lambda e: -e[0])
        if not top:
            continue
        agg = roll.get(acc, {})
        record = {
            "asOf": d.isoformat(),
            "netAssets": round(net) if net else None,
            "positions": agg.get("n", 0),
            "top": [{"name": n, "pct": round(p, 2)} for p, n, _ in top],
            "byCountry": condense(agg.get("country", {})),
            "byAsset": condense(agg.get("asset", {}), ASSET_LABEL),
            "byIssuer": condense(agg.get("issuer", {}), ISSUER_LABEL),
        }
        for symbol in by_series[sid]:
            out[symbol] = record

    (DATA / "holdings.json").write_text(
        json.dumps(dict(sorted(out.items())), separators=(",", ":")) + "\n")

    meta = json.loads((DATA / "meta.json").read_text())
    meta["holdingsKnown"] = len(out)
    meta["holdingsSource"] = f"SEC Form N-PORT {quarter}"
    dates = sorted({v["asOf"] for v in out.values()})
    meta["holdingsAsOf"] = dates[-1] if dates else None
    (DATA / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    print(f"\ntop-{TOP_N} holdings for {len(out)}/{len(wanted)} funds")
    if dates:
        print(f"report dates {dates[0]} … {dates[-1]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", help="use an already-downloaded quarterly ZIP")
    ap.add_argument("--quarter", help="e.g. 2026q2 (default: latest available)")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if this quarter is already processed")
    args = ap.parse_args()

    if not (DATA / "etfs.json").exists():
        print("data/etfs.json missing — run fetch_etfs.py first", file=sys.stderr)
        return 1

    quarter = args.quarter or latest_available()
    if not quarter:
        print("no N-PORT quarter available", file=sys.stderr)
        return 1

    # N-PORT publishes quarterly but this job runs monthly, so most runs have
    # nothing new. Re-downloading 420 MB to rebuild an identical file is pure
    # waste, and it would churn the committed data for no change.
    if not args.zip and not args.force:
        meta = json.loads((DATA / "meta.json").read_text())
        if meta.get("holdingsSource") == f"SEC Form N-PORT {quarter}":
            print(f"{quarter} already processed — nothing to do")
            return 0

    if args.zip:
        return build(pathlib.Path(args.zip), quarter)

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / f"{quarter}.zip"
        download(quarter, path)
        return build(path, quarter)


if __name__ == "__main__":
    sys.exit(main())
