# 09 — Extending the System

Concrete recipes for the most common extensions. Each is small because the system is
built to be extended.

## Add or change companies

**At runtime (no code):** use the sidebar in the app — type a ticker, click Add. Or:

```python
import config
config.add_ticker("WMT")                      # validates against SEC's live map
config.set_universe(["JPM","BAC","WFC","C"], "Banks")   # whole new sector
```

The universe persists in `data/universe.json`. Everything — metrics, charts, briefing,
conflicts, agent — recomputes for the new set. Then rebuild the narrative index:

```bash
python run.py ingest        # idempotent; only fetches what's new
```

## Add a financial metric

Two steps (see [04-metrics.md](04-metrics.md)):

1. If the metric needs a new input concept, add it to `CONCEPT_ALIASES` in
   `sec_client.py` with the real XBRL tag(s) companies use.
2. In `metrics.compute_metrics`, fetch inputs with `get("Concept", fy)` and append a
   `DerivedMetric(... formula=..., inputs=[_prov(a), _prov(b)])`, guarded so it refuses
   when inputs are absent.

It then flows automatically into charts, drill-down, the scorecard, and the agent.

## Add a new XBRL concept (to pull more data)

The CompanyFacts endpoint exposes 400–900+ concepts per filer. To surface a new one,
add it to `CONCEPT_ALIASES`. Find the exact tag by inspecting the raw facts:

```python
from sec_client import get_company_facts
gaap = get_company_facts("NVDA")["facts"]["us-gaap"]
print([k for k in gaap if "Inventory" in k])   # discover the real tag names
```

Then add e.g. `"Inventory": ["InventoryNet"]`. `discover_concepts` and `extract_facts`
pick it up immediately.

## Add a new question type to the agent

Routing is LLM-driven (`_parse_intent_llm`) with a heuristic backstop, both over the
catalog of available quantities. To add a type: add a `route` value the parser can emit
(extend the system prompt and `_heuristic_intent`), write a `node_<type>(state) -> State`,
register it in `build_graph` and the conditional edges, and add eval cases. See
[06-agent.md](06-agent.md).

## Expose more raw line items to the agent

The agent already answers about every concept in `RAW_CONCEPT_LABELS` (in `metrics.py`).
Add an entry there (mapping the canonical concept to a label + unit) and ensure the
concept is in `CONCEPT_ALIASES`; it becomes askable immediately ("what was X's
inventory in FY2024").

## Tune retrieval

In `rag.py`: chunk size/overlap in `chunk_text`, `k` in `retrieve`, sections in
`SECTION_PATTERNS`, or wrap `retrieve` with a reranker / hybrid keyword search (the
documented "next step" for precision).

## Swap the LLM or embedding provider

The brief allows any provider. Two touch points:

- **Generation:** `_ollama_chat` in `agent.py`. To use a hosted API, replace the POST
  body with that provider's chat schema and read the response text. Keep `temperature=0`
  for reproducible eval.
- **Embeddings:** `embed` in `rag.py`. Replace the Ollama call with the provider's embed
  endpoint; ensure the vector dimension is consistent (rebuild the index if you change
  models, since dimensions/space differ).

Models are named in two constants: `GEN_MODEL` (agent.py) and `EMBED_MODEL` (rag.py).

## Add a new filing type (e.g. 20-F for foreign filers)

In `sec_client.list_filings`, add the form to the `forms` tuple. The annual-period logic
(`is_annual_period`) and extraction handle it. The RAG section patterns may need
adjustment for 20-F's different structure.

## Add a conflict detector

In `conflicts.py`, write `find_<kind>(facts, ticker) -> list[Conflict]` and add it to
`all_conflicts`. Add a label + emoji in the app's Data Health tab `KIND_LABELS`.

## Change materiality thresholds

In `linkage.py`, edit the `MATERIALITY` dict — what counts as a "material move" per
metric.

## Project layout for new modules

Put pure-logic modules in `src/`, import them in `app.py` for UI, and add eval coverage.
Keep the trust boundary: numbers from XBRL/metrics, prose from RAG, and never let an LLM
originate a figure.
