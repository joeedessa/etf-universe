#!/usr/bin/env python3
"""Build the Tokyo-listed ETF universe from JPX's published issue table.

JPX lists every TSE-listed ETF with its code, name, management company, the
index it tracks, its listing date and its trust fee. Fee coverage is complete,
which is better than the US side manages.

What does NOT exist for these funds, and is therefore absent rather than
guessed at:

  price / daily move / 1-year   no free quote source covers Tokyo codes. The
                                Nasdaq screener rejects them, Yahoo rate-limits
                                on sight, Stooq has no JP symbols, and JPX
                                publishes no free quote file.
  holdings / AUM                Japanese funds file neither N-PORT nor an SEC
                                prospectus, so the two feeds behind the US
                                holdings and fee data have nothing on them.

That asymmetry is why Tokyo is a separate page rather than extra rows in the
main table: merged in, every Tokyo row would be blank under half the columns
and would break sorting on price and return.

Writes data/tokyo.json.
"""

import html
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SOURCE = "https://www.jpx.co.jp/english/equities/products/etfs/issues/01.html"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en",
}

MIN_EXPECTED = 150

CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CODE = re.compile(r"^\d{4}$")

# Every name carries this trailing label from the linked NAV column.
NAV_SUFFIX = re.compile(r"\s*Indicative NAV\s*$", re.I)
# Fees are annotated with footnote markers: "0.046(*3)".
FOOTNOTE = re.compile(r"\(\*+\d*\)")


def text(cell):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def parse_date(v):
    for fmt in ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_fee(v):
    v = FOOTNOTE.sub("", v or "").strip()
    m = re.search(r"\d+(?:\.\d+)?", v)
    return float(m.group()) if m else None


def issuer_of(manager):
    """Shorten the management company to the name people recognise."""
    m = (manager or "").strip()
    m = re.sub(r"\s+(Asset Management|Investments?|Investment Management|"
               r"Asset Management Co\.?,? ?Ltd\.?|Co\.?,? ?Ltd\.?)\s*$", "", m, flags=re.I)
    return m.strip(" ,.") or "Unknown"


def main():
    try:
        req = urllib.request.Request(SOURCE, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1

    out, seen = [], set()
    for row in ROW.findall(page):
        cells = [text(c) for c in CELL.findall(row)]
        if len(cells) < 7 or not CODE.fullmatch(cells[2]):
            continue
        code = cells[2]
        if code in seen:
            continue
        seen.add(code)

        name = NAV_SUFFIX.sub("", cells[3]).strip()
        index = cells[1].strip()
        out.append({
            "code": code,
            "name": name,
            "issuer": issuer_of(cells[4]),
            "manager": cells[4].strip(),
            "index": index if index and index != "-" else None,
            "listed": parse_date(cells[0]),
            "unit": cells[5].strip() or None,
            "expense": parse_fee(cells[6]),
            "marketMaker": bool(cells[7].strip()) if len(cells) > 7 else None,
        })

    if len(out) < MIN_EXPECTED:
        print(f"refusing to write: parsed only {len(out)} funds — the JPX table "
              f"layout has probably changed", file=sys.stderr)
        return 1

    out.sort(key=lambda e: e["code"])
    DATA.mkdir(exist_ok=True)
    (DATA / "tokyo.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Japan Exchange Group — listed ETF issues",
        "sourceUrl": SOURCE,
        "count": len(out),
        "funds": out,
    }, separators=(",", ":")) + "\n")

    fees = sorted(e["expense"] for e in out if e["expense"] is not None)
    print(f"wrote {len(out)} Tokyo-listed ETFs")
    print(f"  fee known for {len(fees)}/{len(out)}; median {fees[len(fees)//2]:.3f}%")
    print(f"  managers: {len({e['issuer'] for e in out})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
