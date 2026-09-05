#!/usr/bin/env python3
"""Let a fund's own SEC filing override its name-derived category.

fetch_etfs.py classifies from the fund NAME, because the screener carries no
category. That is a guess, and the review found it wrong in ways that matter:
216 bond funds labelled as equity because their names say "Core Plus Income"
rather than "Bond", buffer funds that are 99% derivatives labelled Broad
equity, and so on. Fixed income is hidden by default, so those leaked into the
view meant for diversified equity.

N-PORT gives the fund's actual asset mix, filed by the fund under SEC rules.
Where a filing exists, it wins. The rule, in order:

  1. STRATEGY labels are kept from the name. Alternatives, Derivative income,
     Single stock, Crypto, Commodity, Currency describe what a fund DOES, and
     the asset-category field cannot see that: a 3x Europe fund files as "49%
     equity, 51% swaps", which is not a balanced fund. Leverage != 1 likewise.
  2. Holds other funds (>= FOF_PCT)  -> name label kept; see below
  3. Derivatives >= DERIV_PCT        -> Derivative income (buffer / outcome)
  4. Equity AND debt both >= MIX_PCT -> Multi-asset, with the split published
  5. otherwise                       -> whichever is larger: equity family
                                        (Broad / Sector / Dividend / Real
                                        estate, by name) or Fixed income

MIX_PCT is 10: "any combination of equity and something else is multi-asset",
with a 10% tolerance because every fund holds a little cash or has a rounding
residual. That is the 90% line for a two-class fund, but it does not let an
unclassifiable residual push a fund into Multi-asset -- non-US REITs file as
"OTHER" and would otherwise make IFGL a balanced fund. Preferred stock counts
as debt, by the convention every preferred fund follows. Cash counts on the
debt side unless it is lending collateral on top of a full portfolio.

Labels have HYSTERESIS: a combo stays a combo until its minority side falls
below MIX_LEAVE_PCT, so an 89/11 fund drifting to 91/9 does not flap between
Multi-asset and Broad equity from one quarterly filing to the next.

Every fund carries categorySource: "holdings" when the filing decided, "name"
when there was no usable filing. The split itself is published as equityPct /
debtPct / derivPct / otherPct so the label is never the only thing to go on.

Reads  data/etfs.json, data/holdings.json, data/classification.json
Writes data/etfs.json (in place), data/classification.json, data/meta.json
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch_etfs import CATEGORY_RULES  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "classification.json"

# A fund is a COMBO when both equity and debt are material. 10% is the
# tolerance on the minority side -- equivalent to the 90% "pure" line for a
# two-class fund, but it does not let an unclassifiable residual (non-US REITs
# file as "OTHER") push a real-estate fund into Multi-asset.
MIX_PCT = 10.0
MIX_LEAVE_PCT = 7.0      # hysteresis: once a combo, stays one down to here
DERIV_PCT = 30.0
# A rollup outside this band is notional or futures-based reporting, not a
# composition, and cannot classify anything.
SANE_SUM = (90.0, 110.0)

STRATEGY = {"Alternatives", "Derivative income", "Single stock", "Crypto",
            "Commodity", "Currency"}

# A fund that holds OTHER FUNDS files each of them as "Equity (common)" no
# matter what they hold: Vanguard Total World Bond (BNDW) reads as 100% equity
# because it owns two bond ETFs, and Capital Group Core Balanced (CGBL) reads
# as 100% equity because its 38% bond sleeve is one bond ETF. Any material
# fund-holding makes the split unreliable, so the name label is kept. "PF"
# (private fund) is included: BondBloxx files its own ETFs under it.
FOF_PCT = 20.0
RF_KEYS = ("RF", "Registered fund", "RGS", "PF", "Private fund")
EQUITY_FAMILY = {"Broad equity", "Sector equity", "Dividend equity", "Real estate"}

# Preferred stock is legally equity and economically a bond; every
# convention files preferred funds (PGX, PSK, PFFD) under fixed income.
EQUITY = ("Equity (common)",)
DEBT = ("Debt", "Mortgage-backed", "Asset-backed CP", "Asset-backed (other)",
        "Loan", "US Treasury", "ABS-CBDO", "ABS-APCP", "ABS-MBS",
        "Equity (preferred)")
CASH = ("Short-term / cash", "Repo")
DERIV = ("Derivative", "Derivative (equity)", "Derivative (rates)",
         "Derivative (credit)", "Derivative (FX)", "Derivative (commodity)",
         "Structured note", "DO")

EQUITY_RULES = [(label, rx) for label, rx in CATEGORY_RULES if label in EQUITY_FAMILY]


def equity_subcategory(name):
    """The equity-family label the name rules would give, ignoring every
    non-equity rule — so a fund the filing says is equity cannot be pulled
    into Fixed income by a stray "high yield" in its name."""
    for label, rx in EQUITY_RULES:
        if rx.search(name):
            return label
    return "Broad equity"


def split(by_asset):
    """Normalised equity / debt / derivative / other shares, or None.

    Two passes. Securities-lending collateral is filed as cash on top of a
    fully invested portfolio, so an equity fund can total 115% -- 99.98% equity
    plus 15.6% "cash" (PEY). That cash is not an allocation. If the portfolio
    EXCLUDING cash already accounts for ~100%, the split is taken over that;
    cash is treated as collateral and ignored. Only when it does not -- a
    cash-management fund, a 60/30/10 balanced fund -- does cash enter the
    denominator, on the debt side.
    """
    if not by_asset:
        return None
    def total_of(keys_out):
        return sum(v for k, v in by_asset.items() if k not in keys_out)
    non_cash = total_of(CASH)
    if SANE_SUM[0] <= non_cash <= SANE_SUM[1]:
        total, cash_in = non_cash, False
    else:
        total = total_of(())
        if not (SANE_SUM[0] <= total <= SANE_SUM[1]):
            return None
        cash_in = True
    def share(keys):
        return sum(v for k, v in by_asset.items() if k in keys) / total * 100
    eq, deriv = share(EQUITY), share(DERIV)
    debt = share(DEBT) + (share(CASH) if cash_in else 0.0)
    other = max(0.0, 100 - eq - debt - deriv)
    return {"equityPct": round(eq, 1), "debtPct": round(debt, 1),
            "derivPct": round(deriv, 1), "otherPct": round(other, 1)}


def fund_of_funds(by_issuer):
    return sum(v for k, v in (by_issuer or {}).items() if k in RF_KEYS) >= FOF_PCT


def classify(name_cat, name, leverage, s, prev_cat=None, fof=False):
    """Return (category, source). `s` is a split() result or None."""
    if s is None or leverage != 1 or name_cat in STRATEGY:
        return name_cat, "name"
    if fof:
        return name_cat, "name"

    eq, debt, deriv = s["equityPct"], s["debtPct"], s["derivPct"]
    if deriv >= DERIV_PCT:
        return "Derivative income", "holdings"

    # A real combo: both sides material. Hysteresis keeps a combo a combo
    # until its minority side drops below MIX_LEAVE_PCT.
    line = MIX_LEAVE_PCT if prev_cat == "Multi-asset" else MIX_PCT
    if eq >= line and debt >= line:
        return "Multi-asset", "holdings"
    if eq >= debt:
        return equity_subcategory(name), "holdings"
    return "Fixed income", "holdings"


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
    holdings = load(DATA / "holdings.json", {})
    cache = load(CACHE, {})

    changed, by_src, moves = 0, {"name": 0, "holdings": 0}, {}
    for e in etfs:
        sym = e["symbol"]
        # Re-runs must start from the name label, not from a previous override.
        name_cat = e.get("nameCategory") or e["category"]
        hold = holdings.get(sym) or {}
        s = split(hold.get("byAsset"))
        prev = (cache.get(sym) or {}).get("category")
        fof = fund_of_funds(hold.get("byIssuer"))
        cat, src = classify(name_cat, e["name"], e.get("leverage", 1), s, prev, fof)
        e["fundOfFunds"] = bool(fof)

        e["nameCategory"] = name_cat
        e["category"] = cat
        e["categorySource"] = src
        for k in ("equityPct", "debtPct", "derivPct", "otherPct"):
            e[k] = s[k] if s else None

        by_src[src] += 1
        if cat != name_cat:
            changed += 1
            moves[(name_cat, cat)] = moves.get((name_cat, cat), 0) + 1
        cache[sym] = {"category": cat, "source": src,
                      "asOf": (holdings.get(sym) or {}).get("asOf")}

    (DATA / "etfs.json").write_text(json.dumps(etfs, separators=(",", ":")) + "\n")
    CACHE.write_text(json.dumps(dict(sorted(cache.items())), indent=0) + "\n")

    meta = load(DATA / "meta.json", {})
    tally = {}
    for e in etfs:
        tally[e["category"]] = tally.get(e["category"], 0) + 1
    meta["categories"] = dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))
    meta["categorySource"] = by_src
    meta["reclassified"] = changed
    meta["classifyThresholds"] = {"mix": MIX_PCT, "mixLeave": MIX_LEAVE_PCT,
                                  "deriv": DERIV_PCT, "fundOfFunds": FOF_PCT}
    (DATA / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    print(f"classified {len(etfs)}: {by_src['holdings']} from filings, "
          f"{by_src['name']} from name; {changed} labels changed")
    for (a, b), n in sorted(moves.items(), key=lambda kv: -kv[1])[:14]:
        print(f"  {n:4}  {a:18} -> {b}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
