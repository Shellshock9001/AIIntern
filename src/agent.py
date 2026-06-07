"""
agent.py — Agentic Q&A over the filing corpus (LangGraph).

Graph:
  classify -> rewrite -> route ->
      numeric   : answer from the metrics engine (XBRL ground truth)
      narrative : RAG retrieval over filing text
      comparative: metrics across companies
  -> self_check (is the answer grounded in retrieved evidence?) -> finalize|refuse

Two anti-hallucination guarantees:
  1. Numeric answers are pulled from metrics.py — the LLM only phrases them, never
     produces the digits. If the metric isn't computable, we refuse.
  2. Narrative answers must pass a grounding self-check: the LLM is asked whether
     the retrieved passages actually support an answer. If not -> refuse.

Generation model: qwen2.5:7b-instruct via Ollama (local, $0). Temperature 0 for
determinism / reproducible eval.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal, Optional, TypedDict

import requests

from metrics import (compute_metrics, DerivedMetric, get_raw_value,
                     available_years, capability_catalog, RAW_CONCEPT_LABELS)
from rag import retrieve, Retrieved, OLLAMA_URL

log = logging.getLogger("agent")

GEN_MODEL = "qwen2.5:7b-instruct"

# Canonical quantity vocabulary the intent-parser maps onto. Union of derived
# metric names and raw concept labels — this is the full answerable surface.
DERIVED_NAMES = ["Gross Margin", "Operating Margin", "Net Margin", "Debt-to-Equity",
                 "Free Cash Flow", "Revenue YoY Growth", "Revenue CAGR",
                 "Current Ratio", "SG&A Intensity", "R&D Intensity", "Asset Turnover",
                 "Effective Tax Rate", "Capital Returned / OCF", "Return on Assets",
                 "Return on Equity"]
RAW_NAMES = list(RAW_CONCEPT_LABELS.keys())


def _ollama_chat(system: str, user: str, model: str = GEN_MODEL,
                 json_mode: bool = False, temperature: float = 0.0) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": temperature},
    }
    if json_mode:
        payload["format"] = "json"
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=180)
        r.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_URL}. Start Ollama and run "
            f"`ollama pull {model}`."
        ) from e
    return r.json()["message"]["content"]


# ---------------------------------------------------------------------------
# Metric cache (computed once per session per ticker)
# ---------------------------------------------------------------------------
_METRIC_CACHE: dict[str, list[DerivedMetric]] = {}


def metrics_for(ticker: str) -> list[DerivedMetric]:
    if ticker not in _METRIC_CACHE:
        m, _, _ = compute_metrics(ticker)
        _METRIC_CACHE[ticker] = m
    return _METRIC_CACHE[ticker]


def find_metric(ticker: str, name_sub: str,
                fy: Optional[int] = None) -> Optional[DerivedMetric]:
    cands = [m for m in metrics_for(ticker)
             if name_sub.lower() in m.name.lower() and m.value is not None]
    if fy is not None:
        cands = [m for m in cands if m.fy == fy]
    if not cands:
        return None
    return max(cands, key=lambda m: m.fy)  # latest by default


def resolve_quantity(ticker: str, quantity: str,
                     fy: Optional[int] = None) -> Optional[DerivedMetric]:
    """
    Unified lookup: `quantity` may be a derived-metric name ("Gross Margin") or a
    raw XBRL concept ("Revenue", "NetIncome"). Returns a DerivedMetric (carrying
    provenance) for the requested year or latest. Exact matches win over substring
    so "Revenue" resolves to the raw line item, not "Revenue YoY Growth".
    """
    q = quantity.strip()
    # 1) Exact derived-metric name match (case-insensitive).
    exact = [m for m in metrics_for(ticker)
             if m.name.lower() == q.lower() and m.value is not None]
    if exact:
        if fy is not None:
            exact = [m for m in exact if m.fy == fy]
        if exact:
            return max(exact, key=lambda m: m.fy)
    # 2) Exact raw-concept match (by key or by human label).
    key = next((c for c in RAW_CONCEPT_LABELS if c.lower() == q.lower()), None)
    if key is None:
        key = next((c for c, (lbl, _) in RAW_CONCEPT_LABELS.items()
                    if lbl.lower() == q.lower()), None)
    if key:
        rv = get_raw_value(ticker, key, fy=fy)
        if rv:
            return rv
    # 3) Fall back to fuzzy substring derived-metric match.
    return find_metric(ticker, q, fy=fy)


def quantity_years(ticker: str, quantity: str) -> list[int]:
    return available_years(ticker, quantity)


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------
class State(TypedDict, total=False):
    question: str
    rewritten: str
    route: Literal["numeric", "narrative", "comparative", "causal", "trend"]
    tickers: list[str]
    metric_name: Optional[str]
    req_fy: Optional[int]
    evidence: list[dict]      # serialized provenance or retrieved chunks
    answer: str
    grounded: bool
    refused: bool
    trace: list[str]


METRIC_KEYWORDS = {
    "gross margin": "Gross Margin", "operating margin": "Operating Margin",
    "net margin": "Net Margin", "debt": "Debt-to-Equity",
    "leverage": "Debt-to-Equity", "free cash flow": "Free Cash Flow",
    "fcf": "Free Cash Flow", "revenue growth": "Revenue YoY Growth",
    "yoy": "Revenue YoY Growth", "cagr": "Revenue CAGR",
}


def _detect_tickers(question: str) -> list[str]:
    """
    Resolve company references dynamically against SEC's full ticker map — any of
    ~10,000 filers, not a fixed list. Matches explicit tickers (uppercase tokens)
    and company-name substrings. Falls back to the active universe if none found.
    """
    import re
    from sec_client import load_ticker_map
    import config

    tmap = load_ticker_map()  # TICKER -> {cik, title}
    found: list[str] = []

    # 1a. Tickers from the ACTIVE universe, matched case-insensitively as whole
    # words (so "amd" or "AMD" both work without matching random short words).
    active = config.active_tickers()
    for tk in active:
        if re.search(rf"\b{re.escape(tk)}\b", question, re.IGNORECASE):
            if tk not in found:
                found.append(tk)
    # 1b. Explicit UPPERCASE ticker tokens anywhere in the SEC map (out-of-universe
    # companies like TSLA, WMT — uppercase only, to avoid lowercase false hits).
    for tok in re.findall(r"\b[A-Z]{1,5}\b", question):
        if tok in tmap and tok not in found:
            found.append(tok)

    # 2. Company-name match (e.g. "walmart", "jpmorgan", possessive "nvidia's").
    ql = question.lower()
    if not found:
        # Tokens >=4 chars; also strip trailing 's to catch possessives.
        raw_words = re.findall(r"[a-z]{4,}", ql)
        q_words = set(raw_words) | {w.rstrip("s") for w in raw_words}
        skip = {"first", "general", "american", "national", "united", "global",
                "group", "the", "new", "great", "international", "compare",
                "between", "margin", "revenue", "growth", "company", "their",
                "latest", "fiscal", "ratio", "income", "asset", "equity"}
        claimed_words: set[str] = set()
        for tk, info in tmap.items():
            name = re.sub(r"[^a-z ]", "", info["title"].lower())
            words = name.split()
            if not words:
                continue
            first = words[0]
            if (first in q_words and first not in skip
                    and first not in claimed_words):
                found.append(tk)
                claimed_words.add(first)
            if len(found) >= 5:
                break

    return found or config.active_tickers()


def _fuzzy_quantity(name: Optional[str]) -> Optional[str]:
    """Map a free-text quantity to a real catalog entry (derived name or raw key)."""
    if not name:
        return None
    catalog = list(DERIVED_NAMES) + list(RAW_NAMES)
    n = name.strip().lower().rstrip("s")  # tolerate plurals
    # exact (case-insensitive, plural-tolerant)
    for c in catalog:
        if c.lower().rstrip("s") == n:
            return c
    # label match for raw concepts
    for key, (lbl, _) in RAW_CONCEPT_LABELS.items():
        if lbl.lower().rstrip("s") == n:
            return key
    # substring either direction
    for c in catalog:
        cl = c.lower()
        if n in cl or cl in n:
            return c
    # common abbreviations
    abbr = {"gm": "Gross Margin", "om": "Operating Margin", "nm": "Net Margin",
            "d/e": "Debt-to-Equity", "fcf": "Free Cash Flow",
            "roe": "Return on Equity", "roa": "Return on Assets",
            "capex": "CapEx", "opex": "OperatingIncome", "rev": "Revenue",
            "ni": "NetIncome", "eps": "EPS_Diluted"}
    return abbr.get(n)


def _parse_intent_llm(question: str, tickers: list[str]) -> dict:
    """
    Map ANY question to a structured plan against the real data catalog. The LLM
    does understanding; execution stays deterministic. Robust by design:
      - the LLM's chosen quantity is VALIDATED against the catalog (fuzzy-matched),
      - the heuristic runs as a backstop and fills any gaps,
      - falls back entirely to the heuristic if the LLM is unavailable.
    """
    heur = _heuristic_intent(question)
    quantities = sorted(set(DERIVED_NAMES) | set(RAW_NAMES))
    sys = (
        "You translate a finance question into a JSON plan for a system that has "
        "EXACT figures for US public companies from SEC XBRL filings.\n"
        "Allowed quantities (use the EXACT string, or null):\n"
        f"{', '.join(quantities)}\n\n"
        "JSON keys:\n"
        '  route: "lookup" (one figure), "compare" (rank/contrast 2+ companies), '
        '"trend" (change across years), "causal" (why a metric moved), '
        '"narrative" (qualitative text question about risks, strategy, etc.).\n'
        '  quantity: the single best match from the list above, or null.\n'
        "  fy: 4-digit fiscal year if specified (treat 'FY2024','fiscal 2024','2024' "
        "the same), else null.\n"
        '  operation: "value"|"rank"|"trend"|"explain"|null.\n'
        "Synonyms: revenue/sales/top line->Revenue; profit/earnings/bottom line/"
        "net income->NetIncome; margins->the specific margin; leverage/gearing->"
        "Debt-to-Equity; liquidity->Current Ratio; FCF/free cash flow->Free Cash Flow; "
        "ROE->Return on Equity; ROA->Return on Assets; R&D spend->ResearchDev; "
        "buybacks/repurchases->StockBuyback; cash->CashAndEquivalents.\n"
        "Things NOT in the data (headcount, employees, stock price, guidance, "
        "future/forecast years, earnings-call quotes): set quantity=null. If the "
        "question is about risks/strategy/competition/why-language without a metric, "
        "route='narrative'. Output ONLY the JSON object, no prose."
    )
    user = f"Question: {question}\nCompanies detected: {tickers}"
    try:
        raw = _ollama_chat(sys, user, json_mode=True)
        plan = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError):
        return heur

    plan.setdefault("route", heur["route"])
    plan.setdefault("operation", heur["operation"])
    # Validate quantity against the real catalog; if the LLM hallucinated a name,
    # fuzzy-map it, and if that fails, fall back to the heuristic's pick.
    q = _fuzzy_quantity(plan.get("quantity"))
    if q is None:
        q = heur["quantity"]
    plan["quantity"] = q
    # Year: trust LLM if it gave a sane year, else heuristic.
    fy = plan.get("fy")
    if not (isinstance(fy, int) and 1800 <= fy <= 2099):
        fy = heur["fy"]
    plan["fy"] = fy
    # If we have no quantity at all, it's narrative (unless heuristic said causal).
    if plan["quantity"] is None and plan["route"] not in ("narrative", "causal"):
        plan["route"] = "narrative"
    return plan


def _heuristic_intent(question: str) -> dict:
    """Offline fallback: keyword map so numeric/compare works without Ollama.
    Multi-word/more-specific keys are checked BEFORE generic ones so
    'revenue growth' doesn't get captured by 'revenue'."""
    q = question.lower()
    # Ordered: most specific first.
    syn = [
        ("revenue yoy growth", "Revenue YoY Growth"), ("revenue growth", "Revenue YoY Growth"),
        ("revenue cagr", "Revenue CAGR"), ("yoy growth", "Revenue YoY Growth"),
        ("cagr", "Revenue CAGR"), ("gross margin", "Gross Margin"),
        ("operating margin", "Operating Margin"), ("net margin", "Net Margin"),
        ("gross profit", "GrossProfit"), ("operating income", "OperatingIncome"),
        ("net income", "NetIncome"), ("debt-to-equity", "Debt-to-Equity"),
        ("debt to equity", "Debt-to-Equity"), ("leverage", "Debt-to-Equity"),
        ("free cash flow", "Free Cash Flow"), ("fcf", "Free Cash Flow"),
        ("current ratio", "Current Ratio"), ("return on equity", "Return on Equity"),
        ("roe", "Return on Equity"), ("return on assets", "Return on Assets"),
        ("roa", "Return on Assets"), ("asset turnover", "Asset Turnover"),
        ("r&d intensity", "R&D Intensity"), ("sg&a", "SGA"),
        ("effective tax", "Effective Tax Rate"), ("tax rate", "Effective Tax Rate"),
        ("eps", "EPS_Diluted"), ("inventory", "Inventory"),
        ("total assets", "TotalAssets"), ("cash", "CashAndEquivalents"),
        ("dividend", "Dividends"), ("buyback", "StockBuyback"),
        ("r&d", "ResearchDev"), ("research", "ResearchDev"),
        ("revenue", "Revenue"), ("sales", "Revenue"),
        ("profit", "NetIncome"), ("earnings", "NetIncome"),
    ]
    # Match keys; short alpha keys (<=3 chars) need word boundaries so 'roa'
    # doesn't match inside 'broadcom'.
    def _hit(key: str) -> bool:
        if len(key) <= 3 and key.isalpha():
            return re.search(rf"\b{re.escape(key)}\b", q) is not None
        return key in q
    quantity = next((v for k, v in syn if _hit(k)), None)
    # Colloquial fallbacks if nothing matched yet.
    if quantity is None:
        if any(w in q for w in ["make", "made", "money", "bottom line", "profitab"]):
            quantity = "NetIncome"
        elif any(w in q for w in ["top line", "how big", "size"]):
            quantity = "Revenue"
        elif any(w in q for w in ["levered", "leveraged", "gearing", "indebted"]):
            quantity = "Debt-to-Equity"
        elif "cash position" in q or "how much cash" in q:
            quantity = "CashAndEquivalents"
    yrs = re.findall(r"(?:fy|fiscal)?\s*(18\d\d|19\d\d|20\d\d)", question.lower())
    fy = int(yrs[0]) if yrs else None
    if any(w in q for w in ["why", "what caused", "what drove", "reason"]):
        route, op = "causal", "explain"
    elif any(w in q for w in ["compare", "vs", "versus", "rank", "highest", "lowest",
                              "than", "outperform", "better", "worse", "which company"]):
        route, op = "compare", "rank"
    elif any(w in q for w in ["trend", "over the past", "improved", "improving",
                              "declined", "history", "over time"]):
        route, op = "trend", "trend"
    elif quantity:
        route, op = "lookup", "value"
    else:
        route, op = "narrative", None
    return {"route": route, "quantity": quantity, "fy": fy, "operation": op}


