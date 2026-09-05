#!/usr/bin/env python3
"""Cases for the holdings-based classifier. Each is a real fund whose filing
and name disagree, or a boundary the rule has to respect."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from reclassify import classify, split, fund_of_funds  # noqa: E402

# (label, name_category, name, leverage, byAsset, prev, expected_cat, expected_src)
CASES = [
    # Pure equity with a cash sliver stays equity — the tolerance case.
    ("IVV", "Broad equity", "iShares Core S&P 500 ETF", 1,
     {"Equity (common)": 99.78, "Other": 0.29}, None, "Broad equity", "holdings"),
    # Bond fund misnamed into Dividend equity by the word "income".
    ("CGCP", "Dividend equity", "Capital Group Core Plus Income ETF", 1,
     {"Debt": 98.0, "Short-term / cash": 1.5}, None, "Fixed income", "holdings"),
    # Bond fund in Broad equity with no bond word in its name.
    ("BBIB", "Broad equity", "JPMorgan BetaBuilders U", 1,
     {"Debt": 98.47, "Short-term / cash": 0.76}, None, "Fixed income", "holdings"),
    # Buffer fund: 99% derivatives -> Derivative income, whatever the name said.
    ("APXM", "Broad equity", "FT Vest U.S. Equity Max", 1,
     {"Derivative": 99.69, "Other": 0.37}, None, "Derivative income", "holdings"),
    # Balanced fund -> Multi-asset.
    ("RPAR", "Multi-asset", "RPAR Risk Parity ETF", 1,
     {"Equity (common)": 49.7, "Debt": 43.9, "Commodity": 6.0}, None, "Multi-asset", "holdings"),
    # 60/40 is a combo, not equity.
    ("mix", "Broad equity", "Some Balanced Fund ETF", 1,
     {"Equity (common)": 60, "Debt": 40}, None, "Multi-asset", "holdings"),
    # A levered fund files "49% equity, 51% swaps" — must NOT become Multi-asset.
    ("EURL", "Broad equity", "Direxion Daily FTSE Europe Bull 3X ETF", 3,
     {"Equity (common)": 49.1, "Derivative (equity)": 50.9}, None, "Broad equity", "name"),
    # Strategy labels are kept from the name; the asset field cannot see them.
    ("NTRL", "Alternatives", "First Trust Equity Market Neutral ETF", 1,
     {"Equity (common)": 49.6, "Short-term / cash": 50.4}, None, "Alternatives", "name"),
    ("JEPI", "Derivative income", "JPMorgan Equity Premium Income ETF", 1,
     {"Equity (common)": 84.52, "Structured note": 13.85}, None, "Derivative income", "name"),
    # Equity fund that "high yield" had dragged into Fixed income: the filing
    # says equity, and the sub-category comes from the name's equity rules.
    ("PEY", "Fixed income", "Invesco High Yield Equity Dividend Achievers ETF", 1,
     {"Equity (common)": 99.5, "Short-term / cash": 0.5}, None, "Dividend equity", "holdings"),
    # Lending collateral: 100% equity plus 15.6% "cash" totals 115.6%. The cash
    # is collateral, not an allocation, so this is an equity fund, not a combo
    # and not an unusable rollup.
    ("PEY", "Fixed income", "Invesco High Yield Equity Dividend Achievers ETF", 1,
     {"Equity (common)": 99.98, "Short-term / cash": 15.61}, None, "Dividend equity", "holdings"),
    # A real 60/30/10 balanced fund: non-cash is 90, still in band, so cash is
    # excluded and it reads 67/33 -> Multi-asset either way.
    ("bal", "Broad equity", "Some Balanced Fund ETF", 1,
     {"Equity (common)": 60, "Debt": 30, "Short-term / cash": 10}, None, "Multi-asset", "holdings"),
    # No filing at all -> name label, source "name".
    ("SPY", "Broad equity", "SPDR S&P 500 ETF Trust", 1, None, None, "Broad equity", "name"),
    # TBA-futures fund: 106% MBS+debt plus 94% cash collateral. The non-cash
    # side is in band, so the filing decides and cash is ignored as collateral.
    ("MTBA", "Fixed income", "Simplify MBS ETF", 1,
     {"Mortgage-backed": 98.81, "Short-term / cash": 94.38, "Debt": 7.18}, None, "Fixed income", "holdings"),
    # Genuinely notional (sums to -495%) still cannot classify -> name label.
    ("TXXD", "Crypto", "Some 2x Long Coin ETF", 2,
     {"Derivative": -520.0, "Short-term / cash": 25.0}, None, "Crypto", "name"),
    # Cash counts on the debt side: a T-bill / money-market style fund is Fixed income.
    ("cash", "Broad equity", "Some Cash Management ETF", 1,
     {"Short-term / cash": 99.0, "Other": 1.0}, None, "Fixed income", "holdings"),
    # 92/8: the 8% is under the tolerance, so this is equity, not a combo.
    ("tol1", "Broad equity", "Some Equity ETF", 1,
     {"Equity (common)": 92, "Debt": 8}, None, "Broad equity", "holdings"),
    # 88/12: both material -> a combo.
    ("tol2", "Broad equity", "Some Equity ETF", 1,
     {"Equity (common)": 88, "Debt": 12}, None, "Multi-asset", "holdings"),
    # Hysteresis: 92/8 STAYS a combo if it already was one (8 >= leave line 7)...
    ("hys1", "Broad equity", "Some Equity ETF", 1,
     {"Equity (common)": 92, "Debt": 8}, "Multi-asset", "Multi-asset", "holdings"),
    # ...but at 95/5 it leaves.
    ("hys2", "Broad equity", "Some Equity ETF", 1,
     {"Equity (common)": 95, "Debt": 5}, "Multi-asset", "Broad equity", "holdings"),
    # Preferred stock is fixed income by convention.
    ("PGX", "Fixed income", "Invesco Preferred ETF", 1,
     {"Equity (preferred)": 99.0, "Short-term / cash": 1.0}, None, "Fixed income", "holdings"),
    # Non-US REITs file as OTHER: an unclassifiable residual is not a combo.
    ("IFGL", "Real estate", "iShares International Developed Real Estate ETF", 1,
     {"Equity (common)": 66.94, "OTHER": 32.22, "Short-term / cash": 2.23}, None, "Real estate", "holdings"),
]

# (label, name_category, name, leverage, byAsset, byIssuer, expected)
FOF_CASES = [
    # A bond fund of bond ETFs files as 100% "equity" — the name label must win.
    ("BNDW", "Fixed income", "Vanguard Total World Bond ETF", 1,
     {"Equity (common)": 100.0}, {"RF": 100.0}, ("Fixed income", "name")),
    # An allocation fund of ETFs likewise.
    ("AAAA", "Multi-asset", "Amplius Aggressive Asset Allocation ETF", 1,
     {"Equity (common)": 99.4}, {"RF": 87.7, "Corporate": 12.3}, ("Multi-asset", "name")),
    # A 38% bond sleeve held as one bond ETF files as equity: name label kept.
    ("CGBL", "Multi-asset", "Capital Group Core Balanced ETF", 1,
     {"Equity (common)": 100.0}, {"Corporate": 62.02, "RF": 38.13}, ("Multi-asset", "name")),
    # BondBloxx files its own ETFs as "PF" (private fund) -- also fund-of-funds.
    ("HYSA", "Fixed income", "BondBloxx USD High Yield Bond Sector Rotation ETF", 1,
     {"Equity (common)": 100.0}, {"PF": 99.93}, ("Fixed income", "name")),
    # Below the fund-of-funds line the filing still decides.
    ("part", "Broad equity", "Some Core Fund ETF", 1,
     {"Debt": 70.0, "Equity (common)": 30.0}, {"RF": 15.0, "Corporate": 85.0}, ("Multi-asset", "holdings")),
]


def run():
    fails = []
    for label, ncat, name, lev, ba, prev, want_cat, want_src in CASES:
        got = classify(ncat, name, lev, split(ba), prev)
        if got != (want_cat, want_src):
            fails.append(f"{label}: got {got}, want {(want_cat, want_src)}")
    for label, ncat, name, lev, ba, bi, want in FOF_CASES:
        got = classify(ncat, name, lev, split(ba), None, fund_of_funds(bi))
        if got != want:
            fails.append(f"{label}: got {got}, want {want}")
    total = len(CASES) + len(FOF_CASES)
    if fails:
        print(f"FAILED {len(fails)}/{total}")
        for f in fails: print("  " + f)
        return 1
    print(f"OK — {total} classification cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(run())
