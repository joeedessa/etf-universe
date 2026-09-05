#!/usr/bin/env python3
"""Build the Tokyo-listed ETF universe: JPX's issue list, enriched from the
Investment Trusts Association's fund library.

JPX lists every TSE-listed ETF (code, names, manager, index, listing date,
trust fee, a pamphlet PDF per fund). It has no size, no returns, no prospectus.
The 投信総合検索ライブラリー -- the Investment Trusts Association's fund library,
toushin-lib.fwg.ne.jp -- has a JSON search API that returns a fact sheet for
every Japanese investment trust from the fund's own filings: net assets, NAV,
fee incl. tax, 1/3/5-year NAV returns, risk, Sharpe, distributions, inception,
an OFFICIAL asset class and region, and a prospectus download.

The two are keyed differently -- JPX by 4-digit code, the library by ISIN --
and neither carries the other. They are joined by normalised fund name. That
matching was validated 6/6 against ISINs read from Nomura's own holdings
spreadsheets, and the map is persisted in data/tokyo_isin.json so a fund is
matched once and a later name tweak on either side cannot un-match it.

What cannot match, and why it is not a bug: WisdomTree's commodity ETFs are
Jersey-domiciled, SPDR S&P 500 / SPDR Gold / ABF are foreign JDRs, and the
Mitsubishi UFJ physical-metal products are 信託 rather than 投信. None is a
Japanese investment trust, so none is in the library. They keep JPX's pamphlet
and are labelled as such rather than shown with blanks.

Writes data/tokyo.json and data/tokyo_isin.json.
"""

import html
import json
import pathlib
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ISIN_MAP = DATA / "tokyo_isin.json"

JPX_EN = "https://www.jpx.co.jp/english/equities/products/etfs/issues/01.html"
JPX_JA = "https://www.jpx.co.jp/equities/products/etfs/issues/01.html"
JPX_PAMPHLET = "https://www.jpx.co.jp/equities/products/etfs/issues/files/{code}-j.pdf"

LIB = "https://toushin-lib.fwg.ne.jp"
LIB_SEARCH = LIB + "/FdsWeb/FDST999900/fundDataSearch"
LIB_DETAIL = LIB + "/FdsWeb/FDST030000?isinCd={isin}&associFundCd={afc}"
LIB_PROSPECTUS = LIB + "/FdsWeb/download?reportId={report}&updateFlag=1&associFundCd={afc}"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HTML_HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en"}
# The library's search is a DataTables call posting JSON; without these headers
# the server falls back to defaults and silently ignores the filter.
API_HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en",
               "X-Requested-With": "XMLHttpRequest",
               "Accept": "application/json, text/javascript, */*",
               "Referer": LIB + "/FdsWeb/FDST999900",
               "Content-Type": "application/json"}
# Fields the client always sends as arrays.
ARRAY_FIELDS = ["s_investAssetKindCd", "s_investArea3kindCd", "s_instCd", "s_fdsInstCd",
                "s_dcFundCD", "t_investArea10kindCd", "t_investAssetKindCd", "t_instCd",
                "t_fdsInstCd", "s_investArea10kindCd", "s_setlFqcy", "s_dividend1y",
                "s_totalNetAssets", "s_nowToRedemptionDate", "s_establishedDateToNow",
                "s_isinCd"]

# Legends read from the library's own search form.
ASSET_CLASS = {"1": "Equity", "2": "Bonds", "3": "REIT", "4": "Other assets", "5": "Balanced"}
REGIONS = {"1": "Global", "2": "Japan", "3": "North America", "4": "Europe", "5": "Asia",
           "6": "Oceania", "7": "Latin America", "8": "Africa", "9": "Middle East",
           "10": "Emerging"}

MIN_JPX = 150          # a layout change parses as a short list, not an error
MIN_LIB = 300          # the library filter returned 424 ETFs; far fewer means it broke
MIN_MATCH_RATE = 0.80  # 248/274 when built; a collapse here means a name-format change


