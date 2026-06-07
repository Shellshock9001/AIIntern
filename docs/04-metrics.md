# 04 — `metrics.py`: Derived Metrics

This module turns raw XBRL facts into the derived metrics the dashboard charts and the
agent answers from. Its non-negotiable property: **every metric shows its formula and
its input facts, and every input traces to an accession number.** Nothing here is
computed by an LLM.

## The output objects

```python
@dataclass
class Provenance:           # one input fact behind a metric
    concept, value, fy, accession, form, period_end

@dataclass
class DerivedMetric:
    ticker, name, fy
    value: float | None     # None => not computable; we REFUSE, never guess
    unit: str               # "%", "x", "USD", "ratio"
    formula: str            # human-readable, reproducible
    inputs: list[Provenance]
    note: str
```

`value=None` is meaningful: it records that we *tried* and the inputs weren't there,
so the metric is explicitly not computable rather than silently absent.

## The annual-period logic (the critical correctness step)

XBRL bundles annual and quarterly entries under one `fy` label. `_annual_index` builds
`{(concept, period_fy): Fact}` selecting the **genuine annual value**:

- **Flow items** (Revenue, NetIncome, cash flows): require `is_annual_period()` —
  a ~365-day duration — so quarterly slices embedded in a 10-K are excluded.
- **Instant items** (Assets, Equity): take the period-end-dated snapshot.
- Keyed by the year the period **ends** in (`_period_fy`), not the filing's `fy` label.
- On a true restatement (same period, two values), keep the **latest-filed**.

This is what makes the numbers correct. Without it, you compute margins by dividing an
annual numerator by a quarterly denominator and get nonsense.

## The metrics, with formulas

All computed from annual (10-K) figures so periods are comparable.

| Metric | Formula | Unit | Inputs |
|--------|---------|------|--------|
| Gross Margin | `GrossProfit / Revenue × 100` (or `(Rev − CostOfRev)/Rev`) | % | GrossProfit/CostOfRevenue, Revenue |
| Operating Margin | `OperatingIncome / Revenue × 100` | % | OperatingIncome, Revenue |
| Net Margin | `NetIncome / Revenue × 100` | % | NetIncome, Revenue |
| Debt-to-Equity | `LongTermDebt / StockholdersEquity` | x | LongTermDebt, Equity |
| Free Cash Flow | `OperatingCashFlow − CapEx` | USD | OCF, CapEx |
| Revenue YoY Growth | `(Rev_t − Rev_{t−1}) / Rev_{t−1} × 100` | % | Revenue (two years) |
| Revenue CAGR | `(Rev_end / Rev_start)^(1/n) − 1` | % | Revenue (first & last year) |
| Current Ratio | `CurrentAssets / CurrentLiabilities` | x | CA, CL |
| SG&A Intensity | `SGA / Revenue × 100` | % | SGA, Revenue |
| R&D Intensity | `ResearchDev / Revenue × 100` | % | R&D, Revenue |
| Asset Turnover | `Revenue / TotalAssets` | x | Revenue, Assets |
| Effective Tax Rate | `IncomeTax / PretaxIncome × 100` | % | Tax, PretaxIncome |
| Capital Returned / OCF | `(Buyback + Dividends) / OCF × 100` | % | Buyback, Dividends, OCF |
| Return on Assets | `NetIncome / TotalAssets × 100` | % | NetIncome, Assets |
| Return on Equity | `NetIncome / StockholdersEquity × 100` | % | NetIncome, Equity |

Each is appended **only when its inputs exist** for that year — the source of the
sector-adaptive metric counts.

### Refusal-by-design examples

- FCF when CapEx isn't tagged that year → `value=None`, `note` explains why.
- YoY across a gap in years → skipped (no fabricated interpolation).
- CAGR needs `Rev_end > 0` and ≥2 years → otherwise omitted.

## Conflict detection (also lives here)

`detect_conflicts(facts)` finds `(concept, period)` cells where 10-K filings disagree
by >0.5% — restatements. It groups by exact `period_end` so multi-year reporting isn't
mistaken for a conflict, and reports every disagreement with the resolution rule
("latest-filed"). The richer taxonomy (scale/fiscal/tag) is in
[07-linkage-conflicts-briefing.md](07-linkage-conflicts-briefing.md).

## Entry point

```python
compute_metrics(ticker) -> (metrics: list[DerivedMetric], facts: list[Fact], conflicts)
```

Returns everything a consumer needs: the derived metrics, the raw facts they came
from, and any restatement conflicts. The app caches this per ticker.

## Adding a metric

1. Ensure its input concepts are in `CONCEPT_ALIASES` (in `sec_client.py`).
2. In `compute_metrics`, fetch the inputs with `get("Concept", fy)`.
3. Append a `DerivedMetric` with the value, unit, formula string, and
   `[_prov(input1), _prov(input2)]`.
4. Guard with `if input_a and input_b and input_b.value:` so it refuses cleanly when
   inputs are missing.

That's it — it appears in charts, drill-down, briefing, and is answerable by the agent
automatically.
