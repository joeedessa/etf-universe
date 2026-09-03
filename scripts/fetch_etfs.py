#!/usr/bin/env python3
"""Build the US ETF universe from the public Nasdaq screener.

The screener is the only free source that returns the whole listed universe in
one request (~5,200 funds). What it does NOT return is anything a fund fact
sheet would give you: expense ratio, AUM, inception date, issuer, asset class.
So everything below the raw quote fields is DERIVED FROM THE FUND NAME, and is
labelled as such in the UI. Name-derivation is good enough to filter on and
wrong often enough that it must never be presented as authoritative.

Writes:
  data/etfs.json  — one record per fund
  data/meta.json  — counts, breakdowns, timestamps
"""

import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

SCREENER_URL = (
    "https://api.nasdaq.com/api/screener/etf"
    "?tableonly=true&limit=25&offset=0&download=true"
)

# Nasdaq rejects requests without a browser-ish UA (403, empty body).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# A fetch that returns a fraction of the universe means the endpoint changed
# shape or is degraded. Overwriting 5,000 good records with 40 bad ones is the
# failure mode worth guarding against, since the workflow commits blind.
MIN_EXPECTED_ROWS = 3000


# ------------------------------------------------------- name cleanup

# Nasdaq reports the SEC registrant, which for many small sponsors is a shared
# umbrella trust: "Collaborative Investment Series Trust Anydrus Advantage ETF"
# is an Anydrus fund, not a Collaborative one. Left in place, the issuer filter
# fills up with shell trusts and the real sponsor is unreachable.
UMBRELLA_TRUSTS = [
    "Collaborative Investment Series Trust", "The Advisors Inner Circle Fund",
    "Investment Managers Series Trust", "Northern Lights Fund Trust",
    "Series Portfolios Trust", "Managed Portfolio Series", "ETF Series Solutions",
    "Two Roads Shared Trust", "Listed Funds Trust", "EA Series Trust",
    "World Funds Trust", "Ultimus Managers Trust", "Trust for Advised Portfolios",
    "The RBB Fund", "Starboard Investment Trust", "Unified Series Trust",
    "Elevation Series Trust", "ETF Opportunities Trust",
    "Exchange Listed Funds Trust", "Trust I", "Trust II",
]

# "<Sponsor words> Trust <numeral> <real fund name>". The leading span is
# GREEDY and capped at 60 chars so that, in a name carrying two shell markers
# ("First Trust Exchange-Traded Fund VIII FT Vest ..."), the LAST one inside the
# window wins — a non-greedy match would stop at "First Trust" and leave the
# registrant boilerplate in place.
_NUM = r"(?:I{1,3}|IV|VI{0,3}|IX|XI{0,2}|\d{1,2})"
REGISTRANT_PREFIX = re.compile(
    r"^.{0,60}\b(?:"
    # Unambiguous shell markers — the series numeral after them is optional.
    r"(?:ETF\s+Trust|Exchange[-\s]Traded\s+Funds?(?:\s+Trust)?"
    r"|Series\s+Trust|Funds?\s+Trust)(?:\s+" + _NUM + r")?"
    # A bare "Trust" ONLY when a series numeral follows it. Without that
    # requirement this branch eats the sponsor out of every First Trust fund:
    # "First Trust Nasdaq Cybersecurity ETF" -> "Nasdaq Cybersecurity ETF",
    # silently reassigning 100+ funds to a sponsor named "Nasdaq".
    r"|Trust\s+" + _NUM +
    r")\s+(?=\S)",
    re.I,
)

# The remainder has to still look like a fund. Without this, "Trust" appearing
# legitimately inside a product name gets treated as boilerplate and the name
# is destroyed: "iShares Bitcoin Trust ETF" -> "ETF".
LOOKS_LIKE_FUND = re.compile(
    r"\b(ETF|ETN|Fund|Shares|Portfolio|Strategy|Index|Trust)\b", re.I
)
LEADING_NUMERAL = re.compile(r"^(?:I{1,3}|IV|VI{0,3}|IX|XI{0,2}|\d{1,2})\b", re.I)

# Nasdaq clips companyName at this length; names at or beyond it lost their tail.
NAME_TRUNCATION_LEN = 61


