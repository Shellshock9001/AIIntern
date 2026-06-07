"""
metrics.py — Derived metric engine.

Design rule: a computed metric is only as trustworthy as its provenance. Every
DerivedMetric records the exact input facts (value + accession number) and the
formula string, so the dashboard can show "how we got this" and a human can
re-verify against the filing. Nothing is computed by the LLM.

We work off ANNUAL (10-K) figures for trend/CAGR work to avoid mixing fiscal
period lengths. Quarterly facts are retained but flagged so we never silently
compare a quarter against a full year.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sec_client import Fact, extract_facts


@dataclass
class Provenance:
    concept: str
    value: float
    fy: int
    accession: str
    form: str
    period_end: str


@dataclass
class DerivedMetric:
    ticker: str
    name: str
    fy: int
    value: Optional[float]          # None => not computable (we refuse, not invent)
    unit: str                       # "%", "x", "USD", "ratio"
    formula: str
    inputs: list[Provenance] = field(default_factory=list)
    note: str = ""

    def is_traceable(self) -> bool:
        return self.value is not None and len(self.inputs) > 0


def _period_fy(f: Fact) -> int:
    """Fiscal year the value's PERIOD ends in (from period_end), not the filing label."""
    if f.period_end:
        try:
            return int(f.period_end[:4])
        except ValueError:
            pass
    return f.fy


def _annual_index(facts: list[Fact]) -> dict[tuple[str, int], Fact]:
    """
    Build {(concept, period_fy): Fact} of the genuine ANNUAL value for each year.

    Critical correctness step: XBRL bundles annual + quarterly entries under one
    'fy' label, so we select by actual period:
      - flow items (Revenue, NetIncome ...) -> require a ~full-year duration
      - instant items (Assets, Equity ...)  -> take the period_end-dated value
    Keyed by the year the period ENDS in. On true restatement (same period, two
    values), keep the latest-filed.
    """
    idx: dict[tuple[str, int], Fact] = {}
    for f in facts:
        if f.form != "10-K":
            continue
        if f.is_instant():
            keep = True  # balance-sheet snapshot
        elif f.is_annual_period():
            keep = True  # full fiscal year flow
        else:
            keep = False  # quarterly slice embedded in the 10-K
        if not keep:
            continue
        key = (f.concept, _period_fy(f))
        if key not in idx or f.filed > idx[key].filed:
            idx[key] = f
    return idx


def detect_conflicts(facts: list[Fact]) -> list[dict]:
    """
    Surface (concept, year) cells where 10-K filings report DIFFERENT values for
    the SAME period (true restatements), >0.5% apart. We select by real period
    (mirroring _annual_index) so multi-year reporting is not mistaken for conflict.
    """
    buckets: dict[tuple[str, int], list[Fact]] = {}
    for f in facts:
        if f.form != "10-K":
            continue
        if not (f.is_instant() or f.is_annual_period()):
            continue
        buckets.setdefault((f.concept, _period_fy(f)), []).append(f)

    conflicts = []
    for (concept, yr), fs in buckets.items():
        # Group by exact period_end so we compare like-for-like snapshots.
        by_end: dict[str, set] = {}
        rows = []
        for x in fs:
            by_end.setdefault(x.period_end, set()).add(round(x.value, 2))
            rows.append({"value": x.value, "filed": x.filed,
                         "accession": x.accession, "period_end": x.period_end})
        disagreeing_end = next(
            (end for end, vals in by_end.items() if len(vals) > 1
             and (mx := max(vals)) != 0 and abs(mx - min(vals)) / abs(mx) > 0.005),
            None,
        )
        if disagreeing_end:
            conflicts.append({
                "concept": concept,
                "fy": yr,
                "period_end": disagreeing_end,
                "values": sorted(
                    (r for r in rows if r["period_end"] == disagreeing_end),
                    key=lambda d: d["filed"]),
                "chosen": "latest-filed",
            })
    return sorted(conflicts, key=lambda d: (d["concept"], d["fy"]))


def _prov(f: Fact) -> Provenance:
    return Provenance(f.concept, f.value, f.fy, f.accession, f.form, f.period_end)


