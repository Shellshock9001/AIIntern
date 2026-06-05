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


def compute_metrics(ticker: str) -> tuple[list[DerivedMetric], list[Fact], list[dict]]:
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


if __name__ == "__main__":
    for tk in ["NVDA", "INTC"]:
        metrics, facts, conflicts = compute_metrics(tk)
        latest = max(m.fy for m in metrics)
        print(f"\n=== {tk} (latest FY{latest}) ===")
        for m in metrics:
            if m.fy == latest and m.value is not None:
                v = f"{m.value:,.2f}{m.unit}" if m.unit != "USD" else f"${m.value:,.0f}"
                print(f"  {m.name:22s} {v:>18s}  <- {m.formula}")
        print(f"  conflicts detected: {len(conflicts)}")