def _heuristic_is_confident(question: str, plan: dict) -> bool:
    """The heuristic is trustworthy when it found a concrete quantity AND the
    phrasing isn't the kind that needs real language understanding. In that case
    we skip the LLM entirely for a near-instant answer."""
    if not plan.get("quantity"):
        return False
    q = question.lower()
    # Causal / open-ended phrasing benefits from the LLM; don't shortcut it.
    ambiguous = any(w in q for w in ["why", "explain", "compare", "how does",
                                     "what about", "tell me about", "relative"])
    return not ambiguous


def node_classify(state: State) -> State:
    import config
    state["tickers"] = _detect_tickers(state["question"])

    # FAST PATH: try the cheap heuristic first. If it's confident (found a clear
    # metric and the question isn't open-ended), use it and skip the LLM — this is
    # what makes revenue/margin/ranking questions answer instantly. Only fall back
    # to the LLM intent-parser for ambiguous or narrative phrasing.
    heur = _heuristic_intent(state["question"])
    if _heuristic_is_confident(state["question"], heur):
        plan = heur
        state["trace"] = state.get("trace", []) + ["classify: fast path (heuristic)"]
    else:
        plan = _parse_intent_llm(state["question"], state["tickers"])
        state["trace"] = state.get("trace", []) + ["classify: LLM intent-parse"]

    state["metric_name"] = plan.get("quantity")
    state["req_fy"] = plan.get("fy")

    r = plan.get("route", "narrative")
    if r == "narrative" or not state["metric_name"]:
        state["route"] = "narrative"
    elif r == "causal":
        state["route"] = "causal"
    elif r == "compare" or (r == "lookup" and len(state["tickers"]) > 1):
        state["route"] = "comparative"
    elif r == "trend":
        state["route"] = "trend"
    else:
        state["route"] = "numeric"
    state["trace"].append(
        f"classify: intent={r} -> route={state['route']} "
        f"quantity={state['metric_name']} fy={state['req_fy']} tickers={state['tickers']}")
    return state


