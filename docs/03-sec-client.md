# 03 — `sec_client.py`: The Data Layer

This module is the only thing that talks to SEC EDGAR. Everything else consumes its
output. It is fully generic — every function works for any of the ~10,000 SEC filers.

## Responsibilities

1. Resolve tickers ↔ CIK.
2. Fetch XBRL facts (the numbers) with full provenance.
3. Fetch filing documents (the narrative text).
4. Provide page/table-level deep links.
5. Cache everything and respect SEC's rate limit.

## Networking: `_get`, `_throttle`, caching

```python
HEADERS = {"User-Agent": "ARGUS-FinDash research contact@…"}  # SEC requires this
_MIN_INTERVAL = 0.15   # ~6 req/s, safely under SEC's 10/s
```

- `_throttle()` sleeps as needed between calls.
- `_get(url, cache_key=…)` fetches once, then serves from `data/cache/` on repeat —
  filings don't change, so caching is safe and makes re-runs instant and offline-ish.
- SEC **requires** a descriptive `User-Agent` with contact info; requests without one
  get blocked. Override it via the constant if you fork this.

## Ticker resolution: `load_ticker_map`, `resolve_cik`

`load_ticker_map()` downloads `company_tickers.json` (cached) → `{TICKER: {cik, title}}`.
`resolve_cik("NVDA")` → `{"cik": "0001045810", "title": "NVIDIA CORP"}`. The CIK must
be zero-padded to 10 digits for the data.sec.gov endpoints. This map is what makes the
universe dynamic.

## The `Fact` object — a number with its papers

```python
@dataclass
class Fact:
    ticker, concept, label, value, unit
    fy, fp, form               # fiscal year/period, 10-K vs 10-Q
    period_start, period_end   # the actual reporting window
    filed, accession           # when filed, and the CITATION anchor
    frame                      # XBRL calendrical frame, if present
```

Helper methods encode the hard-won period logic:
- `duration_days()` — span of the period (None for instantaneous balance-sheet items).
- `is_annual_period()` — True if 340–400 days (a real fiscal year, not a quarter).
- `is_instant()` — True for balance-sheet snapshots (no start date).

These let downstream code separate annual flows from quarterly slices and point-in-time
balances — the distinction XBRL's `fy` field blurs.

## Concept aliases — handling tag drift

```python
CONCEPT_ALIASES = {
  "Revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", …],
  "NetIncome": ["NetIncomeLoss", "ProfitLoss"],
  …  # ~35 canonical concepts, each mapping to the real XBRL tags companies use
}
```

A *canonical* name (what our code and UI use) maps to an ordered list of real XBRL
tags. The order is priority: earlier aliases win when multiple are present for the same
period. To add a metric input, add its canonical name and the tags companies file it
under.

## `extract_facts` — the heart of extraction

```python
extract_facts(ticker, concept_keys=None) -> list[Fact]
```

For each canonical concept, it walks **all** aliases (not just the first present),
collects every 10-K/10-Q fact, and resolves per-period collisions by alias priority.
This is what fixes the tag-switch bug: a company that changed tags mid-history still
gets a continuous series, because we don't stop at the first alias that appears
anywhere — we merge them by period.

Collision key: `(form, period_start, period_end)`. Same form + same window = same
datapoint; highest-priority alias wins.

## Concept discovery — adapting to the company

```python
discover_concepts(ticker) -> {canonical: {available: bool, tag: str|None}}
coverage_summary(ticker)  -> {present: [...], missing: [...], coverage_pct: float}
```

Reports which canonical concepts a given filer actually reports. This drives the
sector-adaptive behavior: the metrics engine and UI only attempt metrics whose inputs
exist. A bank legitimately shows ~65% coverage (no inventory, no gross profit).

## Filings: `FilingRef`, `list_filings`, `download_filing`

- `list_filings(ticker, forms, limit)` → recent `FilingRef`s from the Submissions API.
- `FilingRef.doc_url(cik)` / `index_url(cik)` build the canonical SEC URLs.
- `download_filing(ref)` fetches the primary HTML to `data/filings/` (cached).

## Deep links: `statement_links`, `deep_link_for`

```python
deep_link_for("Revenue", accession, "NVDA")
# → https://www.sec.gov/Archives/edgar/data/1045810/0001…/R4.htm  (income statement)
```

`statement_links()` parses `FilingSummary.xml` to find each financial statement's
R-file, skipping parenthetical/detail variants. `deep_link_for()` maps a concept to
its statement type (income/balance/cash) and returns the table URL. This gives the
"page/table" granularity the brief asks for — verify any number in one click.

## Extension points

- **Add a concept:** add to `CONCEPT_ALIASES` with its real XBRL tag(s). It flows into
  discovery, extraction, and (if you add a formula) metrics automatically.
- **Support 20-F (foreign filers):** add `"20-F"` to the `forms` filter and the
  annual-period logic still applies.
- **Change the cache location:** edit `CACHE_DIR` / `FILINGS_DIR`.

## Gotchas

- The data.sec.gov endpoints want the **zero-padded 10-digit** CIK; the Archives path
  wants the **unpadded** integer CIK. Both forms are handled internally — mind this if
  you build new URLs.
- `companyfacts` returns **consolidated** figures only. Segment data lives in raw XBRL
  instance documents with member dimensions, not this endpoint (see conflicts doc).
