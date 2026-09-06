#!/usr/bin/env python3
"""Distribution yield for Tokyo-listed ETFs, from the fund library's NAV history.

The fund library's search API gives one figure, "dividend1y", and it cannot be
used: the library quotes NAV per 1, 10 or 100 units depending on the fund, and
it changes a fund's basis over time. NF Nikkei High Dividend 50 (1489) moved
from per-unit to per-100-units on 2026-07-08; its dividend1y still sums the
per-unit payments (94 yen) while its NAV is now 357,613 -- a 0.03% "yield"
for a fund that pays about 3%.

Each fund's NAV history CSV (年月日, 基準価額, 純資産総額, 分配金, 決算期) is
self-consistent row by row: the distribution and the NAV on its ex-date share a
basis. So each payment is taken as a percentage of that day's NAV and the
trailing twelve months are summed. That is a distribution yield ON NAV, basis
independent, and it is what this writes:

  yield12m   sum over the last 365 days of (distribution / NAV on ex-date), %
  count12m   payments in that window
  lastDate   most recent ex-date
  lastPct    that payment as % of NAV
  recent     up to eight most recent payments, [date, pct]

Yield on NAV, not on price -- there are no Tokyo prices on this site, and for
an ETF the two differ by the premium/discount, normally a few basis points.

One CSV per library fund (~248, ~100 KB each), so this runs weekly, not
nightly. Writes data/tokyo_distributions.json. Refuses to write if fewer than
MIN_FUNDS parse.
"""

import concurrent.futures
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "tokyo_distributions.json"

CSV_URL = ("https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
           "?isinCd={isin}&associFundCd={afc}")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en"}

WINDOW_DAYS = 365
RECENT = 8
MIN_FUNDS = 200   # 248 library funds when built
WORKERS = 6

JA_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def fetch_csv(isin, afc):
    req = urllib.request.Request(CSV_URL.format(isin=isin, afc=afc), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read()
    for enc in ("cp932", "utf-8-sig"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_history(text):
    """-> [(date, nav, distribution or None)] oldest first."""
    rows = []
    for line in text.splitlines()[1:]:
        cells = line.split(",")
        if len(cells) < 4:
            continue
        m = JA_DATE.match(cells[0].strip())
        if not m:
            continue
        try:
            nav = float(cells[1]) if cells[1].strip() else None
            dist = float(cells[3]) if cells[3].strip() else None
        except ValueError:
            continue
        if nav is None:
            continue
        rows.append((date(int(m.group(1)), int(m.group(2)), int(m.group(3))), nav, dist))
    rows.sort()
    return rows


def summarise(rows, today):
    """Trailing distribution yield on NAV from a self-consistent history."""
    paid = [(d, dist / nav * 100) for d, nav, dist in rows if dist and nav > 0]
    if not rows:
        return None
    cutoff = today - timedelta(days=WINDOW_DAYS)
    window = [(d, p) for d, p in paid if d > cutoff]
    last = paid[-1] if paid else None
    return {
        "yield12m": round(sum(p for _, p in window), 3),
        "count12m": len(window),
        "lastDate": last[0].isoformat() if last else None,
        "lastPct": round(last[1], 3) if last else None,
        "recent": [[d.isoformat(), round(p, 3)] for d, p in paid[-RECENT:]],
        "navAsOf": rows[-1][0].isoformat(),
    }


def main():
    tokyo = json.loads((DATA / "tokyo.json").read_text())
    funds = [f for f in tokyo["funds"] if f.get("library") and f.get("isin") and f.get("associFundCd")]
    today = date.today()

    def work(f):
        try:
            return f["code"], summarise(parse_history(fetch_csv(f["isin"], f["associFundCd"])), today), None
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            return f["code"], None, repr(exc)[:80]

    out, failed = {}, []
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        for code, rec, err in ex.map(work, funds):
            if rec:
                out[code] = rec
            else:
                failed.append((code, err or "no rows"))

    paying = sum(1 for r in out.values() if r["count12m"])
    print(f"parsed {len(out)} of {len(funds)} library funds; {paying} paid in the last year; "
          f"failed {len(failed)}: {failed[:5]}")
    if len(out) < MIN_FUNDS:
        print(f"only {len(out)} funds (< {MIN_FUNDS}) — refusing to write", file=sys.stderr)
        return 1
    yields = sorted(r["yield12m"] for r in out.values() if r["count12m"])
    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Investment Trusts Association fund library, NAV/distribution history CSV per fund",
        "basis": "each distribution as % of NAV on its ex-date, summed over the trailing 365 days",
        "count": len(out), "paying12m": paying,
        "medianYield12m": round(yields[len(yields) // 2], 3) if yields else None,
        "funds": dict(sorted(out.items())),
    }, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"wrote {OUT.name}; median trailing yield among payers "
          f"{yields[len(yields) // 2]:.2f}%" if yields else "wrote (no payers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