def node_rewrite(state: State) -> State:
    """Lightweight query expansion for retrieval (narrative route only)."""
    if state["route"] != "narrative":
        state["rewritten"] = state["question"]
        return state
    sys = ("Rewrite the user's question into a concise search query for retrieving "
           "passages from SEC 10-K/10-Q filings. Output only the query text.")
    try:
        rq = _ollama_chat(sys, state["question"]).strip().strip('"')
        state["rewritten"] = rq or state["question"]
    except RuntimeError:
        state["rewritten"] = state["question"]
    state["trace"].append(f"rewrite: {state['rewritten']!r}")
    return state


def _fmt(m: DerivedMetric) -> str:
    if m.unit == "USD":
        return f"${m.value:,.0f}"
    if m.unit == "%":
        return f"{m.value:.2f}%"
    if m.unit == "x":
        return f"{m.value:.2f}x"
    if m.unit == "USD/sh":
        return f"${m.value:.2f}/sh"
    if m.unit == "shares":
        return f"{m.value:,.0f} shares"
    return f"{m.value:,.2f}"


def node_numeric(state: State) -> State:
    tk = state["tickers"][0]
    req_fy = state.get("req_fy")
    m = resolve_quantity(tk, state["metric_name"], fy=req_fy)
    if m is None:
        avail = quantity_years(tk, state["metric_name"])
        yr_clause = (f"for FY{req_fy} " if req_fy else "")
        avail_clause = (f" Available years: {avail}." if avail else
                        " This quantity isn't reported by this company.")
        state["refused"] = True
        state["answer"] = (
            f"I can't answer that: {state['metric_name']} for {tk} {yr_clause}"
            f"is not available from the XBRL facts.{avail_clause} I won't estimate it.")
        state["evidence"] = []
        state["grounded"] = False
        state["trace"].append(
            f"numeric: REFUSE (quantity={state['metric_name']} req_fy={req_fy} not found)")
        return state
    inputs = "; ".join(
        f"{p.concept}={p.value:,.0f} (FY{p.fy}, acc {p.accession})"
        for p in m.inputs)
    state["answer"] = (
        f"{tk} {m.name} for FY{m.fy} is **{_fmt(m)}**.\n\n"
        f"Formula: {m.formula}\nInputs: {inputs}")
    state["evidence"] = [{"type": "metric", "citation": inputs,
                          "formula": m.formula, "value": _fmt(m)}]
    state["grounded"] = True
    state["trace"].append(f"numeric: {tk} {m.name} FY{m.fy} = {_fmt(m)}")
    return state


