# etf-universe

Every US-listed ETF in one searchable table — 5,261 funds, refreshed each weekday
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
derivative income** — 1,948 of the 5,261 funds — leaving 3,313 diversified
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
| Sponsor, category, region, leverage | Inferred from the fund name |

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

It reports blended fee, annual cost in dollars against a portfolio value you
set, weighted one-year and one-day moves, effective leverage, and the split by
category and region. Below that, **look-through**: each fund's published top-10
positions combined and weighted by its share of the portfolio, so you can see
what the blend actually owns and where it doubles up.

Three things it deliberately does not do: renormalise weights behind your back
(a total that is not 100% is flagged and every figure is scaled to what you
actually entered), count a fund with no published fee as free (averages are
taken over the covered weight, and the tile says how much that is), or present
the weighted one-year figure as a backtest — it assumes today's weights held
all year with no rebalancing, which is a hypothetical, not history.

It recommends nothing. It applies your weights and reports the arithmetic.
The portfolio is saved in `localStorage` on your device.

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
**4,198 of 5,261 (80%)**, and each run pulls only quarters it has not seen.

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
- **Coverage is 4,042 of 5,261 funds (77%).** The gap is structural, not a bug:
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
data/etfs.json                 one record per fund (generated)
data/meta.json                 counts, breakdowns, timestamps (generated)
data/inception.json            permanent ticker -> first-trading-day cache
data/holdings.json             top 10 positions per fund (generated)
data/expenses.json             cumulative ticker -> expense ratio cache
scripts/fetch_etfs.py          fetch + derive + write (standard library only)
scripts/fetch_inception.py     top up the inception cache, merge into etfs.json
scripts/fetch_holdings.py      stream SEC N-PORT, keep each fund's top 10
scripts/fetch_expenses.py      accumulate expense ratios across RR quarters
scripts/test_derive.py         regression tests for the derived fields
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
