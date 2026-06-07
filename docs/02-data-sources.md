# 02 — Data Sources: What You're Looking At and Why

Everything in ARGUS comes from **public SEC EDGAR data**. No paid feeds, no scraping
of third parties. This document explains exactly what that data is, what the numbers
mean, and where each piece comes from — so you can trust it and extend it.

## Where the data comes from

### 1. XBRL CompanyFacts API (the numbers)

**Endpoint:** `https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json`
**Auth:** none (SEC requires only a descriptive `User-Agent` header)
**Rate limit:** 10 requests/sec (we self-throttle to ~6/sec)

This returns **every XBRL-tagged financial fact a company has ever filed**, as JSON.
A single call for NVIDIA returns ~330 distinct financial concepts; for a bank like
JPMorgan, ~900+. Each fact includes the value, the unit, the fiscal year/period, the
form it came from (10-K/10-Q), the filing date, and — critically — the **accession
number** of the source filing.

### 2. Submissions API (the filing list)

**Endpoint:** `https://data.sec.gov/submissions/CIK{10-digit}.json`
Lists a company's filings with form type, accession number, primary document name,
filing date, and report (period) date. We use it to enumerate 10-Ks and 10-Qs.

### 3. Filing documents (the narrative text)

**Path:** `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{primaryDoc}`
The actual 10-K/10-Q HTML. We extract the prose sections (MD&A, Risk Factors,
Business) for the RAG layer. The numbers in these documents' tables are *not* used as
a data source — those come from XBRL.

### 4. FilingSummary.xml (the deep-link map)

**Path:** `…/{accession}/FilingSummary.xml`
Lists every "R-file" (rendered financial statement) in a filing — `R4.htm` =
income statement, `R6.htm` = balance sheet, etc. We use it to deep-link a metric to
the exact table it was read from.

### 5. Ticker → CIK map

**File:** `https://www.sec.gov/files/company_tickers.json`
Maps every ticker to its CIK (Central Index Key — the SEC's unique company ID). This
is what makes the system dynamic: any of the ~10,000 tickers here can be analyzed.

## What is XBRL, and why does it matter?

**XBRL** (eXtensible Business Reporting Language) is a structured tagging standard the
SEC has required in financial filings since ~2009. When a company reports "Revenue:
$60,922,000,000," it also tags that number with a standardized machine-readable
concept like `Revenues` or `RevenueFromContractWithCustomerExcludingAssessedTax`.

This is the keystone of ARGUS's accuracy. Because the **company itself** produced the
tag-value pair as part of its legally-filed financial statements, the number is
authoritative. We read it directly; no parsing of PDFs, no LLM extraction, no OCR.
The number you see is the number the company filed.

### The shape of an XBRL fact

```json
{
  "val": 60922000000,           // the value, in raw units (not thousands/millions)
  "unit": "USD",
  "start": "2023-01-30",        // period start (absent for balance-sheet items)
  "end":   "2024-01-28",        // period end
  "fy": 2024,                   // fiscal year LABEL of the filing
  "fp": "FY",                   // fiscal period (FY, Q1, Q2, Q3)
  "form": "10-K",
  "filed": "2024-02-21",
  "accn": "0001045810-24-000029" // accession number → the citation anchor
}
```

### Two subtleties that bite naive implementations

1. **`fy` labels the filing, not the value.** A FY2024 10-K contains prior-year
   quarterly figures all tagged `fy: 2024`. If you key data on `(concept, fy)` you
   collide annual and quarterly values. ARGUS selects the genuine annual figure by
   **period duration** (~365 days) and keys by the year the period *ends* in. See
   [04-metrics.md](04-metrics.md).

2. **Companies switch tags over time.** NVIDIA reported revenue under
   `RevenueFromContractWithCustomerExcludingAssessedTax` in older filings and
   `Revenues` recently. A single-tag lookup silently loses years. ARGUS collects from
   all known aliases and stitches by period. See [03-sec-client.md](03-sec-client.md).

## What the numbers mean (financial primer)

The raw XBRL concepts we pull, grouped by statement:

**Income statement** (performance over a period)
- `Revenue` — total sales.
- `CostOfRevenue` — direct cost of producing what was sold.
- `GrossProfit` — Revenue − CostOfRevenue. What's left to cover everything else.
- `OperatingIncome` — profit from core operations (after R&D, SG&A).
- `NetIncome` — bottom-line profit after everything (interest, tax).
- `ResearchDev`, `SGA` — the two big operating expense lines.
- `EPS_Diluted` — net income per diluted share.

**Balance sheet** (a snapshot at one instant)
- `TotalAssets`, `TotalLiabilities`, `StockholdersEquity` — Assets = Liabilities +
  Equity, the accounting identity.
- `CurrentAssets` / `CurrentLiabilities` — due within a year; their ratio is liquidity.
- `Inventory`, `AccountsReceivable`, `Goodwill`, `CashAndEquivalents`, `LongTermDebt`.

**Cash flow** (actual cash moved over a period)
- `OperatingCashFlow` — cash generated by the business.
- `CapEx` — cash spent on property/plant/equipment.
- `Dividends`, `StockBuyback` — cash returned to shareholders.

### Why we compare within a sector

The brief recommends competitors in one sector, and for good reason: a 70% gross
margin is extraordinary for a retailer and ordinary for a software firm. Comparing
NVIDIA, AMD, Intel, and Broadcom is meaningful because they face similar economics.
The default seed is these four semiconductors; the universe is fully changeable.

### Sector-dependent coverage (why a bank looks different)

Banks don't report "gross profit" or "inventory" — those concepts don't apply to their
business. ARGUS *discovers* which concepts each company actually reports
(`coverage_summary()`) and computes only the metrics whose inputs exist. This is why
JPMorgan shows ~8 metrics and NVIDIA shows ~15. The system adapts rather than forcing
a one-size-fits-all template and producing blanks or errors.

## Citing the data (as the brief requires)

Every figure in the app is traceable to:
- the **accession number** (which filing),
- the **fiscal year / period end** (which period),
- the **XBRL concept tag** (which line item),
- and a **deep link** to the rendered statement table.

All sources are public SEC filings at `sec.gov`. The README lists the specific
filings and CIKs for the default universe.