def node_trend(state: State) -> State:
    """Answer 'has X improved over N years / over time' deterministically."""
    tk = state["tickers"][0]
    q = state["metric_name"]
    metrics, facts, _ = compute_metrics(tk)
    # Gather the series (derived or raw).
    series = sorted(((m.fy, m.value, m) for m in metrics
                     if q.lower() in m.name.lower() and m.value is not None),
                    key=lambda t: t[0])
    if not series:
        # raw concept series
        from metrics import get_raw_value
        yrs = quantity_years(tk, q)
        series = []
        for y in yrs:
            rv = get_raw_value(tk, q, fy=y)
            if rv:
                series.append((y, rv.value, rv))
    if len(series) < 2:
        state["refused"] = True
        state["grounded"] = False
        state["answer"] = (f"Not enough history to assess a trend in {q} for {tk}.")
        state["evidence"] = []
        state["trace"].append("trend: insufficient history -> refuse")
        return state
    # Optional window from the question ("3 years").
    import re
    win = re.search(r"(\d+)\s*year", state["question"].lower())
    pts = series[-(int(win.group(1)) + 1):] if win else series
    first, last = pts[0], pts[-1]
    direction = "improved" if last[1] >= first[1] else "declined"
    unit = last[2].unit
    fv = (lambda v: f"${v:,.0f}" if unit == "USD" else
          (f"{v:.2f}%" if unit == "%" else f"{v:.2f}{unit}"))
    line = " → ".join(f"FY{y}: {fv(v)}" for y, v, _ in pts)
    state["answer"] = (
        f"{tk} {last[2].name} has **{direction}** over FY{first[0]}–FY{last[0]}: "
        f"{line}.\n\n(Net change: {fv(last[1]-first[1])} from FY{first[0]} to FY{last[0]}.)")
    state["evidence"] = [{"type": "metric", "value": fv(v),
                          "formula": m.formula,
                          "citation": "; ".join(f"{p.concept} acc {p.accession}"
                                                for p in m.inputs)}
                         for y, v, m in pts]
    state["grounded"] = True
    state["trace"].append(f"trend: {tk} {q} {direction} over {len(pts)} points")
    return state


