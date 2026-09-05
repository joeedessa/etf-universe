#!/usr/bin/env python3
"""Regression tests for the name-derived fields.

Every case below is one that actually broke during development. The taxonomy is
heuristic, so the guarantee is not "correct for all 5,000 funds" but "still
correct for the cases we know are tricky" — mostly names where a registrant
shell, a sponsor whose own name ends in "Trust", or a source-truncated string
defeats the obvious rule.

Run: python scripts/test_derive.py
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fetch_etfs import (  # noqa: E402
    clean_name, derive_issuer, derive_category, derive_region, derive_leverage,
)

# (raw name, expected clean name or None to assert unchanged)
NAME_CASES = [
    # Registrant shells that must be stripped.
    ("Collaborative Investment Series Trust Anydrus Advantage ETF",
     "Anydrus Advantage ETF"),
    ("Baron ETF Trust Baron Emerging Markets Select ETF",
     "Baron Emerging Markets Select ETF"),
    ("J.P. Morgan Exchange-Traded Fund Trust JPMorgan BetaBuilders Europe ETF",
     "JPMorgan BetaBuilders Europe ETF"),
    ("Tidal Trust II YieldMax MSTR Option Income Strategy ETF",
     "YieldMax MSTR Option Income Strategy ETF"),
    ("The 2023 ETF Series Trust II GMO Beyond China ETF",
     "GMO Beyond China ETF"),
    # Two shell markers: the LAST one inside the window must win.
    ("First Trust Exchange-Traded Fund VIII FT Vest U.S. Equity Equal Weight ETF",
     "FT Vest U.S. Equity Equal Weight ETF"),
    # Stranded series numeral must not survive the strip.
    ("Investment Managers Series Trust II Tradr 2X Long BE Daily ETF",
     "Tradr 2X Long BE Daily ETF"),

    # Real names that must survive untouched. A sponsor whose name ends in
    # "Trust" is the dangerous case: an unanchored rule rewrote 130 First Trust
    # funds to a sponsor called "Nasdaq".
    ("First Trust Nasdaq Cybersecurity ETF", None),
    ("Invesco QQQ Trust Series 1", None),
    ("State Street SPDR S&P 500 ETF Trust Unit", None),
    ("iShares Bitcoin Trust ETF", None),
    ("Grayscale Bitcoin Trust ETF", None),
    ("SPDR Gold Trust", None),
    ("iShares Gold Trust Micro", None),
    ("Bitwise Bitcoin ETF Trust", None),
    ("ETF Opportunities Trust T-Rex 2X Long Apple Daily Target ETF",
     "T-Rex 2X Long Apple Daily Target ETF"),
    ("Vanguard S&P 500 ETF", None),

    # Nasdaq concatenates two fund names. Identical halves: keep the complete
    # copy, since the second is clipped at 61 chars.
    ("Archer Growth ETF Archer Growth ETF", "Archer Growth ETF"),
    ("Gabelli Love Our Planet & People ETF Gabelli Love Our Planet &",
     "Gabelli Love Our Planet & People ETF"),
    # Diverging halves: the SECOND is the fund this ticker actually is.
    ("Tradr 2X Long CIEN Daily ETF Tradr 2X Long SK hynix Daily ETF",
     "Tradr 2X Long SK hynix Daily ETF"),
    ("Tradr 2X Long CIEN Daily ETF Tradr 2X Short SK hynix Daily ETF",
     "Tradr 2X Short SK hynix Daily ETF"),
    # Trailing words that are part of the fund's identity must survive; these
    # are the names a blanket "split on ETF" rule would destroy.
    ("Innovator U.S. Equity Buffer ETF - April", None),
    ("PGIM S&P 500 Buffer 12 ETF - August", None),
    ("Goldman Sachs Physical Gold ETF Shares", None),
    ("Bitwise Bitcoin ETF Common Shares of Beneficial Interest", None),
    ("AIM ETF Products Trust AllianzIM U.S. Equity Buffer15 ETF", None),
]

ISSUER_CASES = [
    ("State Street SPDR S&P 500 ETF Trust Unit", "SPDR"),
    ("First Trust Nasdaq Cybersecurity ETF", "First Trust"),
    ("FT Vest U.S. Equity Equal Weight ETF", "First Trust"),   # sub-brand alias
    ("J.P. Morgan Exchange-Traded Fund Trust", "JPMorgan"),
    ("Global X Lithium & Battery Tech ETF", "Global X"),       # beats bare "Global"
    ("Vanguard Total Bond Market ETF", "Vanguard"),
]

CATEGORY_CASES = [
    ("iShares Bitcoin Trust ETF", "Crypto"),
    ("Vanguard Total Bond Market ETF", "Fixed income"),
    ("JPMorgan Equity Premium Income ETF", "Derivative income"),
    ("Innovator Equity Defined Protection ETF - 2 Yr to April 2028",
     "Derivative income"),
    ("SPDR Gold Shares", "Commodity"),
    ("Schwab US Dividend Equity ETF", "Dividend equity"),
    # "High Yield Equity" is an equity screen, not a bond fund.
    ("Invesco High Yield Equity Dividend Achievers ETF", "Dividend equity"),
    ("iShares iBoxx $ High Yield Corporate Bond ETF", "Fixed income"),
    ("Vanguard S&P 500 ETF", "Broad equity"),
    ("First Trust Nasdaq Cybersecurity ETF", "Sector equity"),
    # Single-stock wrappers must not read as plain equity.
    ("GraniteShares 2x Long NVDA Daily ETF", "Single stock"),
    ("Direxion Daily TSLA Bull 2X ETF", "Single stock"),
    ("Roundhill AAPL WeeklyPay ETF", "Single stock"),
    ("YieldMax MSTR Option Income Strategy ETF", "Single stock"),
    # Underlying spelled out as a company name, or given in parentheses.
    ("T-Rex 2X Long Apple Daily Target ETF", "Single stock"),
    ("T-Rex 2X Long NVIDIA Daily Target ETF", "Single stock"),
    ("Kurv Yield Premium Strategy Apple (AAPL) ETF", "Single stock"),
    ("Tradr 2X Long SpaceX Daily", "Single stock"),
    # ...but a levered INDEX or BASKET fund is not a single-stock fund.
    ("ProShares UltraPro QQQ", "Broad equity"),
    ("Tradr 2X Long SPY Quarterly ETF", "Broad equity"),
    ("Roundhill Daily 2X Long Magnificent Seven ETF", "Broad equity"),
    ("Tradr 2X Long Innovation 100 Monthly ETF", "Broad equity"),
    ("Defiance Daily Target 2X Long Copper Miners ETF", "Sector equity"),
    # A parenthesised non-company token must not trip the rule.
    ("Grayscale Bitcoin Trust (BTC) Common Units", "Crypto"),
    # Miners are equities, not the metal.
    ("VanEck Gold Miners ETF", "Sector equity"),
]

REGION_CASES = [
    ("Vanguard S&P 500 ETF", "US"),
    ("iShares MSCI Emerging Markets ETF", "Emerging markets"),
    ("iShares MSCI EAFE Small-Cap ETF", "Developed ex-US"),
    ("Vanguard Total International Stock ETF", "Developed ex-US"),
    ("iShares MSCI ACWI ETF", "Global"),
]

LEVERAGE_CASES = [
    ("Vanguard S&P 500 ETF", 1.0),
    ("ProShares UltraPro QQQ", 3.0),
    ("ProShares UltraPro Short QQQ", -3.0),
    ("ProShares UltraShort S&P500", -2.0),
    ("Direxion Daily TSLA Bull 2X ETF", 2.0),
    ("Direxion Daily AAPL Bear 1X ETF", -1.0),
    ("ProShares Short S&P500", -1.0),
    ("ProShares Ultra QQQ", 2.0),

    # "Short" as a MATURITY word must never flip the sign. Every case here was
    # live on the site: VUSB and VGUS were being published as -2x inverse funds
    # when they are cash-like ultra-short bond funds.
    ("SPDR Portfolio Short Term Treasury ETF", 1.0),
    ("Vanguard Short-Term Bond ETF", 1.0),
    ("Vanguard Ultra-Short Bond ETF", 1.0),
    ("Vanguard Ultra-Short Treasury ETF", 1.0),
    ("VanEck Short Muni ETF", 1.0),
    ("VanEck Short High Yield Muni ETF", 1.0),
    ("JPMorgan Ultra-Short Income ETF", 1.0),
    ("PIMCO Enhanced Short Maturity Active ETF", 1.0),
    ("iShares Short Treasury Bond ETF", 1.0),
    # One-word "Ultrashort" from a bond sponsor is duration, not -2x.
    ("Dimensional Ultrashort Fixed Income ETF", 1.0),
    ("DoubleLine Ultrashort Income ETF", 1.0),
    ("Angel Oak UltraShort Income ETF", 1.0),
    ("F/m Ultrashort Tax-Free Municipal ETF", 1.0),
    # ...but the identical word from ProShares is.
    ("ProShares UltraShort S&P500", -2.0),
    ("ProShares UltraShort Lehman 20 Year Treasury", -2.0),
    ("ProShares Short 20+ Year Treasury", -1.0),
]


def run():
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")

    for raw, want in NAME_CASES:
        check(f"clean_name({raw[:52]!r})", clean_name(raw),
              raw if want is None else want)

    for name, want in ISSUER_CASES:
        check(f"issuer({name[:52]!r})", derive_issuer(clean_name(name)), want)

    for name, want in CATEGORY_CASES:
        check(f"category({name[:52]!r})", derive_category(clean_name(name)), want)

    for name, want in REGION_CASES:
        check(f"region({name[:52]!r})", derive_region(name), want)

    for name, want in LEVERAGE_CASES:
        check(f"leverage({name[:52]!r})", derive_leverage(name), want)

    total = (len(NAME_CASES) + len(ISSUER_CASES) + len(CATEGORY_CASES)
             + len(REGION_CASES) + len(LEVERAGE_CASES))
    if failures:
        print(f"FAILED {len(failures)}/{total}\n")
        for f in failures:
            print("  " + f)
        return 1
    print(f"OK — {total} derivation cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(run())
