# ARGUS FinDash

Grounded, citation-first financial analysis over SEC filings for four
semiconductor companies — **NVDA, AMD, INTC, AVGO**. A Streamlit dashboard with
agentic Q&A, comparative metrics, full numeric provenance, and a data-conflict
view. Runs entirely on a **local, $0 stack** (Ollama) — no API keys, no per-token
cost, fully reproducible.

---

## What makes this different

Most RAG-over-filings systems feed PDF/HTML text to an LLM and ask it for
numbers — which is exactly how you get confident, wrong figures. ARGUS splits the
problem by **trust tier**:

| Layer | Source | Who produces the number |
|-------|--------|------------------------|
| **Quantitative spine** | SEC XBRL CompanyFacts API | The *filer* tagged it; we never let the LLM generate digits |
| **Narrative** | 10-K / 10-Q HTML (MD&A, Risk Factors) | LLM reasons over retrieved text, cites passages, never invents figures |

Every figure — extracted or derived — traces to a specific **accession number**,
fiscal year, and formula. Numeric questions are answered deterministically from
XBRL; the LLM only phrases the result. Narrative questions must pass a grounding
self-check or the system refuses.

---

## Architecture

```
                    ┌────────────────────── Streamlit app (app.py) ──────────────────────┐
                    │  Ask · Compare · Drill-down (provenance) · Data Health (conflicts)  │
                    └───────────────┬─────────────────────────────────┬──────────────────┘
                                    │                                 │
                   ┌────────────────▼─────────────┐      ┌────────────▼───────────────┐
                   │  agent.py  (LangGraph)        │      │  metrics.py                 │
                   │  classify → rewrite → route → │      │  margins, D/E, FCF, YoY,    │
                   │  {numeric | comparative |     │◄─────┤  CAGR — each with inputs +  │
                   │   narrative} → self-check →   │      │  formula + accession        │
                   │  finalize | REFUSE            │      └────────────┬────────────────┘
                   └───────┬───────────────┬───────┘                   │
                           │               │                           │
              ┌────────────▼──────┐  ┌─────▼───────────┐    ┌──────────▼─────────────┐
              │ rag.py            │  │ Ollama (local)  │    │ sec_client.py          │
              │ chunk + embed +   │  │ qwen2.5:7b      │    │ XBRL CompanyFacts +     │
              │ Chroma retrieve   │  │ nomic-embed-text│    │ filing HTML, provenance │
              └───────────────────┘  └─────────────────┘    └────────────────────────┘
```

### Files
- `src/sec_client.py` — SEC EDGAR client: XBRL facts (ground truth) + filing HTML, caching, rate-limiting, multi-alias tag resolution.
- `src/metrics.py` — derived-metric engine with input/formula traceability and restatement-conflict detection.
- `src/rag.py` — HTML→text, section split, chunking with citation metadata, Ollama embeddings, Chroma store/retrieve.
- `src/agent.py` — LangGraph state machine: routing, numeric/comparative/narrative tools, grounding self-check, refusal.
- `src/app.py` — Streamlit dashboard (4 tabs).
- `src/ingest.py` — one-shot corpus builder.
- `eval/questions.json` — labeled eval set (numeric / comparative / narrative / unanswerable).
- `eval/run_eval.py` — real eval runner (no mocks); reports correctness, citation accuracy, hallucination rate.

---

## Setup & run

### 1. Install Ollama and pull local models
```bash
# https://ollama.com/download
ollama pull qwen2.5:7b-instruct      # generation / reasoning (~4.7 GB)
ollama pull nomic-embed-text         # embeddings (~274 MB)
ollama serve                         # if not already running on :11434
```

### 2. Python environment
```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Build the corpus (downloads filings + builds vector index)
```bash
python src/ingest.py
```

### 4. Launch the dashboard
```bash
streamlit run src/app.py
```

The **Compare** and **Drill-down** tabs work from cached XBRL facts even without
Ollama. The **Ask** tab needs Ollama running.

### 5. Run the evaluation
```bash
python eval/run_eval.py            # writes eval/results.json
```

---

## Design choices (and why)

- **XBRL for numbers, RAG for narrative.** The single most important decision.
  Filer-tagged XBRL eliminates numeric hallucination at the source; the LLM is
  never in the loop for a digit.
- **Local Ollama stack.** $0, no keys, reproducible by any reviewer, and keeps
  filing text on-device. `qwen2.5:7b-instruct` has strong structured-output
  behavior for the JSON self-check; `nomic-embed-text` is a compact, solid
  retrieval embedder.
- **Multi-alias concept resolution.** Companies switch XBRL tags across years
  (NVDA reports revenue under `RevenueFromContractWithCustomerExcludingAssessedTax`
  early, then `Revenues`). We collect from all aliases and resolve per period, so
  there are no silent gaps at tag-switch boundaries.
- **Annual selection by period duration, not the `fy` label.** XBRL bundles
  quarterly and annual entries under one `fy`. We select genuine full-year flows
  by ~365-day duration and balance-sheet items by period-end date.
- **Refuse, don't invent.** Missing input tag → no metric. Requested year not
  available → refusal listing the years that *are* available. Narrative answer
  that fails the grounding self-check → withheld.
- **Conflicts surfaced, not hidden.** Restatements (same period, different values
  across filings) are computed from the latest-filed value but shown in the Data
  Health tab.
- **LangGraph** for orchestration: explicit, inspectable state machine; the Ask
  tab exposes the full reasoning trace.

---

## Document sources

All documents are public, from **SEC EDGAR** (`https://www.sec.gov`). Numeric data
via the official XBRL API (`https://data.sec.gov/api/xbrl/companyfacts/`), no key
required. Filings indexed (4 most-recent per company at authoring time; `ingest.py`
always pulls current):

| Company | CIK | Example filing | Accession |
|---------|-----|----------------|-----------|
| NVIDIA (NVDA) | 0001045810 | 10-K FY ending 2026-01-25 | 0001045810-26-000021 |
| AMD (AMD) | 0000002488 | 10-K FY ending 2025-12-27 | 0000002488-26-000018 |
| Intel (INTC) | 0000050863 | 10-K FY ending 2025-12-27 | 0000050863-26-000011 |
| Broadcom (AVGO) | 0001730168 | 10-K FY ending 2025-11-02 | 0001730168-25-000121 |

XBRL numeric history extends back to ~2007–2009 per company (when XBRL tagging
began), enabling multi-year trend and CAGR analysis.

---

## Evaluation results

Run `python eval/run_eval.py` to generate `eval/results.json` on your machine.
The numeric, comparative, and refusal-via-numeric-engine cases are deterministic
and were verified at build time (6/6 numeric exact-match, 3/3 comparative leader
correct). Narrative correctness, citation accuracy, and the full hallucination
rate require Ollama and are produced by the live run. See `WRITEUP.md` for
interpretation. **Do not copy numbers you have not generated** — paste your real
`results.json` summary here after running.
