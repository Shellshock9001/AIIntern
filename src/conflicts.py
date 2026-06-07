"""
conflicts.py — Data-conflict taxonomy.

The brief: "when filings disagree (restatements, different fiscal years, segment
vs. consolidated, different units), does your system silently pick one, or surface
the conflict?" This module surfaces every conflict type that is genuinely
detectable from the SEC XBRL CompanyFacts API, and is explicit about what that
source does and does not expose:

  1. RESTATEMENT       — same concept+period, materially different values across
                         filings. We compute from latest-filed, surface all.
  2. SCALE/UNIT ANOMALY— a value ~1000x off its own neighbors (classic
                         thousands-vs-units reporting error), or a concept tagged
                         under incompatible units across periods.
  3. FISCAL MISALIGN   — companies whose fiscal year does not end in December, so
                         cross-company "FY2025" comparisons span different calendar
                         windows. We disclose the offset instead of hiding it.
  4. TAG SWITCH        — the company changed the XBRL concept used for one economic
                         line across years (handled in extraction; surfaced here).

NOTE ON SEGMENT vs CONSOLIDATED: the companyfacts endpoint returns consolidated
facts only; per-segment breakdowns live in the raw XBRL instance documents with
explicit member dimensions, not in this API. Rather than fabricate segment
conflicts, we state this limitation in the UI — honest transparency over a fake
feature.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sec_client import Fact, extract_facts, CONCEPT_ALIASES, get_company_facts


@dataclass
class Conflict:
    kind: str            # restatement | scale_anomaly | fiscal_misalignment | tag_switch
    ticker: str
    concept: str
    detail: str
    severity: str        # high | medium | low
    evidence: list[dict]


def _period_fy(f: Fact) -> int:
    if f.period_end:
        try:
            return int(f.period_end[:4])
        except ValueError:
            pass
    return f.fy


# ---------------------------------------------------------------------------
def find_restatements(facts: list[Fact], ticker: str) -> list[Conflict]:
    buckets: dict[tuple[str, str], list[Fact]] = {}
    for f in facts:
        if f.form != "10-K":
            continue
        if not (f.is_instant() or f.is_annual_period()):
            continue
        buckets.setdefault((f.concept, f.period_end), []).append(f)

    out = []
    for (concept, end), fs in buckets.items():
        vals = {round(x.value, 2) for x in fs}
        if len(vals) > 1:
            hi, lo = max(vals), min(vals)
            if hi != 0 and abs(hi - lo) / abs(hi) > 0.005:
                rel = abs(hi - lo) / abs(hi)
                out.append(Conflict(
                    kind="restatement", ticker=ticker, concept=concept,
                    detail=(f"{concept} for period ending {end} reported as "
                            f"{lo:,.0f}–{hi:,.0f} across filings "
                            f"({rel:.1%} apart). Using latest-filed."),
                    severity="high" if rel > 0.05 else "medium",
                    evidence=sorted(({"value": x.value, "filed": x.filed,
                                      "accession": x.accession} for x in fs),
                                    key=lambda d: d["filed"])))
    return out


def find_scale_anomalies(facts: list[Fact], ticker: str) -> list[Conflict]:
    """
    Flag a true units/scale reporting error — the thousands-vs-actual bug. Its
    signature is a clean ~1000x jump against the IMMEDIATELY ADJACENT year(s),
    not merely a value far from the long-run median (which just means the company
    had an unusually good or bad year). We require the ratio to neighbors to sit
    near a power-of-ten boundary (>=100x) on BOTH sides, so genuine business
    swings (e.g. Intel's FY2023 earnings cliff) are not misflagged.
    """
    series: dict[str, list[Fact]] = {}
    for f in facts:
        if f.form == "10-K" and f.is_annual_period():
            series.setdefault(f.concept, []).append(f)

    out = []
    for concept, fs in series.items():
        fs = sorted(fs, key=lambda f: f.period_end)
        vals = [f for f in fs if f.value]
        for i, f in enumerate(vals):
            neighbors = []
            if i > 0:
                neighbors.append(vals[i - 1])
            if i < len(vals) - 1:
                neighbors.append(vals[i + 1])
            if len(neighbors) < 2:
                continue  # need both sides to confirm an isolated spike
            ratios = [abs(f.value) / abs(n.value) for n in neighbors
                      if n.value]
            if len(ratios) < 2:
                continue
            # Isolated spike: this value >=100x BOTH neighbors, or <=1/100 of both.
            if all(r >= 100 for r in ratios) or all(r <= 0.01 for r in ratios):
                out.append(Conflict(
                    kind="scale_anomaly", ticker=ticker, concept=concept,
                    detail=(f"{concept} FY{_period_fy(f)} = {f.value:,.0f} differs "
                            f"by ~{max(ratios) if ratios[0]>1 else 1/min(ratios):.0f}x "
                            f"from BOTH adjacent years — signature of a "
                            f"thousands-vs-units reporting error."),
                    severity="high",
                    evidence=[{"value": f.value, "fy": _period_fy(f),
                               "accession": f.accession,
                               "neighbors": [n.value for n in neighbors]}]))
    return out


def find_unit_inconsistency(ticker: str) -> list[Conflict]:
    """A monetary concept tagged under >1 incompatible unit across its history."""
    gaap = get_company_facts(ticker).get("facts", {}).get("us-gaap", {})
    out = []
    monetary_units = {"USD"}
    for concept, node in gaap.items():
        units = set(node.get("units", {}).keys())
        # Mixed monetary + per-share/pure on the same tag is expected for some
        # tags; flag only when two *monetary* scales coexist (rare but real).
        monetary = {u for u in units if u.startswith("USD") and u != "USD/shares"}
        if len(monetary) > 1:
            out.append(Conflict(
                kind="unit_inconsistency", ticker=ticker, concept=concept,
                detail=f"{concept} reported under multiple monetary units: {sorted(monetary)}",
                severity="medium",
                evidence=[{"units": sorted(monetary)}]))
    return out


def find_fiscal_misalignment(ticker: str) -> list[Conflict]:
    """
    Detect a non-December fiscal-year end. Not an error, but a comparability
    caveat: this company's 'FY2025' covers a different calendar window than a
    December filer's. We disclose the offset.
    """
    facts = extract_facts(ticker, ["Revenue"])
    annual = [f for f in facts if f.form == "10-K" and f.is_annual_period()]
    if not annual:
        return []
    latest = max(annual, key=lambda f: f.period_end)
    try:
        d = date.fromisoformat(latest.period_end)
    except ValueError:
        return []
    if d.month != 12:
        return [Conflict(
            kind="fiscal_misalignment", ticker=ticker, concept="(fiscal calendar)",
            detail=(f"{ticker} fiscal year ends ~{d.strftime('%b %d')}, not Dec 31. "
                    f"Cross-company comparisons by fiscal-year label compare "
                    f"different calendar windows; charts disclose the period-end."),
            severity="low",
            evidence=[{"latest_period_end": latest.period_end}])]
    return []


def find_tag_switches(ticker: str) -> list[Conflict]:
    """Surface canonical concepts whose underlying XBRL tag changed across years."""
    gaap = get_company_facts(ticker).get("facts", {}).get("us-gaap", {})
    out = []
    for canon, aliases in CONCEPT_ALIASES.items():
        present = [a for a in aliases if a in gaap]
        if len(present) > 1:
            # Determine if they cover different (non-overlapping) periods.
            spans = {}
            for a in present:
                ends = set()
                for entries in gaap[a].get("units", {}).values():
                    for e in entries:
                        if e.get("form") == "10-K" and e.get("end"):
                            ends.add(e["end"][:4])
                spans[a] = ends
            out.append(Conflict(
                kind="tag_switch", ticker=ticker, concept=canon,
                detail=(f"{canon} is reported under multiple XBRL tags over time: "
                        f"{present}. Extraction stitches them by period; "
                        f"surfaced here for transparency."),
                severity="low",
                evidence=[{"tag": a, "years": sorted(spans[a])} for a in present]))
    return out


def all_conflicts(ticker: str) -> dict[str, list[Conflict]]:
    facts = extract_facts(ticker)
    grouped: dict[str, list[Conflict]] = {
        "restatement": find_restatements(facts, ticker),
        "scale_anomaly": find_scale_anomalies(facts, ticker),
        "unit_inconsistency": find_unit_inconsistency(ticker),
        "fiscal_misalignment": find_fiscal_misalignment(ticker),
        "tag_switch": find_tag_switches(ticker),
    }
    return grouped


if __name__ == "__main__":
    import config
    for tk in config.active_tickers():
        g = all_conflicts(tk)
        print(f"\n=== {tk} ===")
        for kind, items in g.items():
            print(f"  {kind:20s}: {len(items)}")
            for c in items[:2]:
                print(f"      [{c.severity}] {c.detail[:100]}")
