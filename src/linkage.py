"""
linkage.py — Quant-to-narrative linkage.

The brief asks specifically: "when a metric moves materially, connect it to the
relevant narrative." This module does exactly that:

  1. detect_material_moves() scans the derived-metric time series and flags
     year-over-year swings beyond a per-metric materiality threshold.
  2. link_move_to_narrative() retrieves the MD&A / Risk passage from THAT fiscal
     year's filing that best explains the move, using the RAG index.

The number side is deterministic (from metrics.py / XBRL). The narrative side is
retrieved and cited — the LLM never invents the figure, it only surfaces what
management actually wrote about the change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from metrics import compute_metrics, DerivedMetric


# Per-metric materiality thresholds. Percentage-point swing for margins/growth,
# fractional change for ratios, relative change for dollar amounts.
MATERIALITY = {
    "Gross Margin": 5.0,            # points
    "Operating Margin": 7.0,
    "Net Margin": 7.0,
    "Revenue YoY Growth": 15.0,
    "Debt-to-Equity": 0.25,         # ratio units
    "Free Cash Flow": 0.40,         # 40% relative change
}


@dataclass
class MaterialMove:
    ticker: str
    metric: str
    fy_from: int
    fy_to: int
    value_from: float
    value_to: float
    delta: float
    unit: str
    direction: str                  # "improved" / "deteriorated"
    magnitude_desc: str
    inputs_to: list = field(default_factory=list)   # provenance of the later value
    narrative: Optional[dict] = None  # filled by link_move_to_narrative()

    def headline(self) -> str:
        verb = {"improved": "rose", "deteriorated": "fell"}[self.direction]
        if self.unit == "%":
            return (f"{self.ticker} {self.metric} {verb} from {self.value_from:.1f}% "
                    f"to {self.value_to:.1f}% (FY{self.fy_from}→FY{self.fy_to}, "
                    f"{self.delta:+.1f} pts)")
        if self.unit == "x":
            return (f"{self.ticker} {self.metric} {verb} from {self.value_from:.2f}x "
                    f"to {self.value_to:.2f}x (FY{self.fy_from}→FY{self.fy_to})")
        return (f"{self.ticker} {self.metric} {verb} "
                f"${self.value_from:,.0f}→${self.value_to:,.0f} "
                f"(FY{self.fy_from}→FY{self.fy_to}, {self.delta:+.0%})")


def detect_material_moves(ticker: str,
                          metrics: Optional[list[DerivedMetric]] = None
                          ) -> list[MaterialMove]:
    if metrics is None:
        metrics, _, _ = compute_metrics(ticker)

    # Index latest value per (metric, fy), keeping provenance.
    series: dict[str, list[DerivedMetric]] = {}
    for m in metrics:
        if m.value is None:
            continue
        series.setdefault(m.name, []).append(m)

    moves: list[MaterialMove] = []
    for name, pts in series.items():
        thr = MATERIALITY.get(name)
        if thr is None:
            continue
        pts = sorted(pts, key=lambda m: m.fy)
        for i in range(1, len(pts)):
            a, b = pts[i - 1], pts[i]
            if b.fy - a.fy != 1:
                continue  # only adjacent fiscal years
            unit = b.unit
            if unit in ("%", "x"):
                delta = b.value - a.value
                material = abs(delta) >= thr
            else:  # USD relative change
                if a.value == 0:
                    continue
                delta = (b.value - a.value) / abs(a.value)
                material = abs(delta) >= thr
            if not material:
                continue
            direction = "improved" if (b.value - a.value) >= 0 else "deteriorated"
            # For leverage, "improved" means lower — invert.
            if name == "Debt-to-Equity":
                direction = "deteriorated" if (b.value - a.value) > 0 else "improved"
            moves.append(MaterialMove(
                ticker=ticker, metric=name, fy_from=a.fy, fy_to=b.fy,
                value_from=a.value, value_to=b.value, delta=delta, unit=unit,
                direction=direction,
                magnitude_desc=f"{abs(delta):.1f}{'pts' if unit in ('%','x') else ' rel'}",
                inputs_to=b.inputs,
            ))
    # Biggest moves first.
    moves.sort(key=lambda mv: abs(mv.delta), reverse=True)
    return moves


def link_move_to_narrative(move: MaterialMove, k: int = 3) -> MaterialMove:
    """
    Retrieve the passage from the move's TARGET fiscal year that best explains it.
    Restricts retrieval to the company and prefers MD&A. Requires the RAG index
    (Ollama embeddings) to be built; degrades gracefully if unavailable.
    """
    from rag import retrieve  # local import so metrics-only use needs no Ollama

    metric_terms = {
        "Gross Margin": "gross margin cost of revenue pricing",
        "Operating Margin": "operating expenses operating income margin",
        "Net Margin": "net income tax profitability",
        "Revenue YoY Growth": "revenue increase decrease demand growth drivers",
        "Debt-to-Equity": "debt financing borrowings leverage",
        "Free Cash Flow": "cash flow from operations capital expenditures",
    }
    verb = "increase" if move.direction == "improved" else "decline"
    query = (f"reasons for {verb} in {move.metric} fiscal {move.fy_to} "
             f"{metric_terms.get(move.metric, '')}")
    try:
        hits = retrieve(query, k=k, tickers=[move.ticker])
    except Exception as e:
        move.narrative = {"available": False, "reason": str(e)}
        return move

    # Prefer a hit whose citation references the target fiscal year; else top hit.
    target = None
    for h in hits:
        if str(move.fy_to) in h.citation or str(move.fy_to - 1) in h.citation:
            target = h
            break
    target = target or (hits[0] if hits else None)
    if target is None:
        move.narrative = {"available": False, "reason": "no passages retrieved"}
        return move

    move.narrative = {
        "available": True,
        "citation": target.citation,
        "section": target.section,
        "passage": target.text,
        "candidates": [{"citation": h.citation, "section": h.section,
                        "snippet": h.text[:200]} for h in hits],
    }
    return move


def explain_move_llm(move: MaterialMove) -> str:
    """
    One-sentence grounded explanation tying the verified number to the cited
    passage. The LLM is constrained to the retrieved text. Optional (needs Ollama).
    """
    if not move.narrative or not move.narrative.get("available"):
        return ("No explaining passage found in the indexed filings for this move; "
                "the figure stands on its own (verified from XBRL).")
    from agent import _ollama_chat
    sys = ("You connect a verified financial metric change to what management wrote. "
           "Use ONLY the passage. One sentence. If the passage doesn't explain the "
           "change, say so. Never restate the number as if you computed it.")
    user = (f"Verified change: {move.headline()}\n\n"
            f"Passage ({move.narrative['citation']}):\n{move.narrative['passage']}\n\n"
            f"One-sentence linkage:")
    try:
        return _ollama_chat(sys, user).strip()
    except RuntimeError as e:
        return f"(LLM explanation unavailable: {e})"


def top_moves_with_links(ticker: str, n: int = 5) -> list[MaterialMove]:
    moves = detect_material_moves(ticker)[:n]
    return [link_move_to_narrative(m) for m in moves]


if __name__ == "__main__":
    # Detection works without Ollama; linkage needs the RAG index.
    import config
    for tk in config.active_tickers()[:2]:
        print(f"\n=== {tk}: top material moves ===")
        for mv in detect_material_moves(tk)[:5]:
            print(" ", mv.headline())
