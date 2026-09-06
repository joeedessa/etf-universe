#!/usr/bin/env python3
"""Row-classification and valuation rules for Tokyo PCF files, on real lines.

Every case here is a line that was misread at some point while the fetcher was
being built -- a bond valued at 100x, a cash balance converted twice, a company
called MONEY FORWARD taken for a hedge -- so each one guards a specific mistake.
Run: python scripts/test_tokyo_pcf.py
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch_tokyo_holdings import classify_row, row_value, parse_pcf, build_fund  # noqa: E402

FX = {"JPY": 1.0, "USD": 156.15, "EUR": 181.42, "BRL": 30.57, "CNY": 21.9, "AUD": 112.46}


def line(code="", name="", isin="", exch="", ccy="JPY", qty=0.0, px=0.0, mv=None, fx=None, mult=None):
    return {"code": code, "name": name, "isin": isin, "exch": exch, "ccy": ccy,
            "qty": qty, "px": px, "mv": mv, "fx": fx, "mult": mult}


# (description, line, expected kind, expected yen value or None, has_mv)
CASES = [
    ("plain TSE equity", line("7203", "TOYOTA MOTOR CORP", "JP3633400001", "TSE", "JPY", 100, 2500), "sec", 250000, False),
    ("US equity converted at open rate", line("", "ALBEMARLE CORP", "US0126531013", "XNYS", "USD", 133, 132.16), "sec", 133 * 132.16 * 156.15, False),
    ("company named FUTURE is not a future", line("4722", "FUTURE CORPORATION", "JP3802300008", "TSE", "JPY", 1494200, 2447), "sec", 1494200 * 2447, False),
    ("company named MONEY FORWARD is not an FX forward", line("3994", "MONEY FORWARD INC", "JP3903100009", "TSE", "JPY", 6300, 6710), "sec", 6300 * 6710, False),
    ("TOPIX future on OSE, ICE style", line("", "TOPIX 2609", "", "OSE", "JPY", -110, 4106), "future", None, False),
    ("Nikkei future with blank code", line("", "NK225    FUTURES SEP.2026", "", "OSE", "JPY", 573, 65020), "future", None, False),
    ("E-mini on CME", line("", "E-Mini Russ 2000 Sep26", "", "CME", "USD", 1, 2969.7), "future", None, False),
    ("future with a contract code in the ISIN column", line("", "IFSC NIFTY 50 FUT SEP26", "JGSU6", "NGC", "USD", 6520, 23965), "future", None, False),
    ("currency future on SGX", line("", "INR/USD           SEP26", "XIDU6", "SGX", "USD", 14750, 105.21), "future", None, False),
    ("blank/blank with CASHUSDJPY tag and FX-like price", line("", "", "CASHUSDJPY", "", "USD", 105535806.69, 158.71), "cash", 105535806.69 * 158.71, False),
    ("Amova future carries a multiplier", line("ESU6", "SP EMINI2609", "", "XCME", "USD", 76.9, 7754.75, 29815800.5, 155.68, 50), "future", None, True),
    ("FX forward, Daiwa style", line("FX FORWARD", "USD20261005", "", "", "USD", -90191000, 0.006267), "fxfwd", None, False),
    ("FX forward, MUFG style", line("FX FORWARD", "FX FORWARD USDJPY", "", "", "USD", -67176150, 158.298), "fxfwd", None, False),
    ("FX forward, ICE date-as-name", line("", "20261207", "", "", "USD", 79000000, 157.55), "fxfwd", None, False),
    ("MUFG cash: price is yen per dollar", line("CASH", "CASH USD", "", "", "USD", 2119553.027, 158.71), "cash", 2119553.027 * 158.71, False),
    ("Daiwa cash: price is dollars per yen", line("CASH", "USD", "", "", "USD", 1163664.517, 0.006423432), "cash", 1163664.517 / 0.006423432, False),
    ("Norinchukin cash: no rate given", line("CASH", "USD", "", "", "USD", 105901.83, 0), "cash", 105901.83 * 156.15, False),
    ("Daiwa CNY cash with inverse rate", line("CASH", "CNY", "", "", "CNY", 2840252.89, 0.043153111), "cash", 2840252.89 / 0.043153111, False),
    ("blank/blank with FX-like price is a currency balance", line("", "", "", "", "BRL", 1785353.43, 31.1675), "cash", 1785353.43 * 31.1675, False),
    ("blank/blank with ISIN and price near 100 is a T-bill", line("", "", "US912797VD60", "OTC", "USD", 12050000, 99.8077), "bond", 12050000 * 99.8077 / 100 * 156.15, False),
    ("Treasury with SEDOL code on NONE exchange", line("BMJ0P87", "UNITED STATES 10 YEAR BENCHMARK 4.000% 2035-11-15", "US91282CPJ44", "NONE", "USD", 739129.29, 94.53125), "bond", 739129.29 * 94.53125 / 100 * 156.15, False),
    ("Treasury with ISIN as code, blank exchange", line("US91282CJJ18", "4.5 T-NOTE 331115", "US91282CJJ18", "", "USD", 4085658.712, 98.978516), "bond", 4085658.712 * 98.978516 / 100 * 156.15, False),
    ("Nomura corporate bond on OTC", line("BWFB658", "ABBOTT LABORATORIES", "US002824BE90", "OTC", "USD", 124809.35, 95.37632), "bond", 124809.35 * 95.37632 / 100 * 156.15, False),
    ("Amova bond: market value is x100, own FX", line("Bond", "US TREASURY N/B", "US912810FT08", "", "USD", 1200000, 98.789, 118546800.0, 155.68), "bond", 118546800.0 / 100 * 155.68, True),
    ("Amova equity: market value in local currency, own FX", line("SLB US", "SLB LTD", "AN8068571086", "XNYS", "USD", 9358.73, 57.41, 537284.54, 155.68), "sec", 537284.54 * 155.68, True),
    ("Amova cash line", line("Cash", "USD", "", "", "USD", 0, 0, 1254038.251, 155.68), "cash", 1254038.251 * 155.68, True),
    ("Amova margin line", line("Margin", "JPY", "", "", "JPY", 0, 0, -159521400.0, 1.0), "cash", -159521400.0, True),
    ("Amova FX forward", line("FX Forward", "USD", "", "", "USD", 14528466.2, 1.0, 2260723777.2, 155.6065), "fxfwd", None, True),
    ("disclaimer footer", line("Disclaimer", "", "", "", "", 0, 0), "skip", None, False),
    ("China ETF unit on SHG is a security", line("BNGCJH0", "ICBCCS SSE SCI AND TECH 50 IX ETF", "CNE100004FT6", "SHG", "CNY", 32933450, 1.654), "sec", 32933450 * 1.654 * 21.9, False),
]

SAMPLE = """ETF Code,ETF Name,Fund Cash Component,Shares Outstanding,Fund Date
9999,Test Fund,1000,100,20260904

