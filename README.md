# etf-universe

Every US-listed ETF in one searchable table — 5,253 funds, refreshed each weekday
after the close.

Live: https://joeedessa.github.io/etf-universe/

## What this is

A single static page over a nightly snapshot of the public Nasdaq ETF screener.
No build step, no dependencies, no API keys. GitHub Actions refreshes the data
and commits it; GitHub Pages serves the page.

Filter by sponsor, category, region and leverage; search by ticker or name; sort
by today's move, one-year return or price. All four filters are multi-select
dropdowns, and the sponsor one is searchable since there are 498 of them.
Leverage selections are a union — picking Leveraged and Inverse returns 760
funds, not 900, because -3x funds are both.

### Favourites

Star any fund to add it to a watchlist, and use the Favourites button to show
only those. Favourites live in `localStorage` on your own device — nothing is
uploaded, and the site has no backend to upload it to. They survive reloads and
are untouched by Reset, which clears filters only.

Because a starred fund can sit in a category the default view hides, turning on
Favourites reports how many are being held back and offers to reveal them,
rather than quietly showing a shorter watchlist than you saved.

### The default view

On load the page hides **crypto, currency, single stock, fixed income and
derivative income** — 2,144 of the 5,253 funds — leaving 3,109 diversified
funds. Single-stock and derivative-income wrappers are the reason: there are
now ~950 of them, many being a dozen tickers on one underlying, and they crowd
out everything else in an A–Z list.

Every exclusion is one click away, and the page says so rather than hiding it.
If a search matches funds inside a hidden category, the hint reports the count
and offers to reveal them — searching `T-Rex` returning a bare "no funds match"
would be the page looking broken while working correctly.

## What the data is, and is not

The screener publishes the full list of listed ETFs with a quote for each. That
is all it publishes. Everything else in the table is **derived from the fund's
name**:

| Field | Source |
|---|---|
| Ticker, fund name, price, today's %, 1-year % | Nasdaq screener |
| Listed (first trading day) | Earliest bar in the fund's price history |
| Top 10 holdings | SEC Form N-PORT (quarterly, lagging) |
| Expense ratio | SEC prospectus risk/return summary XBRL |
| Category | Fund's N-PORT asset mix where filed; else the name |
| Sponsor, region, leverage | Inferred from the fund name |

### Category: the fund's own filing wins

Category used to be inferred from the fund's name alone. A review found that
wrong in ways that matter: 216 bond funds labelled as equity because their
names say "Core Plus Income" rather than "Bond", buffer funds that are 99%
derivatives labelled Broad equity. Fixed income is hidden by default, so those
leaked into the view meant for diversified equity funds.

`scripts/reclassify.py` now lets a fund's N-PORT asset mix override the name
wherever a usable filing exists. The rule, in order:

1. **Strategy labels are kept from the name** — Alternatives, Derivative
   income, Single stock, Crypto, Commodity, Currency, and anything levered. The
   asset field cannot see a strategy: a 3x Europe fund files as "49% equity,
   51% swaps", which is not a balanced fund.
2. **Funds that hold other funds keep the name label.** A fund holding ETFs
   files each one as "Equity (common)" regardless of what that ETF holds —
   `BNDW` Vanguard Total World Bond reads as 100% equity because it owns two
   bond ETFs, and `CGBL` Core Balanced hides a 38% bond sleeve the same way.
   Any holding of ≥20% in other funds makes the split unreliable. 612 funds.
3. **Derivatives ≥30% → Derivative income.** Buffer and defined-outcome funds.
4. **Equity and debt both ≥10% → Multi-asset**, with the split shown on the
   row. "Any combination of equity and something else is multi-asset", with a
   10% tolerance because every fund holds a little cash or has a rounding
   residual. For a two-class fund that is the 90% line; unlike a plain 90%
   rule it does not let an unclassifiable residual (non-US REITs file as
   `OTHER`) push a real-estate fund into Multi-asset.
5. **Otherwise, whichever is larger** — the equity family (Broad / Sector /
   Dividend / Real estate, by name) or Fixed income.

Preferred stock counts as debt, by the convention every preferred fund
follows. Securities-lending collateral, filed as cash on top of a fully
invested portfolio, is ignored rather than treated as an allocation. Labels
have hysteresis so a fund at 89/11 drifting to 91/9 does not flap between
Multi-asset and Broad equity from one quarterly filing to the next.

On the current data: **2,771 funds classified from their filing, 2,482 from
their name** (no usable filing, or fund-of-funds), and **258 labels changed** —
91 Broad equity → Fixed income, 71 Broad equity → Derivative income, 29 Dividend
equity → Fixed income, 28 Broad equity → Multi-asset. Every fund carries
`categorySource` and its `equityPct` / `debtPct` / `derivPct`, so the label is
never the only thing to go on: hover a category chip for its source and mix,
and an outlined chip is one the filing decided.

`scripts/test_classify.py` holds 27 cases, each a real fund where name and
filing disagreed or a boundary the rule has to respect.

