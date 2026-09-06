#!/usr/bin/env python3
"""Holdings for Tokyo-listed ETFs, from each fund's daily portfolio composition file.

Every ETF on the Tokyo Stock Exchange publishes a PCF -- a portfolio composition
file listing every security in the fund, its quantity and its price -- each
business day, through the agent that computes the fund's indicative NAV. Only
three agents serve the market, so three feeds cover it:

  ICE         https://inav.ice.com/pcf-download/all/all_pcf_YYYYMMDD.zip
              one zip, ~230 funds: Nomura, BlackRock, MUFG, AM One, Simplex, ...
  IHS Markit  https://api.ebs.ihsmarkit.com/inav/   (S&P Global)
              Amova, Daiwa and Norinchukin, ~110 funds, one CSV per fund
  Solactive   https://legacy2.solactive.com/downloads/etfservices/tse-pcf/single/{code}.csv
              Global X Japan, ~65 funds

All three publish the same shape: a header row (code, name, cash component,
shares outstanding, date) then one row per line (code, name, ISIN, exchange,
currency, quantity, price). Amova's files add market value, FX rate and futures
multiplier columns. Nomura also publishes a monthly holdings workbook, used for
the one Nomura fund with no PCF on ICE.

WEIGHTS ARE COMPUTED HERE, NOT READ, so the rules matter:

  * Foreign lines are converted to yen. Amova's files carry their own FX rate
    per line; for the others a daily open rate table is used (open.er-api.com,
    with a second source as fallback -- both key-free, both cover every
    currency seen in the files).
  * Bonds are priced per 100 of face, so a bond line is quantity x price / 100.
    Bond lines are recognised by exchange (OTC / NONE / blank) plus an ISIN or
    security identifier; T-bills arrive with blank code AND blank name.
  * Cash lines take three forms: an amount with a yen-per-unit rate (MUFG),
    an amount with a units-per-yen rate (Daiwa), or an amount alone. The rate's
    orientation is decided by comparing it to the open rate, never assumed.
  * Futures have no market value -- their notional is exposure, not asset -- so
    they are kept OUT of the denominator and listed separately, with a notional
    only where the file states the contract multiplier (Amova). FX forwards
    (hedges) are likewise excluded; their unrealised value is small and shows
    up as a residual in the reconciliation.
  * The denominator is the fund's own bottom-up value: header cash + every
    security, bond and cash line. Amova states AUM directly and that is used.

EVERY FUND IS RECONCILED against the fund library's net assets (data/tokyo.json,
an independent source). Within 3% is "ok"; within 15% is "approx" (hedged bond
funds, whose forward gains are not in the file, and tiny funds whose basket is
dated a day ahead); beyond that "off", and the page says so instead of showing
weights as fact. A misread file -- bonds x100, a doubled FX conversion -- lands
at 0.01x or 100x and is impossible to miss.

Writes data/tokyo_holdings.json (per fund: top 10, rollups, derivatives, the
reconciliation) and data/tokyo_positions/{code}.json (largest 500 lines, fetched
by the page on demand). Refuses to write if fewer than MIN_FUNDS parse or
fewer than MIN_OK_RATE of them reconcile -- a broken feed must not overwrite a
good file with a bad one.

  python scripts/fetch_tokyo_holdings.py
  python scripts/fetch_tokyo_holdings.py --codes 1306 1320   # debug a few
"""

import argparse
import concurrent.futures
import csv
import io
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "tokyo_holdings.json"
POSITIONS_DIR = DATA / "tokyo_positions"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

ICE_LIST = "https://inav.ice.com/pcf-download/listOfZips"
ICE_ZIP = "https://inav.ice.com/pcf-download/all/{name}"
ICE_HEADERS = {"User-Agent": UA, "Referer": "https://inav.ice.com/tse/iopv/table"}
ICE_MEMBER = re.compile(r"(\w+?)(?:tse|ose)pcf", re.I)

IHS_BASE = "https://api.ebs.ihsmarkit.com/inav/"
IHS_HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Origin": "https://ebs.ihsmarkit.com", "Referer": "https://ebs.ihsmarkit.com/inav/"}

SOLACTIVE = "https://legacy2.solactive.com/downloads/etfservices/tse-pcf/single/{code}.csv"
NOMURA_XLSX = "https://www.nomura-am.co.jp/fund/monthly_holdings/{code}_brd_data.xlsx"

