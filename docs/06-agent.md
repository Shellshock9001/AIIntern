# 06 — `agent.py`: The Agentic Layer (LangGraph)

The agent decides *how* to answer a question and guarantees it either grounds the answer
or refuses. It's a LangGraph state machine — explicit nodes, inspectable, with the
reasoning trace surfaced in the UI.

The key design point: **routing is done by an LLM intent-parser over the real data
catalog, not by keyword matching.** The LLM does the *understanding* (what quantity,
which companies, which year, what operation); execution stays fully deterministic from
XBRL. This is what lets the system answer almost any phrasing instead of a fixed list.

## The graph

```
                 ┌──────────┐
   question ───► │ classify │  LLM intent-parse → {route, quantity, fy, operation}
                 └────┬─────┘  + dynamic ticker detection
                      ▼
                 ┌──────────┐
                 │ rewrite  │  (narrative only) expand into a retrieval query
                 └────┬─────┘
     ┌──────────┬─────┼──────────┬──────────────┬───────────────┐
     ▼          ▼     ▼          ▼              ▼               ▼
 ┌────────┐ ┌────────────┐ ┌───────┐    ┌──────────┐    ┌──────────┐
 │numeric │ │comparative │ │ trend │    │  causal  │    │narrative │
 └───┬────┘ └─────┬──────┘ └───┬───┘    └────┬─────┘    └────┬─────┘
     │            │            │             │               ▼
     │            │            │             │          ┌──────────┐
     │            │            │             │          │selfcheck │
     │            │            │             │          └────┬─────┘
     ▼            ▼            ▼             ▼               ▼
                          END  (answer or refusal)
```

## The answerable surface

The agent can answer about **44 distinct quantities per company**: 15 derived metrics
(margins, ROE/ROA, current ratio, FCF, asset turnover, etc.) plus 29 raw XBRL line items
(revenue, net income, inventory, EPS, cash, R&D, …), each per fiscal year. The catalog is
discovered per company, so a bank and a chipmaker expose different sets.

## Intent parsing: heuristic-first, LLM-fallback

Routing uses a **fast path** for speed. On each question:

1. `_heuristic_intent` runs first (pure Python, instant) — a keyword/synonym map
   with word-boundary matching and colloquial fallbacks.
2. `_heuristic_is_confident` checks whether the heuristic found a concrete quantity
   **and** the phrasing isn't open-ended (no "why/explain/compare/relative").
3. If confident → use the heuristic and **skip the LLM entirely**. This is what
   makes the common questions (revenue, margins, rankings, specific-year lookups)
   answer in well under a second.
4. If not confident (ambiguous or narrative phrasing) → fall back to
   `_parse_intent_llm`, which asks qwen2.5 (JSON mode) to map the question onto the
   catalog, validated and fuzzy-matched against real quantities.

The LLM is reserved for questions that genuinely need language understanding;
everything deterministic stays deterministic *and* fast.

`_parse_intent_llm(question, tickers)` builds a system prompt listing the allowed
quantities and asks the LLM (qwen2.5, JSON mode) to return:

```json
{"route": "lookup|compare|trend|causal|narrative",
 "quantity": "<exact catalog name or null>",
 "fy": 2024, "operation": "value|rank|trend|explain|null"}
```

Robustness is layered:
1. The LLM's `quantity` is **validated against the real catalog** and fuzzy-matched
   (`_fuzzy_quantity` handles plurals, abbreviations like ROE/FCF/GM, substrings).