The **Listed** column is the fund's first trading day, not its prospectus
inception — the latter is not published in bulk anywhere free. The two are
usually within days of each other; spot checks match known listing dates to the
day (SPY 1993-01-29, QQQ 1999-03-10, GLD 2004-11-18, IBIT 2024-01-11). A fund
that changed sponsor or exchange can show the later date rather than its
original launch, so treat old funds with more suspicion than new ones.

Because a first trading day never changes, `data/inception.json` is a permanent
cache. The expensive pass is paid once; each scheduled run then looks up only
the funds listed since the last one.

### Portfolio builder

`portfolio.html` models a blend of any funds in the universe. Add funds by
ticker or name, then weight them:

- **Equal weight** — the remainder goes to the last row so the total is exactly
  100%, not 99.99%
- **Weight by fund size** — proportional to net assets, for the funds that
  publish them
- **Normalise to 100%** — rescale whatever you have typed
- or type weights by hand

**Save it under a name** and reopen it later; the library holds as many as you
like, in `localStorage` on your device. Saving is explicit, so experimenting
with the working sheet never overwrites something you deliberately kept.

It reports blended fee, annual cost in dollars against a portfolio value you
set, weighted one-year and one-day moves, effective leverage, and concentration.

**Concentration is computed over every position, not the top ten.** N-PORT
carries `INVESTMENT_COUNTRY`, `ASSET_CAT` and `ISSUER_TYPE` per holding, so the
fetcher rolls each fund up across its whole portfolio — 507 positions for `IVV`,
8,878 for `VXUS`, 17,368 for `BND` — and the page combines those by weight. An
equal-weight VOO/VXUS/BND blend comes out 64.4% United States, 5.1% Japan, 3.2%
United Kingdom; 66% equity, 26% debt, 7% mortgage-backed; 77% corporate, 16% US
Treasury. That is real look-through, not a top-10 approximation.

The top-10 list is still there underneath, for "what does it actually own".

**Full holdings are pulled on demand.** The *Pull full holdings* button fetches
`data/positions/<TICKER>.json` for the funds currently in the sheet — nothing is
loaded with the page, and a saved portfolio stores no holdings, so reopening one
and pulling again is a deliberate act. For a VOO/VXUS/BND blend that takes
coverage from 26% (top-10 only) to **75%**, across 1,002 distinct holdings.

Each file holds the fund's **largest 500** positions and states what it left
out. The cap matters for bond funds: `BND` reports 17,368 lines, almost all
under 0.01% of the fund. An earlier version kept the first 500 rows *encountered*
rather than the largest, which for a bond fund is an arbitrary slice — file
order has nothing to do with position size.

Three things it deliberately does not do: renormalise weights behind your back
(a total that is not 100% is flagged and every figure is scaled to what you
actually entered), count a fund with no published fee as free (averages are
taken over the covered weight, and the tile says how much that is), or present
the weighted one-year figure as a backtest — it assumes today's weights held
all year with no rebalancing, which is a hypothetical, not history.

It recommends nothing. It applies your weights and reports the arithmetic.
The portfolio is saved in `localStorage` on your device.

### Tokyo-listed ETFs

`tokyo.html` lists the 274 ETFs on the Tokyo Stock Exchange, from JPX's own
published issue table: code, fund name, management company, index tracked,
listing date, trading unit and trust fee. **Fee coverage is complete** — 274 of
274, against 80% on the US side — with a median of 0.220%.

There is no price, daily move, one-year return, fund size or holdings, and that
is not an oversight. No free source publishes quotes for Tokyo codes: the Nasdaq
screener rejects them, Yahoo rate-limits on sight, Stooq carries no JP symbols,
and JPX publishes no free quote file. The SEC feeds behind the US page's
holdings and expense data cover only US-registered funds, which these are not.

It is a separate page for the same reason. Merged into the US table these funds
would be blank under half its columns and would break sorting on price and
return. Fee, manager, index and listing date are directly comparable across the
two; nothing else is.

### Newly listed funds

The nightly run refetches the whole universe, so new listings appear on their
own — but the file just gets longer, which says nothing about *which* funds are
new. `scripts/track_new.py` keeps a first-seen date per ticker in
`data/first_seen.json`; anything listed or first seen in the last 90 days gets a
**New** badge and can be filtered with the New button.

Seeding matters here. On the first run every ticker is "first seen today",
which would tag all 5,250 as new, so the backfill uses each fund's listing date
instead. A fund is tagged on the *earlier* of (first seen, listed), so one that
listed years ago but only just entered the screener — a re-listing, a ticker
change, a data fix — is not announced as a new launch.

### Comparing funds

Tick the checkbox on up to four rows and press Compare. The table puts fee, net
assets, listing date, leverage and returns side by side, marking every fund tied
for the lowest fee, largest size and strongest return.

Below it, **shared top-10 holdings**: which positions the funds have in common
and at what weight. `IVV` and `VOO` share nine of ten at matching weights;
`SCHD` shares none with either. Issuer names are normalised before matching —
filers punctuate the same company differently (`Alphabet, Inc.` vs
`Alphabet Inc`), and without that step the funds that overlap most report almost
no overlap at all. Share classes collapse into one issuer, so a fund holding
both Alphabet A and C shows their combined weight.