def _compute_metrics_uncached(ticker: str) -> tuple[list[DerivedMetric], list[Fact], list[dict]]:
    facts = extract_facts(ticker)
    idx = _annual_index(facts)
    conflicts = detect_conflicts(facts)
    years = sorted({fy for (_, fy) in idx.keys()})
    out: list[DerivedMetric] = []

    def get(concept: str, fy: int) -> Optional[Fact]:
        return idx.get((concept, fy))

    for fy in years:
        rev = get("Revenue", fy)
        cor = get("CostOfRevenue", fy)
        gp = get("GrossProfit", fy)
        op = get("OperatingIncome", fy)
        ni = get("NetIncome", fy)
        eq = get("StockholdersEquity", fy)
        debt = get("LongTermDebt", fy)
        ocf = get("OperatingCashFlow", fy)
        capex = get("CapEx", fy)

        # Gross margin — prefer reported GrossProfit; else Revenue - CostOfRevenue.
        if rev and rev.value:
            if gp:
                out.append(DerivedMetric(
                    ticker, "Gross Margin", fy, gp.value / rev.value * 100, "%",
                    "GrossProfit / Revenue * 100", [_prov(gp), _prov(rev)]))
            elif cor:
                out.append(DerivedMetric(
                    ticker, "Gross Margin", fy,
                    (rev.value - cor.value) / rev.value * 100, "%",
                    "(Revenue - CostOfRevenue) / Revenue * 100",
                    [_prov(rev), _prov(cor)]))

            if op:
                out.append(DerivedMetric(
                    ticker, "Operating Margin", fy, op.value / rev.value * 100, "%",
                    "OperatingIncome / Revenue * 100", [_prov(op), _prov(rev)]))
            if ni:
                out.append(DerivedMetric(
                    ticker, "Net Margin", fy, ni.value / rev.value * 100, "%",
                    "NetIncome / Revenue * 100", [_prov(ni), _prov(rev)]))

        # Debt-to-equity.
        if debt and eq and eq.value:
            out.append(DerivedMetric(
                ticker, "Debt-to-Equity", fy, debt.value / eq.value, "x",
                "LongTermDebt / StockholdersEquity", [_prov(debt), _prov(eq)]))

        # Free cash flow (capex stored as positive outflow on EDGAR).
        if ocf and capex:
            out.append(DerivedMetric(
                ticker, "Free Cash Flow", fy, ocf.value - capex.value, "USD",
                "OperatingCashFlow - CapEx", [_prov(ocf), _prov(capex)]))
        elif ocf and not capex:
            out.append(DerivedMetric(
                ticker, "Free Cash Flow", fy, None, "USD",
                "OperatingCashFlow - CapEx", [_prov(ocf)],
                note="CapEx tag not reported this year — refusing to compute."))

        # --- Additional derived metrics (computed only when inputs exist) ---
        ca = get("CurrentAssets", fy)
        cl = get("CurrentLiabilities", fy)
        sga = get("SGA", fy)
        rnd = get("ResearchDev", fy)
        tax = get("IncomeTax", fy)
        pretax = get("PretaxIncome", fy)
        assets = get("TotalAssets", fy)
        buyback = get("StockBuyback", fy)
        div = get("Dividends", fy)

        # Current ratio (liquidity).
        if ca and cl and cl.value:
            out.append(DerivedMetric(
                ticker, "Current Ratio", fy, ca.value / cl.value, "x",
                "CurrentAssets / CurrentLiabilities", [_prov(ca), _prov(cl)]))

        if rev and rev.value:
            if sga:
                out.append(DerivedMetric(
                    ticker, "SG&A Intensity", fy, sga.value / rev.value * 100, "%",
                    "SGA / Revenue * 100", [_prov(sga), _prov(rev)]))
            if rnd:
                out.append(DerivedMetric(
                    ticker, "R&D Intensity", fy, rnd.value / rev.value * 100, "%",
                    "ResearchDev / Revenue * 100", [_prov(rnd), _prov(rev)]))
            if assets and assets.value:
                out.append(DerivedMetric(
                    ticker, "Asset Turnover", fy, rev.value / assets.value, "x",
                    "Revenue / TotalAssets", [_prov(rev), _prov(assets)]))

        # Effective tax rate.
        if tax and pretax and pretax.value:
            out.append(DerivedMetric(
                ticker, "Effective Tax Rate", fy, tax.value / pretax.value * 100, "%",
                "IncomeTax / PretaxIncome * 100", [_prov(tax), _prov(pretax)]))

        # Shareholder return (buyback + dividends) relative to FCF proxy.
        if (buyback or div) and ocf and ocf.value:
            returned = (buyback.value if buyback else 0) + (div.value if div else 0)
            inp = [p for p in [buyback, div, ocf] if p]
            out.append(DerivedMetric(
                ticker, "Capital Returned / OCF", fy, returned / ocf.value * 100, "%",
                "(Buyback + Dividends) / OperatingCashFlow * 100",
                [_prov(x) for x in inp]))

        # ROA / ROE.
        if ni and assets and assets.value:
            out.append(DerivedMetric(
                ticker, "Return on Assets", fy, ni.value / assets.value * 100, "%",
                "NetIncome / TotalAssets * 100", [_prov(ni), _prov(assets)]))
        if ni and eq and eq.value:
            out.append(DerivedMetric(
                ticker, "Return on Equity", fy, ni.value / eq.value * 100, "%",
                "NetIncome / StockholdersEquity * 100", [_prov(ni), _prov(eq)]))

    # Revenue YoY growth + CAGR (annual only).
    rev_by_year = {fy: idx[("Revenue", fy)] for (_, fy) in idx if ("Revenue", fy) in idx}
    sorted_years = sorted(rev_by_year)
    for i in range(1, len(sorted_years)):
        y0, y1 = sorted_years[i - 1], sorted_years[i]
        if y1 - y0 != 1:
            continue  # don't compute YoY across a gap
        f0, f1 = rev_by_year[y0], rev_by_year[y1]
        if f0.value:
            out.append(DerivedMetric(
                ticker, "Revenue YoY Growth", y1,
                (f1.value - f0.value) / f0.value * 100, "%",
                f"(Rev_FY{y1} - Rev_FY{y0}) / Rev_FY{y0} * 100",
                [_prov(f1), _prov(f0)]))

    if len(sorted_years) >= 2:
        y0, y1 = sorted_years[0], sorted_years[-1]
        f0, f1 = rev_by_year[y0], rev_by_year[y1]
        n = y1 - y0
        if f0.value and f1.value > 0 and n > 0:
            cagr = ((f1.value / f0.value) ** (1 / n) - 1) * 100
            out.append(DerivedMetric(
                ticker, "Revenue CAGR", y1, cagr, "%",
                f"(Rev_FY{y1} / Rev_FY{y0})^(1/{n}) - 1, over {n}y",
                [_prov(f1), _prov(f0)]))

    return out, facts, conflicts