def _plausible_fund(candidate: str, truncated: bool) -> bool:
    """Every real registrant shell leaves >=3 words behind; the false positives
    ("Unit", "Series 1", "ETF") all leave one or two.

    The fund-word check is skipped for source-truncated names, whose tail —
    and therefore the trailing "ETF" — was cut off before we ever saw it.
    """
    return (
        len(candidate) >= 12
        and len(candidate.split()) >= 3
        and (truncated or bool(LOOKS_LIKE_FUND.search(candidate)))
        and not LEADING_NUMERAL.match(candidate)
    )


def _strip(name: str, prefix_len: int, truncated: bool) -> str:
    """Drop a leading shell of prefix_len chars, plus any series numeral left
    stranded behind it, and keep the result only if it still reads as a fund."""
    candidate = name[prefix_len:].strip()
    candidate = LEADING_NUMERAL.sub("", candidate, count=1).strip()
    candidate = re.sub(r"\s+", " ", candidate).strip(" -,")
    return candidate if _plausible_fund(candidate, truncated) else name


# Nasdaq ships some names as two fund names run together, either the same one
# twice ("Archer Growth ETF Archer Growth ETF") or a stale sibling followed by
# the real one ("Tradr 2X Long CIEN Daily ETF Tradr 2X Long SK hynix Daily
# ETF" -- SKHA is the SK hynix fund, not the CIEN one).
#
# The tell is the tail restarting with the same word the name opened with.
# Splitting on any mid-string "ETF" WITHOUT that check would wreck 299 legitimate
# names -- "Innovator U.S. Equity Buffer ETF - April", "Goldman Sachs Physical
# Gold ETF Shares" -- whose trailing words are part of the fund's identity.
CONCAT_SPLIT = re.compile(r"\bETF\s+(?=\S)")


def dedupe_concatenated(name: str) -> str:
    m = CONCAT_SPLIT.search(name)
    if not m:
        return name
    head, tail = name[:m.end()].strip(), name[m.end():].strip()
    if not head or not tail:
        return name
    if head.split()[0].lower() != tail.split()[0].lower():
        return name
    # A tail that is a prefix of the head is the same name clipped by the
    # 61-char limit, so the head is the complete copy. A tail that diverges is
    # a different fund -- and the one this ticker actually is.
    return head if head.lower().startswith(tail.lower()) else tail


def clean_name(name: str) -> str:
    original = name
    # Nasdaq clips companyName at 61 characters, so long names arrive with the
    # trailing "ETF" already missing.
    truncated = len(name) >= NAME_TRUNCATION_LEN

    for trust in UMBRELLA_TRUSTS:
        if name.lower().startswith(trust.lower() + " "):
            name = _strip(name, len(trust), truncated)
            break

    m = REGISTRANT_PREFIX.match(name)
    if m:
        name = _strip(name, m.end(), truncated)

    name = dedupe_concatenated(name)
    name = re.sub(r"\s+", " ", name).strip(" -,")
    return name if len(name) >= 8 else original


# ---------------------------------------------------------------- issuers