This is overlap *within published top-10s*, not true portfolio overlap — the
full position lists are not in this dataset.

### Expense ratios

Funds tag their fee table in XBRL when they file a prospectus, and the SEC
republishes those tags quarterly. This is the only free bulk source; N-PORT
carries holdings but no fees.

A prospectus is filed roughly **annually**, so one quarter holds only a slice of
the universe — 2026q2 alone covered 932 of our funds. Coverage comes from
accumulating quarters, and `data/expenses.json` is cumulative: 8 quarters reach
**4,177 of 5,253 (80%)**, and each run pulls only quarters it has not seen.

Net expense (after waivers) is preferred over gross, since net is what a holder
pays. The modal states which basis a figure uses. Values above 10% are dropped —
the raw data contains figures as absurd as 85%, which are mis-tagged rather than
real.

### Holdings

Click any fund for its ten largest positions. These come from SEC Form N-PORT,
the only free source covering the whole universe in one download — every
registered investment company files its full portfolio quarterly.

Two things to keep in mind:

- **They lag.** N-PORT is published ~60 days after a quarter ends, so report
  dates currently span 2026-02 to 2026-04. Each drawer shows its own as-of date.
  These describe how a fund *was* positioned.
- **Coverage is 4,042 of 5,250 funds (77%).** The gap is structural, not a bug:
  commodity and crypto grantor trusts (`GLD`, `IBIT`, `SLV`) file 10-Ks, and
  unit investment trusts (`SPY`, `DIA`, `MDY`) file nothing of this shape.
  Coverage is 84–96% across the diversified categories and weakest in commodity
  (43%) and crypto (48%) — categories where the funds hold one asset anyway.

Swap-based funds report *notional*, so a levered fund's positions can sum well
past 100% of net assets; the drawer says so rather than showing an apparently
broken total.

Name inference is accurate enough to filter on and wrong often enough that you
should confirm against the fund's own fact sheet before acting on it. A fund
whose name does not say what it holds will be classified on what its name
suggests.

Two source limitations worth knowing:

- **Every added field is partial.** Fees cover 80% of funds, holdings 77%,
  listing dates 99.96%. Missing values render as `—` and sort last; nothing is
  estimated or filled in, because a guessed fee is worse than a blank one.
- **Names are truncated at 61 characters** by Nasdaq itself. 461 funds arrive
  with their tails cut off, which is why some rows end mid-word and why a
  classifier keyed on a trailing word can miss them.

Prices are end-of-day and unadjusted. Nothing here is investment advice.

## Layout

```
index.html                     the dashboard — one self-contained file
portfolio.html                 portfolio builder / weight modelling
tokyo.html                     Tokyo Stock Exchange ETF list
data/etfs.json                 one record per fund (generated)
data/meta.json                 counts, breakdowns, timestamps (generated)
data/inception.json            permanent ticker -> first-trading-day cache
data/holdings.json             top 10 positions per fund (generated)
data/expenses.json             cumulative ticker -> expense ratio cache
data/positions/<TICKER>.json   largest 500 positions, fetched on demand
data/first_seen.json           ticker -> date first seen in the universe
data/classification.json       last category per ticker (for hysteresis)
data/tokyo.json                Tokyo-listed ETFs (generated)
scripts/fetch_etfs.py          fetch + derive + write (standard library only)
scripts/fetch_inception.py     top up the inception cache, merge into etfs.json
scripts/fetch_holdings.py      stream SEC N-PORT, keep each fund's top 10
scripts/fetch_expenses.py      accumulate expense ratios across RR quarters
scripts/reclassify.py          category from the fund's own filing
scripts/track_new.py           first-seen dates, tags newly listed funds
scripts/fetch_tokyo.py         parse JPX's listed ETF issue table
scripts/test_derive.py         regression tests for the name-derived fields
scripts/test_classify.py       regression tests for the filing-based category
scripts/serve.py               local static server for previewing
.github/workflows/refresh-data.yml   weeknight refresh, commits data/
.github/workflows/refresh-holdings.yml  monthly N-PORT check
.github/workflows/ci.yml             validates data, tests, JS syntax
```

## Running it locally

```bash
python3 scripts/fetch_etfs.py && python3 scripts/fetch_inception.py && python3 -m http.server 8791
```

Then open http://localhost:8791.

`fetch_inception.py` looks up at most 300 funds per run by default. To rebuild
the cache from empty — roughly 5,000 lookups, tens of minutes — pass `--all`.

## Changing the taxonomy

Sponsor, category, region and leverage rules all live at the top of
`scripts/fetch_etfs.py` as ordered keyword lists — first match wins, so narrow
buckets must precede broad ones. After editing, run:

```bash
python3 scripts/test_derive.py
```

Those cases are the ones that have actually broken before: registrant shells
(`Baron ETF Trust Baron Emerging Markets Select ETF`), sponsors whose own name
ends in "Trust" (First Trust), levered index funds that must not be mistaken for
single-stock wrappers, and "Short" used as a maturity rather than a direction.
Add a case whenever you fix a misclassification.