# ---------------------------------------------------------------------------
# Disk-cached public entry point.
# ---------------------------------------------------------------------------
# compute_metrics is the unit the whole app/agent/eval consume, so caching lives
# HERE in the data layer (not sprinkled in the UI). Its full output is cached to
# disk keyed by ticker with a TTL, so even a cold start (fresh process) is fast,
# and every consumer benefits automatically.
from dataclasses import asdict as _asdict  # noqa: E402
import cache as _cache  # noqa: E402


def _serialize(metrics, facts, conflicts) -> dict:
    return {
        "metrics": [{**{k: v for k, v in _asdict(m).items() if k != "inputs"},
                      "inputs": [_asdict(p) for p in m.inputs]} for m in metrics],
        "facts": [_asdict(f) for f in facts],
        "conflicts": conflicts,
    }


def _deserialize(blob: dict):
    metrics = []
    for m in blob["metrics"]:
        md = {k: v for k, v in m.items() if k != "inputs"}
        metrics.append(DerivedMetric(**md,
                                     inputs=[Provenance(**p) for p in m["inputs"]]))
    facts = [Fact(**f) for f in blob["facts"]]
    return metrics, facts, blob["conflicts"]


def compute_metrics(ticker: str) -> tuple[list[DerivedMetric], list[Fact], list[dict]]:
    """Disk-cached. Recomputes only on a cache miss or TTL expiry."""
    tk = ticker.upper()
    hit = _cache.get("metrics", tk)
    if hit is not None:
        try:
            return _deserialize(hit)
        except (TypeError, KeyError):
            pass  # cached schema changed → fall through and recompute
    metrics, facts, conflicts = _compute_metrics_uncached(tk)
    _cache.put("metrics", tk, _serialize(metrics, facts, conflicts))
    return metrics, facts, conflicts