def fetch(url, data=None, headers=None, timeout=90):
    req = urllib.request.Request(url, headers=headers or HTML_HEADERS, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def text(cell):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
CODE = re.compile(r"^\d{4}$")
NAV_SUFFIX = re.compile(r"\s*Indicative NAV(\s+Active ETF)?\s*$", re.I)
FOOTNOTE = re.compile(r"\(\*+\d*\)|\(注\d*\)|（注\d*）")


def parse_date(v):
    for fmt in ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(v.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_pct(v):
    v = FOOTNOTE.sub("", v or "")
    m = re.search(r"\d+(?:\.\d+)?", v)
    return float(m.group()) if m else None


def issuer_of(manager):
    m = (manager or "").strip()
    m = re.sub(r"\s+(Asset Management|Investments?|Investment Management|Co\.?,? ?Ltd\.?)\s*$",
               "", m, flags=re.I)
    return m.strip(" ,.") or "Unknown"


def jpx_english():
    page = fetch(JPX_EN).decode("utf-8", "replace")
    out = {}
    for row in ROW.findall(page):
        c = [text(x) for x in CELL.findall(row)]
        if len(c) < 7 or not CODE.fullmatch(c[2]):
            continue
        out[c[2]] = {
            "code": c[2],
            "name": NAV_SUFFIX.sub("", FOOTNOTE.sub("", c[3])).strip(),
            "manager": c[4].strip(),
            "issuer": issuer_of(c[4]),
            "index": (c[1].strip() or None) if c[1].strip() != "-" else None,
            "listed": parse_date(c[0]),
            "unit": c[5].strip() or None,
            "jpxFee": parse_pct(c[6]),
        }
    return out


def jpx_japanese():
    """Japanese name (for matching), manager URL and pamphlet per code."""
    page = fetch(JPX_JA).decode("utf-8", "replace")
    out = {}
    for row in ROW.findall(page):
        cells = CELL.findall(row)
        c = [text(x) for x in cells]
        if len(c) < 5 or not CODE.fullmatch(c[1]):
            continue
        links = re.findall(r'href="([^"]+)"', row)
        manager_url = next((l for l in links
                            if l.startswith("http") and "jpx.co.jp" not in l
                            and "inav" not in l.lower() and "ihsmarkit" not in l
                            and "ice.com" not in l), None)
        out[c[1]] = {"nameJa": FOOTNOTE.sub("", c[2]).replace(" iNAV", "").strip(),
                     "managerUrl": manager_url}
    return out


def library_etfs():
    """Every ETF in the fund library, one fact sheet per fund."""
    def page(start, draw):
        body = {k: [] for k in ARRAY_FIELDS}
        body.update({"f_etfKBun": "2", "s_kensakuKbn": "1", "s_keyword": "",
                     "startNo": start, "draw": draw, "searchBtnClickFlg": "1"})
        raw = fetch(LIB_SEARCH, data=json.dumps(body).encode(), headers=API_HEADERS)
        return json.loads(raw.decode("utf-8"))["searchResultInfo"]
    first = page(0, 1)
    rows = list(first["resultInfoMapList"])
    pages = int(first["allPageNo"])
    for p in range(1, pages):
        rows += page(p * 20, p + 1)["resultInfoMapList"]
    return rows, first.get("standardDate", "")[:10]


DASHES = "‐‑‒–—―−－ー〜⁃­"
STRIP = re.compile(r"\(注\d*\)|（注\d*）|®|inav|indicative nav|上場投資信託|上場投信|"
                   r"上場インデックスファンド|exchange traded fund|etf")
PUNCT = re.compile(r"[\s\-・〜~/／,，.。:：&＆'’\"()（）\[\]【】" + DASHES + "]")


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return PUNCT.sub("", STRIP.sub("", s))


def match_names(jpx_ja, lib_rows):
    """code -> library row, by normalised name. Exact, then unique substring,
    then the JPX name with its truncated tail trimmed contained in exactly one
    library name (JPX clips long names mid-token)."""
    by_norm = {}
    for r in lib_rows:
        by_norm.setdefault(norm(r["fundNm"]), []).append(r)
    keys = list(by_norm)

    def unique(cands):
        return by_norm[cands[0]][0] if len(cands) == 1 and len(by_norm[cands[0]]) == 1 else None

    out = {}
    for code, j in jpx_ja.items():
        n = norm(j["nameJa"])
        if n in by_norm and len(by_norm[n]) == 1:
            out[code] = (by_norm[n][0], "exact"); continue
        r = unique([k for k in keys if len(n) >= 6 and (n in k or k in n)])
        if r:
            out[code] = (r, "substring"); continue
        for cut in (1, 2, 3):
            t = n[:-cut]
            if len(t) < 16:
                break
            r = unique([k for k in keys if t in k])
            if r:
                out[code] = (r, "truncated"); break
    return out


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def region_of(row):
    flags = [REGIONS[str(i)] for i in range(1, 11) if str(row.get(f"investArea10kindCd{i}")) == "1"]
    return flags or None


def main():
    try:
        en = jpx_english()
        ja = jpx_japanese()
        lib_rows, lib_asof = library_etfs()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, KeyError) as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1

    if len(en) < MIN_JPX:
        print(f"refusing to write: JPX English list parsed {len(en)} funds", file=sys.stderr)
        return 1
    if len(lib_rows) < MIN_LIB:
        print(f"refusing to write: library returned {len(lib_rows)} ETFs — filter or API "
              f"shape has changed", file=sys.stderr)
        return 1

    lib_by_isin = {r["isinCd"]: r for r in lib_rows}
    isin_map = {}
    if ISIN_MAP.exists():
        try:
            isin_map = json.loads(ISIN_MAP.read_text())
        except ValueError:
            isin_map = {}

    matched = match_names(ja, lib_rows)
    # Persist: a code matched once stays matched. New matches are added; a code
    # whose cached ISIN vanished from the library falls back to a fresh match.
    for code, (row, how) in matched.items():
        if code not in isin_map:
            isin_map[code] = {"isin": row["isinCd"], "associFundCd": row["associFundCd"],
                              "how": how, "libraryName": row["fundNm"]}
    rate = sum(1 for c in en if c in isin_map and isin_map[c]["isin"] in lib_by_isin) / len(en)
    if rate < MIN_MATCH_RATE:
        print(f"refusing to write: only {rate:.0%} of JPX funds match the library "
              f"(expected ~90%) — a name format changed", file=sys.stderr)
        return 1

    funds = []
    for code in sorted(en):
        f = dict(en[code])
        f["nameJa"] = ja.get(code, {}).get("nameJa")
        f["managerUrl"] = ja.get(code, {}).get("managerUrl")
        f["pamphletUrl"] = JPX_PAMPHLET.format(code=code)
        m = isin_map.get(code)
        row = lib_by_isin.get(m["isin"]) if m else None
        if not row:
            f.update({"library": False,
                      "libraryNote": "Not a Japanese investment trust (foreign-domiciled or a "
                                     "trust), so it has no record in the fund library. "
                                     "JPX pamphlet only.",
                      "expense": f["jpxFee"]})
            funds.append(f)
            continue
        afc = row["associFundCd"]
        f.update({
            "library": True,
            "isin": row["isinCd"],
            "associFundCd": afc,
            "expense": num(row.get("trustReward")),          # % incl. tax
            "nav": num(row.get("standardPrice")),            # yen
            "netAssets": (num(row.get("totalNetAssets")) or 0) * 1e6 or None,   # yen
            "return1y": num(row.get("standardPriceRa1y")),
            "return3y": num(row.get("standardPriceRa3y")),
            "return5y": num(row.get("standardPriceRa5y")),
            "risk1y": num(row.get("riskRa1y")),
            "sharpe1y": num(row.get("sharpRa1y")),
            "dividend1y": num(row.get("dividend1y")),
            "inception": parse_date(str(row.get("establishedDate") or "")) or f["listed"],
            "assetClass": ASSET_CLASS.get(str(row.get("investAssetKindCd")), "Unclassified"),
            "regions": region_of(row),
            "prospectusUrl": LIB_PROSPECTUS.format(report=row.get("repordNo"), afc=afc)
                             if row.get("repordNo") else None,
            "libraryUrl": LIB_DETAIL.format(isin=row["isinCd"], afc=afc),
            "libraryName": row["fundNm"],
        })
        funds.append(f)

    DATA.mkdir(exist_ok=True)
    ISIN_MAP.write_text(json.dumps(dict(sorted(isin_map.items())), ensure_ascii=False, indent=0) + "\n")
    with_lib = sum(1 for f in funds if f["library"])
    (DATA / "tokyo.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "JPX listed ETF issues; Investment Trusts Association fund library",
        "libraryAsOf": lib_asof,
        "count": len(funds),
        "withLibrary": with_lib,
        "funds": funds,
    }, ensure_ascii=False, separators=(",", ":")) + "\n")

    aum = sum(f["netAssets"] for f in funds if f.get("netAssets"))
    print(f"wrote {len(funds)} Tokyo ETFs; {with_lib} with a library record "
          f"({with_lib / len(funds):.0%}), {len(funds) - with_lib} pamphlet-only")
    print(f"  library as of {lib_asof}; total net assets ¥{aum / 1e12:.1f} trillion")
    return 0


if __name__ == "__main__":
    sys.exit(main())