Code,Name,ISIN,Exchange,Currency,Shares Amount,Stock Price
7203,TOYOTA MOTOR CORP,JP3633400001,TSE,JPY,10,2500
,ALBEMARLE CORP,US0126531013,XNYS,USD,1,100
,TOPIX 2609,,OSE,JPY,-1,4106
FX FORWARD,USD20261005,,,USD,-500,0.0064
CASH,CASH USD,,,USD,10,156.15
"""


def main():
    failures = 0
    for desc, p, kind, value, has_mv in CASES:
        got = classify_row(p, FX)
        if got != kind:
            failures += 1
            print(f"FAIL kind   {desc}: expected {kind}, got {got}")
            continue
        if value is not None:
            v = row_value(p, kind, has_mv, FX)
            if abs(v - value) > max(1.0, abs(value) * 1e-6):
                failures += 1
                print(f"FAIL value  {desc}: expected {value:,.2f}, got {v:,.2f}")

    # Whole-file: denominator = header cash + lines (futures and forwards excluded).
    rec, full = build_fund("9999", SAMPLE, "test", FX, ref_net_assets=None)
    expected_denominator = 1000 + 10 * 2500 + 1 * 100 * 156.15 + 10 * 156.15
    if abs(rec["denominatorYen"] - round(expected_denominator)) > 1:
        failures += 1
        print(f"FAIL denominator: expected {expected_denominator:,.0f}, got {rec['denominatorYen']:,}")
    if rec["positions"] != 2 or rec["derivatives"]["contracts"] != 1 or rec["fxForwards"] != 1:
        failures += 1
        print(f"FAIL counts: {rec['positions']} positions, {rec['derivatives']}, {rec['fxForwards']} forwards")
    top = {t["name"]: t for t in rec["top"]}
    if abs(top["TOYOTA MOTOR CORP"]["pct"] - 25000 / expected_denominator * 100) > 0.01:
        failures += 1
        print(f"FAIL weight: Toyota {top['TOYOTA MOTOR CORP']['pct']}")
    if top["ALBEMARLE CORP"]["country"] != "US" or top["TOYOTA MOTOR CORP"]["country"] != "JP":
        failures += 1
        print(f"FAIL country: {top}")
    if abs(sum(rec["byAsset"].values()) - 100) > 0.01:
        failures += 1
        print(f"FAIL byAsset does not sum to 100: {rec['byAsset']}")
    if rec["asOf"] != "2026-09-04":
        failures += 1
        print(f"FAIL asOf: {rec['asOf']}")

    total = len(CASES) + 6
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