# Human-friendly labels + units for raw XBRL concepts (so the agent can answer
# "what was revenue" directly, not only derived metrics).
RAW_CONCEPT_LABELS = {
    "Revenue": ("Revenue", "USD"), "CostOfRevenue": ("Cost of Revenue", "USD"),
    "GrossProfit": ("Gross Profit", "USD"), "OperatingIncome": ("Operating Income", "USD"),
    "NetIncome": ("Net Income", "USD"), "ResearchDev": ("R&D Expense", "USD"),
    "SGA": ("SG&A Expense", "USD"), "InterestExpense": ("Interest Expense", "USD"),
    "IncomeTax": ("Income Tax", "USD"), "PretaxIncome": ("Pretax Income", "USD"),
    "EPS_Diluted": ("Diluted EPS", "USD/sh"), "EPS_Basic": ("Basic EPS", "USD/sh"),
    "DilutedShares": ("Diluted Shares", "shares"),
    "TotalAssets": ("Total Assets", "USD"), "TotalLiabilities": ("Total Liabilities", "USD"),
    "StockholdersEquity": ("Stockholders' Equity", "USD"),
    "CashAndEquivalents": ("Cash & Equivalents", "USD"),
    "CurrentAssets": ("Current Assets", "USD"), "CurrentLiabilities": ("Current Liabilities", "USD"),
    "Inventory": ("Inventory", "USD"), "AccountsReceivable": ("Accounts Receivable", "USD"),
    "Goodwill": ("Goodwill", "USD"), "LongTermDebt": ("Long-Term Debt", "USD"),
    "SharesOutstanding": ("Shares Outstanding", "shares"),
    "OperatingCashFlow": ("Operating Cash Flow", "USD"), "CapEx": ("Capital Expenditures", "USD"),
    "DepreciationAmort": ("Depreciation & Amortization", "USD"),
    "Dividends": ("Dividends Paid", "USD"), "StockBuyback": ("Stock Buybacks", "USD"),
}


def get_raw_value(ticker: str, concept: str, fy: Optional[int] = None
                  ) -> Optional[DerivedMetric]:
    """
    Return a raw XBRL concept value as a DerivedMetric (so it carries provenance),
    for the requested fiscal year or the latest available.
    """
    facts = extract_facts(ticker, [concept])
    idx = _annual_index(facts)
    rows = {y: f for (c, y), f in idx.items() if c == concept}
    if not rows:
        return None
    if fy is not None:
        if fy not in rows:
            return None
        f = rows[fy]
    else:
        f = rows[max(rows)]
    label, unit = RAW_CONCEPT_LABELS.get(concept, (concept, "USD"))
    return DerivedMetric(ticker, label, f.fy, f.value, unit,
                         f"Reported {concept} (XBRL)", [_prov(f)])


def available_years(ticker: str, name_or_concept: str) -> list[int]:
    """Years available for a derived metric OR a raw concept."""
    metrics, facts, _ = compute_metrics(ticker)
    yrs = {m.fy for m in metrics
           if name_or_concept.lower() in m.name.lower() and m.value is not None}
    if not yrs:
        idx = _annual_index(facts)
        yrs = {y for (c, y) in idx if c == name_or_concept}
    return sorted(yrs)


def capability_catalog(ticker: str) -> dict:
    """
    Everything answerable for this company: derived metric names + raw concepts,
    each with the fiscal years available. The agent reasons over THIS, so it can
    answer anything in the data without hardcoded keywords.
    """
    metrics, facts, _ = compute_metrics(ticker)
    derived: dict[str, list[int]] = {}
    for m in metrics:
        if m.value is not None:
            derived.setdefault(m.name, []).append(m.fy)
    idx = _annual_index(facts)
    raw: dict[str, list[int]] = {}
    for (c, y) in idx:
        if c in RAW_CONCEPT_LABELS:
            raw.setdefault(c, []).append(y)
    return {
        "derived_metrics": {k: sorted(v) for k, v in derived.items()},
        "raw_concepts": {k: sorted(v) for k, v in raw.items()},
    }


if __name__ == "__main__":
    import config
    for tk in config.active_tickers()[:2]:
        metrics, facts, conflicts = compute_metrics(tk)
        latest = max(m.fy for m in metrics)
        print(f"\n=== {tk} (latest FY{latest}) ===")
        for m in metrics:
            if m.fy == latest and m.value is not None:
                v = f"{m.value:,.2f}{m.unit}" if m.unit != "USD" else f"${m.value:,.0f}"
                print(f"  {m.name:22s} {v:>18s}  <- {m.formula}")
        print(f"  conflicts detected: {len(conflicts)}")