# Matched as a prefix of the fund name, longest first, so "Global X" wins over
# a bare "Global" and "JPMorgan Betabuilders" collapses into "JPMorgan".
ISSUERS = [
    "State Street", "DoubleLine", "Leverage Shares", "Tradr", "T-Rex",
    "Corgi", "Brown Advisory", "IMGP", "Russell Investments", "Cambiar",
    "Carbon Collective", "GraniteShares", "Bahl & Gaynor", "Sterling",
    "iShares", "Vanguard", "SPDR", "Invesco", "Schwab", "First Trust",
    "ProShares", "VanEck", "Global X", "Direxion", "WisdomTree", "JPMorgan",
    "J.P. Morgan", "Fidelity", "PIMCO", "Dimensional", "Janus Henderson",
    "Franklin", "Xtrackers", "ARK", "Amplify", "Pacer", "Innovator",
    "Simplify", "Roundhill", "YieldMax", "Defiance", "GraniteShares",
    "Goldman Sachs", "T. Rowe Price", "Neuberger Berman", "Alpha Architect",
    "AdvisorShares", "Themes", "Harbor", "Hartford", "Nuveen", "abrdn",
    "Sprott", "Grayscale", "Bitwise", "KraneShares", "Matthews", "Avantis",
    "American Century", "Capital Group", "Calamos", "Strive", "Cambria",
    "BondBloxx", "Angel Oak", "Virtus", "Tidal", "REX", "Tuttle", "TCW",
    "Putnam", "Federated Hermes", "Principal", "SEI", "Touchstone",
    "Vident", "Eaton Vance", "Morgan Stanley", "BlackRock", "Alger",
    "Motley Fool", "AXS", "Sound", "Aptus", "Astoria", "FT Vest",
    "Fount", "Freedom", "Hashdex", "Horizon", "Inspire", "IQ", "John Hancock",
    "Kurv", "LeaderShares", "Lyrical", "Madison", "Main", "Meridian",
    "NightShares", "Northern Lights", "Overlay", "Peerless", "Point Bridge",
    "Quadratic", "Rareview", "Regan", "Return Stacked", "SPDR Portfolio",
    "Teucrium", "Timothy Plan", "Unlimited", "USCF", "Volatility Shares",
    "Westwood", "Zacks", "AAM", "Alpha", "Applied", "BNY Mellon", "Brandes",
    "Bridgeway", "Build", "Cabana", "Clough", "Columbia", "Convergence",
    "Davis", "Day Hagan", "Democracy", "Distillate", "Donoghue", "Dynamic",
    "Ecofin", "Emles", "Engine No. 1", "Etho", "Euclid", "Evolve", "Exchange",
    "F/m", "Formidable", "Gabelli", "Gotham", "Hennessy", "Howard Hughes",
    "Hoya", "Impact", "Innovative", "Intelligent", "iM", "Jackson", "KFA",
    "Knowledge", "LGIM", "Liberty", "Lord Abbett", "Macquarie", "Mairs",
    "Merk", "MFS", "Natixis", "Newday", "Nushares", "Opal", "Optimize",
    "Pathway", "Perth Mint", "Polen", "Praxis", "Precidian", "Reality",
    "Renaissance", "Ridgeline", "Riverfront", "Robinson", "Running Oak",
    "Sage", "Salt", "Sarofim", "Segall", "Siren", "SmartETFs", "Sparkline",
    "Spear", "Stone Ridge", "Strategy Shares", "Swan", "Syntax", "Tema",
    "Terranova", "Texas", "Thornburg", "Tortoise", "TrueShares", "Two Roads",
    "USA", "Valkyrie", "Vest", "VictoryShares", "Volato", "Wahed",
    "Wilshire", "Xponance",
]
ISSUERS_SORTED = sorted(ISSUERS, key=len, reverse=True)

# Sub-brands and legal names folded onto the name people actually filter by.
ISSUER_ALIASES = {
    "State Street": "SPDR",
    "J.P. Morgan": "JPMorgan",
    "FT Vest": "First Trust",
    "Vest": "First Trust",
    "KFA": "KraneShares",
    "Nushares": "Nuveen",
    "IQ": "New York Life",
}


def derive_issuer(name: str) -> str:
    low = name.lower()
    for brand in ISSUERS_SORTED:
        if low.startswith(brand.lower()):
            return ISSUER_ALIASES.get(brand, brand)
    # Unknown sponsor: the first token is right far more often than not, and a
    # visibly odd bucket in the filter list is a better signal than "Other".
    first = name.split()[0] if name.split() else ""
    first = first.strip(",.")
    return ISSUER_ALIASES.get(first, first) or "Unknown"


# ---------------------------------------------------------- single stock

# Single-stock leveraged and option-income wrappers are now a large, distinct
# slice of the universe. They read as plain equity funds by name alone, so
# without this they hide inside "Broad equity" — the one bucket a user filtering
# for diversified funds most needs to be clean.
SINGLE_STOCK_SHAPE = re.compile(
    r"\b\d(?:\.\d)?x\s+(?:long|short)\b|\b(?:long|short)\s+[A-Z]{2,5}\s+daily\b"
    r"|\byieldboost\b|\bweekly ?pay\b|\b[A-Z]{2,5}\s+\d(?:\.\d)?x\b"
    r"|\boption\s+income\s+strategy\b",
    re.I,
)
# Tickers that denote an index, basket or asset — not a company.
INDEX_TICKERS = {
    "QQQ", "SPY", "SPX", "NDX", "DIA", "IWM", "DJIA", "SMH", "TLT", "GLD",
    "SLV", "IBIT", "ETHA", "XLE", "XLF", "XLK", "SOXX", "EEM", "EFA", "VIX",
    "ARKK", "TQQQ", "USO", "UNG", "HYG", "LQD", "AGG", "BND", "MSCI", "FTSE",
    "ESG", "ETF", "ETN", "USA", "US", "REIT", "TIPS", "ICE", "SP", "AI",
    "BTC", "ETH", "XRP", "SOL", "TI",
}
CAPS_TOKEN = re.compile(r"\b([A-Z]{2,5})\b")