def node_comparative(state: State) -> State:
    name = state["metric_name"]
    req_fy = state.get("req_fy")
    rows = []
    for tk in state["tickers"]:
        m = resolve_quantity(tk, name, fy=req_fy)
        if m:
            rows.append((tk, m))
    if not rows:
        state["refused"] = True
        state["answer"] = (f"Can't compare {name}: not computable for any of "
                           f"{state['tickers']}.")
        state["grounded"] = False
        state["evidence"] = []
        state["trace"].append("comparative: REFUSE")
        return state
    rows.sort(key=lambda r: r[1].value, reverse=True)
    lines = [f"{tk}: {_fmt(m)} (FY{m.fy})" for tk, m in rows]
    leader = rows[0]
    state["answer"] = (
        f"Comparison of {name} (latest available fiscal year per company):\n\n"
        + "\n".join(lines)
        + f"\n\nLeader: {leader[0]} at {_fmt(leader[1])}.")
    state["evidence"] = [{
        "type": "metric", "value": _fmt(m), "formula": m.formula,
        "citation": "; ".join(f"{p.concept} acc {p.accession}" for p in m.inputs),
    } for _, m in rows]
    state["grounded"] = True
    state["trace"].append(f"comparative: {name} leader={leader[0]}")
    return state


def node_narrative(state: State) -> State:
    try:
        hits: list[Retrieved] = retrieve(state["rewritten"], k=5,
                                         tickers=state["tickers"])
    except RuntimeError as e:
        state["refused"] = True
        state["grounded"] = False
        state["answer"] = (
            "This looks like a qualitative question that needs the narrative index "
            "(filing text search), which requires Ollama to be running. The numeric "
            "questions work without it. Details: " + str(e))
        state["evidence"] = []
        state["trace"].append("narrative: retrieval unavailable (Ollama down)")
        return state
    if not hits:
        state["refused"] = True
        state["answer"] = "No relevant passages found in the indexed filings."
        state["evidence"] = []
        state["grounded"] = False
        state["trace"].append("narrative: no hits -> REFUSE")
        return state

    context = "\n\n".join(
        f"[{i+1}] ({h.citation})\n{h.text}" for i, h in enumerate(hits))
    sys = (
        "You answer questions about SEC filings using ONLY the numbered passages "
        "provided. Cite passages inline as [1], [2]. If the passages do not "
        "contain enough information to answer, reply EXACTLY: INSUFFICIENT_EVIDENCE. "
        "Never use outside knowledge. Never invent figures.")
    user = f"Passages:\n{context}\n\nQuestion: {state['question']}\n\nAnswer:"
    ans = _ollama_chat(sys, user).strip()
    state["evidence"] = [{"type": "chunk", "citation": h.citation,
                          "section": h.section, "distance": h.distance,
                          "text": h.text[:400]} for h in hits]

    if "INSUFFICIENT_EVIDENCE" in ans:
        state["refused"] = True
        state["grounded"] = False
        state["answer"] = ("The indexed filings don't contain enough information "
                           "to answer that. I won't speculate.")
        state["trace"].append("narrative: model returned INSUFFICIENT_EVIDENCE")
        return state
    state["answer"] = ans
    state["grounded"] = True  # provisional; node_selfcheck verifies
    state["trace"].append(f"narrative: answered from {len(hits)} passages")
    return state


