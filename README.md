# etf-universe

Every US-listed ETF in one searchable table — 5,261 funds, refreshed each weekday
after the close.

Live: https://joeedessa.github.io/etf-universe/

## What this is

A single static page over a nightly snapshot of the public Nasdaq ETF screener.
No build step, no dependencies, no API keys. GitHub Actions refreshes the data
and commits it; GitHub Pages serves the page.

Filter by sponsor, category, region and leverage; search by ticker or name; sort
by today's move, one-year return or price.

## What the data is, and is not

The screener publishes the full list of listed ETFs with a quote for each. That
is all it publishes. Everything else in the table is **derived from the fund's
name**:

| Field | Source |
|---|---|
| Ticker, fund name, price, today's %, 1-year % | Nasdaq screener |
| Sponsor, category, region, leverage | Inferred from the fund name |

Name inference is accurate enough to filter on and wrong often enough that you
should confirm against the fund's own fact sheet before acting on it. A fund
whose name does not say what it holds will be classified on what its name
suggests.

Two source limitations worth knowing:

- **No expense ratio, AUM, inception date or holdings.** The screener does not
  carry them. They are absent rather than estimated — a guessed expense ratio is
  worse than none.
- **Names are truncated at 61 characters** by Nasdaq itself. 461 funds arrive
  with their tails cut off, which is why some rows end mid-word and why a
  classifier keyed on a trailing word can miss them.

Prices are end-of-day and unadjusted. Nothing here is investment advice.

## Layout

```
index.html                     the dashboard — one self-contained file
data/etfs.json                 one record per fund (generated)
data/meta.json                 counts, breakdowns, timestamps (generated)
scripts/fetch_etfs.py          fetch + derive + write (standard library only)
scripts/test_derive.py         regression tests for the derived fields
.github/workflows/refresh-data.yml   weeknight refresh, commits data/
.github/workflows/ci.yml             validates data, tests, JS syntax
```

## Running it locally

```bash
python3 scripts/fetch_etfs.py && python3 -m http.server 8791
```

Then open http://localhost:8791.

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