# A parenthesised ticker names the single underlying outright:
# "Kurv Yield Premium Strategy Apple (AAPL) ETF".
PAREN_TICKER = re.compile(r"\(([A-Z]{1,5})\)")

# Underlyings spelled out rather than ticker'd. Without these, "T-Rex 2X Long
# Apple Daily Target ETF" reads as diversified equity — which matters more now
# that the default view hides single-stock funds, since anything the detector
# misses lands in "Broad equity" instead.
SINGLE_STOCK_COMPANY = re.compile(
    r"\b(apple|tesla|nvidia|microsoft|amazon|alphabet|google|netflix|"
    r"coinbase|microstrategy|palantir|broadcom|berkshire|salesforce|"
    r"spacex|robinhood|rivian|lucid|intel|qualcomm|oracle|adobe|paypal|"
    r"uber|walmart|disney|boeing|starbucks|pinterest|snowflake|"
    r"eli lilly|novo nordisk|taiwan semiconductor|super micro|arm holdings|"
    r"advanced micro devices|meta platforms)\b",
    re.I,
)


def is_single_stock(name: str) -> bool:
    paren = PAREN_TICKER.search(name)
    if paren and paren.group(1) not in INDEX_TICKERS:
        return True
    if not SINGLE_STOCK_SHAPE.search(name):
        return False
    if any(t not in INDEX_TICKERS for t in CAPS_TOKEN.findall(name)):
        return True
    # Baskets and indices reached here too ("2X Long Magnificent Seven",
    # "2X Long Innovation 100"), so require a named company rather than
    # treating every remaining levered wrapper as single-stock.
    return bool(SINGLE_STOCK_COMPANY.search(name))


# ------------------------------------------------------------- leverage

MULTIPLIER = re.compile(r"(?<![\w.])(-?[1-5](?:\.\d)?)\s*x\b", re.I)

# Unambiguous direction words.
BEAR_EXPLICIT = re.compile(r"\b(inverse|bear)\b|\bultrashort\b", re.I)

# ...and the trap. Across fixed income, "short" describes MATURITY, not
# direction: "Vanguard Ultra-Short Bond ETF" is a cash-like bond fund, and
# reading it as -2x inverse (which this did) is as wrong as a call can be.
# "Short" is treated as a direction by default and disarmed by this pattern.
SHORT_AS_MATURITY = re.compile(
    r"ultra[-\s]?short"
    r"|\bshort[-\s](term|duration|maturity|dated|intermediate)\b"
    # "Short <anything> <fixed-income noun>" — VanEck Short High Yield Muni ETF
    r"|\bshort\b(?=[^,]*\b(bond|muni|municipal|treasur|credit|corporate|"
    r"government|income|duration|maturity|bill|note|tips|aggregate|"
    r"securitized|loan|mortgage|grade|yield)\b)",
    re.I,
)


# The sponsor is the disambiguator. "UltraShort" is spelled identically by
# ProShares (-2x inverse) and by a dozen bond sponsors (ultra-short DURATION):
# ProShares UltraShort S&P500 vs Dimensional Ultrashort Fixed Income. Likewise
# "ProShares Short 20+ Year Treasury" is inverse while "iShares Short Treasury
# Bond" is not. ProShares is the only issuer using these words directionally,
# and issues no short-duration bond funds, so gating on it separates the two
# senses cleanly where spelling cannot.
DIRECTIONAL_SPONSOR = re.compile(r"\bproshares\b", re.I)


def derive_leverage(name: str) -> float:
    """Signed daily multiple. 1.0 = plain long, -1.0 = inverse, 3.0 = 3x long."""
    directional = bool(DIRECTIONAL_SPONSOR.search(name))
    maturity = bool(SHORT_AS_MATURITY.search(name)) and not directional

    mult = 1.0
    m = MULTIPLIER.search(name)
    if m:
        mult = abs(float(m.group(1)))
    elif re.search(r"\bultrapro\b", name, re.I):
        mult = 3.0
    elif re.search(r"\bultrashort\b", name, re.I) and directional:
        mult = 2.0
    # A bare "Ultra" means 2x for ProShares, but "Ultra-Short Treasury" is a
    # duration label and must not pick up a multiplier at all.
    elif re.search(r"\bultra\b", name, re.I) and not maturity:
        mult = 2.0

    bear = bool(BEAR_EXPLICIT.search(name) and directional) or \
        bool(re.search(r"\b(inverse|bear)\b", name, re.I)) or (
            bool(re.search(r"\bshort\b", name, re.I)) and not maturity
        )
    return -mult if bear else mult