def node_causal(state: State) -> State:
    """
    'Why did <metric> change?' — find the largest material move in that metric for
    the company and link it to the explaining filing passage. Number from XBRL,
    explanation retrieved + cited.
    """
    from linkage import detect_material_moves, link_move_to_narrative, explain_move_llm
    tk = state["tickers"][0]
    name = state["metric_name"]
    req_fy = state.get("req_fy")
    moves = [m for m in detect_material_moves(tk)
             if name.lower() in m.metric.lower()]
    if req_fy:
        moves = [m for m in moves if m.fy_to == req_fy]
    if not moves:
        state["refused"] = True
        state["grounded"] = False
        state["answer"] = (
            f"No material year-over-year move in {name} for {tk}"
            + (f" in FY{req_fy}" if req_fy else "")
            + " met the materiality threshold, so there's nothing notable to explain.")
        state["evidence"] = []
        state["trace"].append("causal: no material move -> refuse")
        return state
    mv = link_move_to_narrative(moves[0])
    explanation = explain_move_llm(mv)
    cite = (mv.narrative or {}).get("citation", "n/a")
    state["answer"] = (f"{mv.headline()}.\n\n**What management said:** {explanation}\n\n"
                       f"**Source:** {cite}")
    state["evidence"] = [{
        "type": "metric",
        "value": f"{mv.value_to:.2f}{mv.unit}",
        "formula": f"Δ {mv.metric} FY{mv.fy_from}->FY{mv.fy_to}",
        "citation": "; ".join(f"{p.concept} acc {p.accession}" for p in mv.inputs_to),
    }]
    if mv.narrative and mv.narrative.get("available"):
        state["evidence"].append({
            "type": "chunk", "section": mv.narrative["section"],
            "citation": mv.narrative["citation"],
            "text": mv.narrative["passage"][:400]})
    state["grounded"] = True
    state["trace"].append(f"causal: linked {mv.metric} FY{mv.fy_to} move to narrative")
    return state