FX_SOURCES = [
    ("open.er-api.com", "https://open.er-api.com/v6/latest/JPY",
     lambda j: (j["rates"], j.get("time_last_update_utc", ""))),
    ("fawazahmed0/currency-api",
     "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/jpy.json",
     lambda j: ({k.upper(): v for k, v in j["jpy"].items()}, j.get("date", ""))),
]

TOP_N = 10
POSITIONS_CAP = 500
ROLLUP_KEEP = 10
ROLLUP_MIN_PCT = 0.5
OK_PCT = 3.0        # reconciliation band for "ok"
APPROX_PCT = 15.0   # ... for "approx"; beyond is "off"
MIN_FUNDS = 200     # 241 when built
MIN_OK_RATE = 0.85  # ok + approx, of parsed
WORKERS = 6

# A price that sits within this of the open FX rate (or its inverse) IS an FX
# rate. The widest gap seen was 6% (an onshore/offshore yuan quote); orders of
# magnitude separate the alternatives, so the band is generous.
RATE_TOL = 0.12

FUTURES_EXCHANGES = {"OSE", "XOSE", "CME", "XCME", "NYM", "XNYM", "CMX", "XCEC", "EUX",
                     "XEUR", "ICF", "IFEU", "XCBT", "CBT", "SGX", "XSIM", "HKF", "XHKF", "NGC"}
FUTURES_NAME = re.compile(
    r"\b(FUTR?|FUTURES?|E-?MINI|MINI|MIC|MIN|INDX|IX FU|ST 2\d{3}|IX ?2\d{3})\b"
    r"|\b[A-Z ]{2,12} ?2\d{3}$|先物", re.I)
DATE8 = re.compile(r"^\d{8}$")
CCY_DATE8 = re.compile(r"^[A-Z]{3}\d{8}$")
IDENTIFIER = re.compile(r"^[A-Z0-9]{7,12}$")   # SEDOL, CUSIP, ISIN
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
CASH_CODES = {"cash", "margin", "cash & others"}

# Exchange (MIC or agent shorthand) -> country, for lines with no ISIN.
EXCHANGE_COUNTRY = {
    "TSE": "JP", "XTKS": "JP", "XJAS": "JP", "OSE": "JP", "XOSE": "JP", "SHG": "CN", "XSHG": "CN",
    "XSHE": "CN", "SHE": "CN", "XHKG": "HK", "HKG": "HK", "XNYS": "US", "XNAS": "US", "XNGS": "US",
    "XNMS": "US", "XNCM": "US", "ARCX": "US", "BATS": "US", "UN": "US", "UW": "US", "UA": "US",
    "XASE": "US", "XLON": "GB", "LSE": "GB", "XETR": "DE", "XFRA": "DE", "XPAR": "FR", "XAMS": "NL",
    "XBRU": "BE", "XMIL": "IT", "XMAD": "ES", "XLIS": "PT", "XSWX": "CH", "XVTX": "CH", "XWBO": "AT",
    "XDUB": "IE", "XSTO": "SE", "XCSE": "DK", "XOSL": "NO", "XHEL": "FI", "XWAR": "PL", "XPRA": "CZ",
    "XBUD": "HU", "XATH": "GR", "XIST": "TR", "XTAE": "IL", "XJSE": "ZA", "XTSE": "CA", "XTSX": "CA",
    "XMEX": "MX", "BVMF": "BR", "XBSP": "BR", "XSGO": "CL", "XBOG": "CO", "XLIM": "PE",
    "XBUE": "AR", "XASX": "AU", "XNZE": "NZ", "XSES": "SG", "XKRX": "KR", "XKOS": "KR", "XTAI": "TW",
    "ROCO": "TW", "XBOM": "IN", "XNSE": "IN", "XKLS": "MY", "XBKK": "TH", "XIDX": "ID", "XPHS": "PH",
    "XSAU": "SA", "XADS": "AE", "DFM": "AE", "XDFM": "AE", "XKUW": "KW", "DSMD": "QA", "XCAI": "EG",
}

KIND_LABEL = {"sec": "Listed securities", "bond": "Bonds & bills", "cash": "Cash & margin"}


# ---------------------------------------------------------------- fetching

def fetch(url, headers=None, timeout=90, attempts=2):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                raise
            time.sleep(1.5 * (i + 1))
    raise last