# ------------------------------------------------------------ categories

# Ordered: first match wins, so the narrow buckets must precede the broad ones.
CATEGORY_RULES = [
    ("Crypto", r"bitcoin|ether(eum)?\b|\bcrypto|solana|\bxrp\b|dogecoin|"
               r"digital asset|blockchain trust"),
    ("Derivative income", r"covered call|buffer|option income|premium income|"
                          r"defined outcome|defined protection|target income|managed floor|"
                          r"collar|option strategy|yieldmax|income strategy|"
                          r"accelerated return|dual directional"),
    ("Commodity", r"(?<!miner )(physical |shares )?(gold|silver|platinum|"
                  r"palladium)\b(?!.*(miner|mining|producer|equit))|"
                  r"crude oil|natural gas|gasoline|heating oil|"
                  r"\bcommodit|agricultur|\bcorn\b|\bwheat\b|soybean|sugar\b|"
                  r"coffee\b|cocoa\b|livestock|grains|carbon allowance|"
                  r"broad basket"),
    ("Currency", r"\bcurrency|dollar index|japanese yen|\beuro\b|swiss franc|"
                 r"british pound|mexican peso|chinese yuan"),
    ("Fixed income", r"\bbond|treasur|municipal|\bmuni\b|high yield|"
                     r"aggregate|\btips\b|inflation-protected|mortgage|\bmbs\b|"
                     r"\bclo\b|bank loan|senior loan|\bt-bill|\bbill etf|"
                     r"floating rate|convertible|preferred|fixed income|"
                     r"investment grade|\bcredit\b|securitized|"
                     r"ultra short|cash management|enhanced yield|"
                     r"\byears? treasury|duration"),
    ("Real estate", r"\breit\b|real estate|\bproperty\b|mortgage reit"),
    ("Alternatives", r"managed futures|merger arbitrage|market neutral|"
                     r"long/short|long short|tail risk|volatilit|\bvix\b|"
                     r"trend following|alternative|hedge fund|arbitrage|"
                     r"private cred|interest rate hedge"),
    ("Multi-asset", r"allocation|target risk|balanced|multi-asset|"
                    r"target date|risk parity|core portfolio|"
                    r"conservative|moderate growth"),
    ("Sector equity", r"technolog|semiconductor|software|internet|cyber|"
                      r"artificial intelligence|\bai\b|healthcare|health care|"
                      r"biotech|pharmaceutic|medical|financial|\bbank\b|"
                      r"insurance|energy|utilit|industrial|aerospace|defense|"
                      r"transport|materials|mining|miners|consumer|retail|"
                      r"homebuild|construction|infrastructure|clean energy|"
                      r"solar|wind|nuclear|uranium|robotic|space\b|gaming|"
                      r"cannabis|travel|leisure|media|telecom|agribusiness|"
                      r"water\b|timber|shipping|airline|steel|copper miners|"
                      r"lithium|battery|cloud|fintech|e-commerce|"
                      r"communication services|real assets"),
    ("Dividend equity", r"dividend|\bincome\b|high yield equity|"
                        r"aristocrat|payout"),
]
CATEGORY_RULES = [(label, re.compile(rx, re.I)) for label, rx in CATEGORY_RULES]


def derive_category(name: str) -> str:
    # Checked ahead of the keyword rules: a single-stock wrapper's name is
    # dominated by the underlying company's sector, which would otherwise win.
    if is_single_stock(name):
        return "Single stock"
    for label, rx in CATEGORY_RULES:
        if rx.search(name):
            return label
    return "Broad equity"


# --------------------------------------------------------------- region