def node_selfcheck(state: State) -> State:
    """Verification pass for narrative answers: is the answer supported?"""
    if state["route"] != "narrative" or state.get("refused"):
        return state
    evidence = "\n\n".join(e["text"] for e in state["evidence"]
                           if e.get("type") == "chunk")
    sys = ("You are a strict fact-checker. Given EVIDENCE and an ANSWER, decide if "
           "every claim in the answer is supported by the evidence. Respond in JSON: "
           '{"supported": true/false, "reason": "..."}')
    user = f"EVIDENCE:\n{evidence}\n\nANSWER:\n{state['answer']}"
    try:
        verdict = json.loads(_ollama_chat(sys, user, json_mode=True))
    except (json.JSONDecodeError, RuntimeError):
        verdict = {"supported": True, "reason": "self-check unavailable"}
    if not verdict.get("supported", True):
        state["refused"] = True
        state["grounded"] = False
        state["answer"] = (
            "I drafted an answer but it failed my grounding self-check against the "
            "filing text, so I'm withholding it rather than risk a hallucination. "
            f"(reason: {verdict.get('reason','')})")
        state["trace"].append(f"selfcheck: FAILED -> refuse ({verdict.get('reason')})")
    else:
        state["trace"].append("selfcheck: passed")
    return state


def build_graph():
    from langgraph.graph import StateGraph, END
    g = StateGraph(State)
    g.add_node("classify", node_classify)
    g.add_node("rewrite", node_rewrite)
    g.add_node("numeric", node_numeric)
    g.add_node("comparative", node_comparative)
    g.add_node("trend", node_trend)
    g.add_node("narrative", node_narrative)
    g.add_node("causal", node_causal)
    g.add_node("selfcheck", node_selfcheck)
    g.set_entry_point("classify")
    g.add_edge("classify", "rewrite")
    g.add_conditional_edges("rewrite", lambda s: s["route"],
                            {"numeric": "numeric", "comparative": "comparative",
                             "trend": "trend", "narrative": "narrative",
                             "causal": "causal"})
    g.add_edge("numeric", END)
    g.add_edge("comparative", END)
    g.add_edge("trend", END)
    g.add_edge("causal", END)
    g.add_edge("narrative", "selfcheck")
    g.add_edge("selfcheck", END)
    return g.compile()


_GRAPH = None


def ask(question: str) -> State:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    init: State = {"question": question, "trace": [], "refused": False}
    return _GRAPH.invoke(init)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for q in [
        "What was NVDA's gross margin?",
        "Compare operating margin between NVDA and INTC.",
        "What does Intel say about competition risk?",
        "What was NVDA's gross margin in 1850?",  # should refuse
    ]:
        print(f"\nQ: {q}")
        out = ask(q)
        print("A:", out["answer"][:400])
        print("refused:", out.get("refused"), "| trace:", out["trace"])