def fx_table():
    """Currency -> yen per unit. Tries each source in turn; fails loudly if none
    answers, because a stale or absent rate would silently mis-weight every
    foreign line."""
    for name, url, pick in FX_SOURCES:
        try:
            rates, asof = pick(json.loads(fetch(url, timeout=60)))
        except Exception as exc:  # noqa: BLE001 - any failure means try the next source
            print(f"  fx: {name} failed ({exc}); trying next", flush=True)
            continue
        table = {k.upper(): 1.0 / v for k, v in rates.items() if v}
        table["JPY"] = 1.0
        table.setdefault("CNH", table.get("CNY"))
        return table, name, asof
    raise SystemExit("no FX source answered — refusing to compute weights without rates")


def ice_files():
    """code -> CSV text, from the latest daily zip."""
    listing = fetch(ICE_LIST, ICE_HEADERS, timeout=60).decode("utf-8", "replace")
    names = sorted(set(re.findall(r"all_pcf_\d{8}\.zip", listing)))
    if not names:
        raise RuntimeError("ICE listOfZips returned no zip names")
    name = names[-1]
    blob = fetch(ICE_ZIP.format(name=name), ICE_HEADERS, timeout=180)
    out = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for member in z.namelist():
            m = ICE_MEMBER.match(member.rsplit("/", 1)[-1])
            if m:
                out[m.group(1)] = z.read(member).decode("utf-8-sig", "replace")
    print(f"  ICE: {name}, {len(out)} funds", flush=True)
    return out


def ihs_index():
    """code -> file name, from the agent's fund table."""
    data = json.loads(fetch(IHS_BASE + "data?language=en", IHS_HEADERS, timeout=90))
    rows = [r for grp in data.get("funds", []) for r in grp.get("inavFunds", [])]
    return {str(r["code"]): r["fileName"] for r in rows if r.get("fileName")}


def ihs_file(filename):
    url = IHS_BASE + "getfile?filename=" + urllib.parse.quote(filename)
    return fetch(url, IHS_HEADERS, timeout=90).decode("utf-8-sig", "replace")


def solactive_file(code):
    try:
        body = fetch(SOLACTIVE.format(code=code), timeout=60)
    except urllib.error.HTTPError:
        return None
    text = body.decode("utf-8-sig", "replace")
    return text if "Shares Outstanding" in text else None


# ---------------------------------------------------------------- parsing

def parse_pcf(text):
    """-> (header dict, lowercased column list, [line dict])."""
    rows = list(csv.reader(io.StringIO(text)))
    head = [c.strip() for c in rows[0]]
    vals = rows[1] if len(rows) > 1 else []
    header = {head[i]: (vals[i].strip() if i < len(vals) else "") for i in range(len(head)) if head[i]}
    start = next(i for i, r in enumerate(rows) if r and r[0].strip().lower() == "code")
    cols = [c.strip().lower() for c in rows[start]]
    idx = {c: i for i, c in enumerate(cols)}

    def cell(row, *names):
        for n in names:
            i = idx.get(n)
            if i is not None and i < len(row):
                return row[i].strip()
        return ""

    def num(s):
        try:
            return float(s) if s not in ("", None) else None
        except ValueError:
            return None

    lines = []
    for row in rows[start + 1:]:
        if not any(c.strip() for c in row):
            continue
        qty = num(cell(row, "shares amount", "shares"))
        price = num(cell(row, "stock price"))
        if qty is None and price is None:
            continue
        lines.append({
            "code": cell(row, "code"), "name": cell(row, "name"), "isin": cell(row, "isin"),
            "exch": cell(row, "exchange").upper(), "ccy": (cell(row, "currency") or "JPY").upper(),
            "qty": qty or 0.0, "px": price or 0.0,
            "mv": num(cell(row, "market value")), "fx": num(cell(row, "fx rate")),
            "mult": num(cell(row, "future multiplier")),
        })
    return header, cols, lines


def near(a, b, tol=RATE_TOL):
    return a > 0 and b > 0 and abs(a / b - 1) <= tol


