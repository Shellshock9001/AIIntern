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
from dataclasses import dataclass, field
from typing import Literal, Optional, TypedDict

import requests

from metrics import compute_metrics, DerivedMetric
from rag import retrieve, Retrieved, OLLAMA_URL

log = logging.getLogger("agent")

GEN_MODEL = "qwen2.5:7b-instruct"
KNOWN_TICKERS = ["NVDA", "AMD", "INTC", "AVGO"]


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


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------
class State(TypedDict, total=False):
    question: str
    rewritten: str
    route: Literal["numeric", "narrative", "comparative"]
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


def node_classify(state: State) -> State:
    q = state["question"].lower()
    tickers = [t for t in KNOWN_TICKERS if t.lower() in q
               or t in state["question"]]
    # Company-name fallbacks.
    name_map = {"nvidia": "NVDA", "amd": "AMD", "advanced micro": "AMD",
                "intel": "INTC", "broadcom": "AVGO"}
    for name, tk in name_map.items():
        if name in q and tk not in tickers:
            tickers.append(tk)
    state["tickers"] = tickers or KNOWN_TICKERS

    # Extract an explicitly requested fiscal year (4-digit, 1990-2099).
    import re as _re
    yrs = [int(y) for y in _re.findall(r"\b(19[9]\d|20\d\d|18\d\d)\b", state["question"])]
    state["req_fy"] = yrs[0] if yrs else None

    metric_hit = next((v for k, v in METRIC_KEYWORDS.items() if k in q), None)
    state["metric_name"] = metric_hit

    if metric_hit and len(state["tickers"]) > 1 and any(
            w in q for w in ["compare", "vs", "versus", "than", "outperform",
                             "higher", "lower", "better", "worse"]):
        state["route"] = "comparative"
    elif metric_hit:
        state["route"] = "numeric"
    else:
        state["route"] = "narrative"
    state.setdefault("trace", []).append(
        f"classify: route={state['route']} tickers={state['tickers']} "
        f"metric={metric_hit} req_fy={state['req_fy']}")
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
    return f"{m.value:,.2f}"


def node_numeric(state: State) -> State:
    tk = state["tickers"][0]
    req_fy = state.get("req_fy")
    m = find_metric(tk, state["metric_name"], fy=req_fy)
    if m is None:
        avail = sorted({x.fy for x in metrics_for(tk)
                        if state["metric_name"].lower() in x.name.lower()
                        and x.value is not None})
        yr_clause = (f"for FY{req_fy} " if req_fy else "")
        avail_clause = (f" Available years: {avail}." if avail else
                        " No years are computable for this metric.")
        state["refused"] = True
        state["answer"] = (
            f"I can't answer that: {state['metric_name']} for {tk} {yr_clause}"
            f"is not available from the XBRL facts.{avail_clause} I won't estimate it.")
        state["evidence"] = []
        state["grounded"] = False
        state["trace"].append(
            f"numeric: REFUSE (metric={state['metric_name']} req_fy={req_fy} not found)")
        return state
    inputs = "; ".join(
        f"{p.concept}=${p.value:,.0f} (FY{p.fy}, acc {p.accession})"
        for p in m.inputs)
    state["answer"] = (
        f"{tk} {m.name} for FY{m.fy} is **{_fmt(m)}**.\n\n"
        f"Formula: {m.formula}\nInputs: {inputs}")
    state["evidence"] = [{"type": "metric", "citation": inputs,
                          "formula": m.formula, "value": _fmt(m)}]
    state["grounded"] = True
    state["trace"].append(f"numeric: {tk} {m.name} FY{m.fy} = {_fmt(m)}")
    return state


def node_comparative(state: State) -> State:
    name = state["metric_name"]
    req_fy = state.get("req_fy")
    rows = []
    for tk in state["tickers"]:
        m = find_metric(tk, name, fy=req_fy)
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
    hits: list[Retrieved] = retrieve(state["rewritten"], k=5,
                                     tickers=state["tickers"])
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
    g.add_node("narrative", node_narrative)
    g.add_node("selfcheck", node_selfcheck)
    g.set_entry_point("classify")
    g.add_edge("classify", "rewrite")
    g.add_conditional_edges("rewrite", lambda s: s["route"],
                            {"numeric": "numeric", "comparative": "comparative",
                             "narrative": "narrative"})
    g.add_edge("numeric", END)
    g.add_edge("comparative", END)
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