2. `_heuristic_intent` runs as a backstop — a keyword/synonym map with word-boundary
   matching (so "roa" doesn't match inside "broadcom") and colloquial fallbacks
   ("how much money did X make" → NetIncome, "top line" → Revenue, "how levered" →
   Debt-to-Equity).
3. If Ollama is unavailable, the heuristic alone runs so numeric questions still work.

This two-layer design means the system degrades gracefully rather than failing.

## Dynamic ticker detection: `_detect_tickers`

Resolves company references against SEC's full live ticker map (~10,000 filers):
- active-universe tickers matched case-insensitively as whole words ("amd", "AMD"),
- explicit uppercase tickers anywhere (out-of-universe like TSLA, WMT),
- company-name matching tolerant of possessives ("nvidia's", "intels").
Falls back to the active universe only if nothing matches.

## State


```python
class State(TypedDict, total=False):
    question, rewritten, route, tickers, metric_name, req_fy
    evidence, answer, grounded, refused, trace
```

`trace` accumulates a human-readable log of every decision — shown in the UI's
"Reasoning trace" expander.

## Node-by-node

### `classify`
- **Ticker detection** (`_detect_tickers`): see above — dynamic, case-insensitive,
  possessive-tolerant against the live SEC map.
- **Intent parse** (`_parse_intent_llm`): the LLM returns `{route, quantity, fy,
  operation}`, validated against the catalog with a heuristic backstop.
- **Routing**: maps the intent route to a graph node. `lookup` with one company →
  `numeric`; `lookup`/`compare` with ≥2 companies → `comparative`; `trend` → `trend`;
  `causal` → `causal`; anything without a recognized quantity → `narrative`.

### `rewrite`
Narrative-only. Uses the LLM to turn a question into a tighter retrieval query. Falls
back to the original question if Ollama is unavailable.

### `numeric` (deterministic — no hallucination possible)
Calls `resolve_quantity(ticker, quantity, fy)`, which returns either a derived
`DerivedMetric` (exact-name match wins over substring, so "Revenue" ≠ "Revenue YoY
Growth") **or** a raw XBRL line item via `get_raw_value`. If a requested year isn't
available → **refuse**, listing available years. Otherwise returns the value with its
formula and the accession of every input. The LLM never produces the number.

### `comparative` (deterministic)
Resolves the quantity for each detected ticker, ranks them, names the leader. Leverage
ranks ascending (lower is better); everything else descending. Every figure carries its
accession.

### `trend` (deterministic)
Answers "has X improved over N years / over time". Builds the annual series for the
quantity, optionally windows it to the requested number of years, and reports the
direction plus the year-by-year path. Each point carries provenance. Refuses if there
isn't enough history.

### `causal` (deterministic move + cited prose)
Finds the largest material move in the metric (via `linkage.py`), retrieves the MD&A
passage explaining it, and asks the LLM for a one-sentence linkage constrained to that
passage. The number is verified; the explanation is cited. If no material move →
refuse ("nothing notable to explain").

### `narrative` (retrieved + self-checked)
Retrieves top-k passages (ticker-filtered), then asks the LLM to answer **only** from
them, citing `[1]`,`[2]`. If the model returns `INSUFFICIENT_EVIDENCE` → refuse.

### `selfcheck` (the anti-hallucination gate)
A second LLM pass acts as a strict fact-checker: given the evidence and the drafted
answer, is every claim supported? Returns JSON `{supported, reason}`. If not supported →
the answer is **withheld** and replaced with a refusal noting it failed grounding. This
is the verification step the brief asks for.

## The refusal philosophy

Refusal is a feature, not a failure. The agent refuses when:
- a requested metric/year isn't in the data,
- a comparison's metric is computable for none of the companies,
- there's no material move to explain,
- retrieval finds nothing relevant,
- or the self-check finds the answer unsupported.

Each refusal is specific about *why*, which is far more useful (and trustworthy) than a
confident wrong answer.

## LLM access: `_ollama_chat`

```python
_ollama_chat(system, user, model="qwen2.5:7b-instruct", json_mode=False, temperature=0.0)
```

Direct POST to Ollama's `/api/chat`. Temperature 0 for reproducible eval. `json_mode`
forces structured output for the self-check. Raises a clear error if Ollama is down.

> **Design note:** we call Ollama's REST API directly rather than through a LangChain
> chat wrapper. For two simple endpoints, the wrapper added version-coupling and
> indirection without benefit; raw calls make JSON-mode and temperature control trivial.
> LangGraph is used where it earns its place — the control flow — not everywhere.

## Adding a new question type

1. Add a route literal to `State.route` and a branch in `node_classify`.
2. Write a `node_<type>(state) -> State` that fills `answer`, `evidence`, `grounded`,
   and appends to `trace` (refuse by setting `refused=True`).
3. Register the node and edges in `build_graph()`.
4. Add eval cases in `eval/questions.json`.

## Entry point

```python
ask("Compare operating margin between NVDA and INTC") -> State
```

Returns the full state: `answer`, `evidence` (metric provenance or cited chunks),
`refused`, and `trace`.