def classify_row(p, fx):
    """-> 'sec' | 'bond' | 'cash' | 'future' | 'fxfwd' | 'skip'."""
    code, name, exch, ccy, px = p["code"], p["name"], p["exch"], p["ccy"], p["px"]
    cl, cu, nu = code.lower(), code.upper(), name.upper()
    if cl == "disclaimer" or nu.startswith("DISCLAIMER"):
        return "skip"
    # Hedging forwards: "FX FORWARD" as code, or a currency+date / date as name.
    if (cu.startswith("FX FORWARD") or cu.startswith("FX FWD") or nu.startswith("FX FORWARD")
            or CCY_DATE8.match(name) or DATE8.match(name) or DATE8.match(code)):
        return "fxfwd"
    if cl in CASH_CODES or cu == ccy or nu.startswith("CASH"):
        return "cash"
    if not code and not name:
        # Blank/blank is either a currency balance (price = FX rate) or a T-bill
        # (ISIN, price near 100). The rate comparison decides.
        rate = fx.get(ccy)
        if rate and (near(px, rate) or near(px, 1 / rate)):
            return "cash"
        if p["isin"] and 10 < px < 300:
            return "bond"
        return "cash"
    # A futures line may carry a contract code (JGSU6, XIDU6) in the ISIN column;
    # only a real ISIN protects a name from the futures pattern.
    if p["mult"] or exch in FUTURES_EXCHANGES or (not ISIN.match(p["isin"].upper()) and FUTURES_NAME.search(name)):
        return "future"
    if cl == "bond":
        return "bond"
    if exch in ("OTC", "NONE", "") and (p["isin"] or IDENTIFIER.match(cu)) and 10 < px < 300:
        return "bond"
    return "sec"


def row_value(p, kind, has_mv, fx):
    """Yen value of a sec / bond / cash line."""
    ccy = p["ccy"]
    rate = fx.get(ccy) or 0.0
    if has_mv:
        # Amova: market value in the line's currency (bonds x100 of value), FX per line.
        r = p["fx"] if p["fx"] else (1.0 if ccy == "JPY" else rate)
        base = p["mv"] if p["mv"] is not None else p["qty"] * p["px"]
        if kind == "bond":
            base /= 100.0
        return base * r
    px = p["px"]
    if kind == "cash":
        if ccy == "JPY":
            return p["qty"] * (px if px and near(px, 1.0, 0.01) else 1.0)
        if px and near(px, rate):          # yen per unit of currency (MUFG)
            return p["qty"] * px
        if px and rate and near(px, 1 / rate):   # units of currency per yen (Daiwa)
            return p["qty"] / px
        return p["qty"] * rate
    value = p["qty"] * px * (1.0 if ccy == "JPY" else rate)
    return value / 100.0 if kind == "bond" else value


def country_of(p):
    isin = p["isin"].upper()
    if ISIN.match(isin):
        return isin[:2]
    return EXCHANGE_COUNTRY.get(p["exch"])