EM = re.compile(
    r"emerging|frontier|\bchina\b|chinese|\bindia\b|brazil|mexic|taiwan|"
    r"korea|vietnam|indonesia|thailand|malaysia|philippin|turkey|"
    r"south africa|latin america|\basean\b|\bem\b|saudi|\buae\b|qatar|"
    r"poland|chile|colombia|peru|egypt|nigeria|argentin",
    re.I,
)
DEVELOPED_INTL = re.compile(
    r"international|developed|\beafe\b|europe|eurozone|japan|german|france|"
    r"united kingdom|\bu\.?k\.?\b|canada|australia|switzerland|swiss|"
    r"spain|italy|netherland|sweden|norway|denmark|israel|singapore|"
    r"hong kong|\bex[- ]u\.?s\.?|ex[- ]united states|\bex[- ]north america",
    re.I,
)
GLOBAL = re.compile(r"\bglobal\b|\bworld\b|all country|\bacwi\b|international",
                    re.I)


def derive_region(name: str) -> str:
    if EM.search(name):
        return "Emerging markets"
    if GLOBAL.search(name) and not DEVELOPED_INTL.search(name):
        return "Global"
    if DEVELOPED_INTL.search(name):
        return "Developed ex-US"
    if GLOBAL.search(name):
        return "Global"
    return "US"


# ------------------------------------------------------------- parsing

def to_float(raw, *, strip="$%, "):
    """Nasdaq ships numbers as decorated strings; absent values as '' or 'NA'."""
    if raw is None:
        return None
    s = str(raw).strip()
    for ch in strip:
        s = s.replace(ch, "")
    s = s.replace("(", "-").replace(")", "")
    if s in ("", "NA", "N/A", "--", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_rows():
    req = urllib.request.Request(SCREENER_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)

    body = payload.get("data") or {}
    inner = body.get("data") or {}
    rows = inner.get("rows") or inner.get("data") or []
    if not isinstance(rows, list):
        raise RuntimeError("screener returned an unexpected shape for rows")
    return rows, body.get("dataAsOf")


def build(rows):
    out = []
    seen = set()
    for r in rows:
        symbol = (r.get("symbol") or "").strip().upper()
        name = clean_name((r.get("companyName") or "").strip())
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)

        lev = derive_leverage(name)
        out.append({
            "symbol": symbol,
            "name": name,
            "issuer": derive_issuer(name),
            "category": derive_category(name),
            "region": derive_region(name),
            "leverage": lev,
            "price": to_float(r.get("lastSalePrice")),
            "change": to_float(r.get("netChange")),
            # Nasdaq sends full float precision here; two decimals is all the
            # table shows and it cuts the payload meaningfully across 5k rows.
            "changePct": (lambda v: round(v, 2) if v is not None else None)(
                to_float(r.get("percentageChange"))
            ),
            "oneYearPct": to_float(r.get("oneYearPercentage")),
        })

    out.sort(key=lambda e: e["symbol"])
    return out


def tally(records, key):
    counts = {}
    for r in records:
        counts[r[key]] = counts.get(r[key], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def main():
    try:
        rows, as_of = fetch_rows()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1

    records = build(rows)
    if len(records) < MIN_EXPECTED_ROWS:
        print(
            f"refusing to write: got {len(records)} funds, expected "
            f">= {MIN_EXPECTED_ROWS}. Leaving existing data in place.",
            file=sys.stderr,
        )
        return 1

    DATA.mkdir(exist_ok=True)
    (DATA / "etfs.json").write_text(
        json.dumps(records, separators=(",", ":")) + "\n"
    )

    levered = [r for r in records if abs(r["leverage"]) != 1.0]
    inverse = [r for r in records if r["leverage"] < 0]
    # MERGE rather than overwrite. fetch_holdings.py, fetch_expenses.py and
    # fetch_inception.py each add their own keys here, and only the latter two
    # run after this script in the nightly workflow. Rebuilding the file from
    # scratch silently dropped holdingsSource every night, which defeated the
    # monthly holdings job's "already processed this quarter" guard and made it
    # re-download 420 MB every run.
    meta = {}
    meta_path = DATA / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            meta = {}
    meta.update({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "priceAsOf": as_of,
        "source": "Nasdaq ETF screener (public endpoint)",
        "count": len(records),
        "derived": ["issuer", "category", "region", "leverage"],
        "leveredCount": len(levered),
        "inverseCount": len(inverse),
        "issuers": tally(records, "issuer"),
        "categories": tally(records, "category"),
        "regions": tally(records, "region"),
    })
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    print(f"wrote {len(records)} ETFs  (as of {as_of})")
    print(f"  categories: {meta['categories']}")
    print(f"  regions:    {meta['regions']}")
    print(f"  levered:    {len(levered)}  inverse: {len(inverse)}")
    print(f"  top issuers: {list(meta['issuers'].items())[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