def to_num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def build_fund(code, text, source, fx, ref_net_assets):
    """One fund's holdings record + full position list, or None if unparseable."""
    header, cols, lines = parse_pcf(text)
    has_mv = "market value" in cols
    asof = header.get("Fund Date") or ""
    if DATE8.match(asof):
        asof = f"{asof[:4]}-{asof[4:6]}-{asof[6:]}"
    header_cash = to_num(header.get("Fund Cash Component") or header.get("Cash & Others")) or 0.0
    aum = to_num(header.get("AUM"))

    # Amova states AUM and lists its cash as lines; its "Cash & Others" header is
    # a balancing figure that would double-count against them. Everyone else's
    # header cash IS the fund's cash, and is the only place it appears.
    totals = {"sec": 0.0, "bond": 0.0, "cash": 0.0 if aum else header_cash}
    positions, derivatives, n_fwd = [], [], 0
    for p in lines:
        kind = classify_row(p, fx)
        if kind == "skip":
            continue
        if kind == "future":
            notional = None
            if p["mult"]:
                r = p["fx"] if p["fx"] else fx.get(p["ccy"], 0.0)
                notional = p["qty"] * p["px"] * p["mult"] * r
            derivatives.append({"name": p["name"] or p["code"], "exchange": p["exch"] or None,
                                "contracts": p["qty"], "notionalYen": notional})
            continue
        if kind == "fxfwd":
            n_fwd += 1
            continue
        value = row_value(p, kind, has_mv, fx)
        totals[kind] += value
        if kind == "cash":
            continue
        positions.append({"name": p["name"] or p["code"] or p["isin"], "yen": value,
                          "country": country_of(p), "ccy": p["ccy"], "kind": kind,
                          "isin": p["isin"] or None})

    bottom_up = totals["sec"] + totals["bond"] + totals["cash"]
    # The denominator is the fund's own total: stated AUM where the file gives
    # one (Amova), otherwise the bottom-up sum. The CHECK is always my computed
    # lines against an independent figure -- the stated AUM, or failing that the
    # fund library's net assets -- so it tests the arithmetic, not two official
    # numbers against each other.
    denominator = aum if aum else bottom_up
    if not denominator or denominator <= 0:
        return None

    check = None
    reference = aum or ref_net_assets
    if reference:
        ratio = bottom_up / reference
        dev = abs(ratio - 1) * 100
        check = {"ratio": round(ratio, 4), "against": "stated AUM" if aum else "library net assets",
                 "status": "ok" if dev <= OK_PCT else "approx" if dev <= APPROX_PCT else "off"}

    def pct(v):
        return round(v / denominator * 100, 4)

    positions.sort(key=lambda x: -abs(x["yen"]))
    by_country, by_ccy = {}, {}
    for x in positions:
        by_country[x["country"] or "??"] = by_country.get(x["country"] or "??", 0.0) + x["yen"]
        by_ccy[x["ccy"]] = by_ccy.get(x["ccy"], 0.0) + x["yen"]
    by_asset = {KIND_LABEL[k]: pct(v) for k, v in totals.items() if abs(v / denominator) >= 0.00005}
    notional = sum(d["notionalYen"] for d in derivatives if d["notionalYen"] is not None)
    known = all(d["notionalYen"] is not None for d in derivatives)

    record = {
        "source": source, "asOf": asof, "positions": len(positions),
        "top": [{"name": x["name"], "pct": round(x["yen"] / denominator * 100, 2),
                 "country": x["country"], "ccy": x["ccy"], "kind": x["kind"]}
                for x in positions[:TOP_N]],
        "byCountry": condense({k: pct(v) for k, v in by_country.items()}),
        "byCurrency": condense({k: pct(v) for k, v in by_ccy.items()}),
        "byAsset": by_asset,
        "derivatives": {"contracts": len(derivatives),
                        "names": sorted({d["name"] for d in derivatives})[:6],
                        "notionalPct": round(notional / denominator * 100, 2) if derivatives and known else None},
        "fxForwards": n_fwd,
        "denominatorYen": round(denominator),
        "check": check,
    }
    full = {"code": code, "source": source, "asOf": asof, "denominatorYen": round(denominator),
            "total": len(positions), "shown": min(len(positions), POSITIONS_CAP),
            "positions": [{"name": x["name"], "pct": pct(x["yen"]), "country": x["country"],
                           "ccy": x["ccy"], "kind": x["kind"], "isin": x["isin"]}
                          for x in positions[:POSITIONS_CAP]]}
    return record, full


def condense(d):
    """Largest slices, tail summed into "Other" -- same shape as the US rollups."""
    if not d:
        return {}
    items = sorted(d.items(), key=lambda kv: -abs(kv[1]))
    out, tail = {}, 0.0
    for key, p in items:
        if len(out) < ROLLUP_KEEP and abs(p) >= ROLLUP_MIN_PCT:
            out[key] = round(p, 2)
        else:
            tail += p
    if abs(tail) >= 0.005:
        out["Other"] = round(tail, 2)
    return out


# ---------------------------------------------------------------- Nomura workbook

def xlsx_rows(blob, sheet="xl/worksheets/sheet2.xml"):
    """Cells of one sheet as [{col letter: text}], stdlib only."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            shared = ["".join(re.findall(r"<t[^>]*>([^<]*)</t>", si))
                      for si in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode(), re.S)]
        xml = z.read(sheet).decode()
    rows = []
    for row in re.findall(r"<row [^>]*>(.*?)</row>", xml, re.S):
        cells = {}
        for m in re.finditer(r'<c r="([A-Z]+)\d+"((?:\s+[a-z]+="[^"]*")*)\s*(?:/>|>(.*?)</c>)', row, re.S):
            col, attrs, inner = m.groups()
            t = re.search(r't="(\w+)"', attrs or "")
            v = re.search(r"<v>([^<]*)</v>", inner or "")
            val = None
            if v:
                val = shared[int(v.group(1))] if t and t.group(1) == "s" else v.group(1)
            elif inner and "<t" in inner:
                val = "".join(re.findall(r"<t[^>]*>([^<]*)</t>", inner))
            cells[col] = val
        rows.append(cells)
    return rows


def build_nomura(code, blob, fx, ref_net_assets):
    """Nomura's monthly workbook already states yen value and % of NAV per line."""
    rows = xlsx_rows(blob)
    asof = ""
    for r in rows[:4]:
        for v in r.values():
            m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", v or "")
            if m:
                asof = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    hdr_i = next(i for i, r in enumerate(rows) if any("% of NAV" in (v or "") for v in r.values()))
    hdr = rows[hdr_i]

    def col(*needles):
        for c, v in hdr.items():
            if v and any(n in v for n in needles):
                return c
        return None
    c_name_en, c_name = col("Name"), col("銘柄")
    c_isin, c_ctry, c_ccy = col("ISIN"), col("Country"), col("Currency")
    c_yen, c_pct = col("Valuation (y", "評価金額(円"), col("% of NAV")
    nav = None
    positions, derivatives = [], []
    for r in rows[hdr_i + 1:]:
        label = " ".join(v for v in r.values() if v)
        if "Total Net Assets" in label or "純資産総額" in label:
            nav = to_num(r.get(c_yen))
            continue
        if "合計" in label or not r.get(c_pct):
            continue
        yen = to_num(r.get(c_yen))
        if yen is None:
            continue
        name = (r.get(c_name_en) or r.get(c_name) or "").strip()
        isin = (r.get(c_isin) or "").strip()
        if isin == "-" or not name:
            if re.search(r"\d{4}$", name):
                derivatives.append({"name": name, "exchange": None,
                                    "contracts": to_num(r.get(col("Quantity", "数量", "枚数"))), "notionalYen": yen})
                continue
        positions.append({"name": name, "yen": yen, "isin": isin if ISIN.match(isin) else None,
                          "country": (r.get(c_ctry) or "").strip() or (isin[:2] if ISIN.match(isin) else None),
                          "ccy": (r.get(c_ccy) or "JPY").strip(), "kind": "bond" if re.match(r"^[A-Z]{2}\d", isin) and c_ccy and "債" in label else "sec"})
    if not nav or not positions:
        return None
    denominator = nav

    def pct(v):
        return round(v / denominator * 100, 4)
    positions.sort(key=lambda x: -abs(x["yen"]))
    by_country, by_ccy = {}, {}
    for x in positions:
        by_country[x["country"] or "??"] = by_country.get(x["country"] or "??", 0.0) + x["yen"]
        by_ccy[x["ccy"]] = by_ccy.get(x["ccy"], 0.0) + x["yen"]
    held = sum(x["yen"] for x in positions)
    check = None
    if ref_net_assets:
        ratio = denominator / ref_net_assets
        dev = abs(ratio - 1) * 100
        check = {"ratio": round(ratio, 4), "against": "library net assets",
                 "status": "ok" if dev <= OK_PCT else "approx" if dev <= APPROX_PCT else "off"}
    record = {
        "source": "nomura", "asOf": asof, "positions": len(positions),
        "top": [{"name": x["name"], "pct": round(x["yen"] / denominator * 100, 2), "country": x["country"],
                 "ccy": x["ccy"], "kind": x["kind"]} for x in positions[:TOP_N]],
        "byCountry": condense({k: pct(v) for k, v in by_country.items()}),
        "byCurrency": condense({k: pct(v) for k, v in by_ccy.items()}),
        "byAsset": {"Listed securities": pct(held), "Cash & margin": pct(denominator - held)},
        "derivatives": {"contracts": len(derivatives), "names": sorted({d["name"] for d in derivatives})[:6],
                        "notionalPct": round(sum(d["notionalYen"] for d in derivatives) / denominator * 100, 2) if derivatives else None},
        "fxForwards": 0, "denominatorYen": round(denominator), "check": check,
    }
    full = {"code": code, "source": "nomura", "asOf": asof, "denominatorYen": round(denominator),
            "total": len(positions), "shown": min(len(positions), POSITIONS_CAP),
            "positions": [{"name": x["name"], "pct": pct(x["yen"]), "country": x["country"], "ccy": x["ccy"],
                           "kind": x["kind"], "isin": x["isin"]} for x in positions[:POSITIONS_CAP]]}
    return record, full


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*", help="only these codes (debug; does not write)")
    args = ap.parse_args()

    tokyo = json.loads((DATA / "tokyo.json").read_text())
    funds = {f["code"]: f for f in tokyo["funds"]}
    wanted = set(args.codes) if args.codes else set(funds)

    fx, fx_source, fx_asof = fx_table()
    print(f"  fx: {fx_source} ({fx_asof}), {len(fx)} currencies", flush=True)

    texts = {}   # code -> (source, text or bytes)
    ice = ice_files()
    for code in wanted & set(ice):
        texts[code] = ("ice", ice[code])

    ihs = ihs_index()
    todo = sorted(wanted & set(ihs) - set(texts))
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        for code, text in zip(todo, ex.map(lambda c: ihs_file(ihs[c]), todo)):
            texts[code] = ("ihs", text)
    print(f"  IHS Markit: {len(todo)} funds", flush=True)

    # Solactive has no index; try every remaining library fund (cheap 404s).
    todo = sorted(c for c in wanted - set(texts) if funds[c].get("library"))
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        got = [(c, t) for c, t in zip(todo, ex.map(solactive_file, todo)) if t]
    for code, text in got:
        texts[code] = ("solactive", text)
    print(f"  Solactive: {len(got)} funds", flush=True)

    nomura = sorted(c for c in wanted - set(texts) if funds[c].get("issuer") == "Nomura")
    for code in nomura:
        try:
            texts[code] = ("nomura", fetch(NOMURA_XLSX.format(code=code), timeout=90))
        except urllib.error.HTTPError:
            pass
    print(f"  Nomura workbook: {sum(1 for v in texts.values() if v[0] == 'nomura')} funds", flush=True)

    out, fulls, failed = {}, {}, []
    for code in sorted(texts):
        source, body = texts[code]
        ref = funds[code].get("netAssets")
        try:
            built = (build_nomura(code, body, fx, ref) if source == "nomura"
                     else build_fund(code, body, source, fx, ref))
        except Exception as exc:  # noqa: BLE001 - one bad file must not sink the run
            failed.append((code, source, repr(exc)[:80]))
            continue
        if not built:
            failed.append((code, source, "empty"))
            continue
        out[code], fulls[code] = built

    status = {"ok": 0, "approx": 0, "off": 0, "unchecked": 0}
    for r in out.values():
        status[r["check"]["status"] if r["check"] else "unchecked"] += 1
    by_source = {}
    for r in out.values():
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    lib_total = sum(1 for f in funds.values() if f.get("library"))
    print(f"\nparsed {len(out)} funds {by_source}; failed {len(failed)}: {failed[:6]}")
    print(f"reconciliation vs library net assets: {status}")
    for code, r in sorted(out.items()):
        if r["check"] and r["check"]["status"] != "ok":
            print(f"   {r['check']['status']:<6} {code} {r['source']:<9} ratio {r['check']['ratio']:.3f}  "
                  f"{funds[code]['name'][:50]}")
    print(f"coverage: {len(out)} of {lib_total} library funds, {len(funds)} listed")

    if args.codes:
        for code in sorted(out):
            print(json.dumps(out[code], ensure_ascii=False, indent=1)[:1500])
        return 0
    if len(out) < MIN_FUNDS:
        print(f"only {len(out)} funds parsed (< {MIN_FUNDS}) — refusing to write", file=sys.stderr)
        return 1
    reconciled = status["ok"] + status["approx"]
    if reconciled < MIN_OK_RATE * len(out):
        print(f"only {reconciled}/{len(out)} reconcile — refusing to write", file=sys.stderr)
        return 1

    POSITIONS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in POSITIONS_DIR.glob("*.json"):
        if stale.stem not in fulls:
            stale.unlink()
    for code, full in fulls.items():
        (POSITIONS_DIR / f"{code}.json").write_text(json.dumps(full, ensure_ascii=False, separators=(",", ":")) + "\n")
    dates = sorted({r["asOf"] for r in out.values() if r["asOf"]})
    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Daily portfolio composition files via ICE, IHS Markit and Solactive; Nomura monthly workbook",
        "fx": {"source": fx_source, "asOf": fx_asof},
        "asOfRange": [dates[0], dates[-1]] if dates else None,
        "count": len(out), "bySource": by_source, "check": status,
        "thresholds": {"okPct": OK_PCT, "approxPct": APPROX_PCT},
        "funds": dict(sorted(out.items())),
    }, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"wrote {OUT.name} and {len(fulls)} position files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
