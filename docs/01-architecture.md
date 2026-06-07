# 01 — Architecture

## The core problem

A naive "RAG over filings" system feeds 10-K text to an LLM and asks it for numbers.
LLMs are fluent but not arithmetic engines reading tables: they transpose digits,
mix up thousands vs. millions, attribute one company's figure to another, and state
all of it with total confidence. For financial analysis — where a wrong margin can
mislead an investment decision — that failure mode is disqualifying.

ARGUS is built around avoiding it.

## The two-tier trust model

We split every request along a boundary defined by **who produced the number**.

```
                          ┌─────────────────────────────────────────┐
                          │              A QUESTION                   │
                          └───────────────────┬───────────────────────┘
                                              │ classify
                  ┌───────────────────────────┼───────────────────────────┐
                  │                            │                           │
          is it about a               is it about WHY a            is it about the
          FIGURE / METRIC?            metric changed?              filing's PROSE?
                  │                            │                           │
                  ▼                            ▼                           ▼
        ┌───────────────────┐      ┌─────────────────────┐     ┌────────────────────┐
        │  NUMERIC tier      │      │  CAUSAL tier         │     │  NARRATIVE tier     │
        │  XBRL ground truth │      │  XBRL move + cited   │     │  RAG over filing    │
        │  LLM phrases only  │      │  MD&A passage        │     │  text, must cite or │
        │                    │      │                      │     │  refuse             │
        └───────────────────┘      └─────────────────────┘     └────────────────────┘
              deterministic               deterministic #             retrieved +
              (no hallucination           + retrieved prose           self-checked
               possible)
```

- **Numeric tier** (the quantitative spine). Numbers come from the SEC XBRL
  CompanyFacts API, where the *filer* tagged each value. The LLM never generates a
  digit; at most it phrases a value we already computed. Structurally, a numeric
  answer cannot be a hallucinated number — only a possibly-wrong *concept choice*,
  which is why we always show the XBRL tag + accession for one-glance verification.

- **Narrative tier**. Qualitative questions (risk factors, MD&A commentary) are
  answered by retrieving filing passages and asking the LLM to answer **only** from
  them, citing each. A grounding self-check then verifies the answer is supported;
  if not, the system refuses rather than guess.

- **Causal tier** bridges the two: it detects a material metric move (numeric,
  deterministic) and links it to the filing passage that explains it (narrative,
  cited).

## Data flow

```
  SEC EDGAR
  ├── XBRL CompanyFacts API ──► sec_client.extract_facts ──► metrics.compute_metrics
  │   (data.sec.gov, JSON)          (Fact objects,            (DerivedMetric: value +
  │                                  provenance)               formula + inputs)
  │                                                                   │
  │                                                                   ├─► briefing.py (scorecard)
  │                                                                   ├─► linkage.py (material moves)
  │                                                                   └─► conflicts.py (data health)
  │
  └── Filing HTML (10-K/10-Q) ──► rag.build_chunks ──► Ollama embed ──► ChromaDB
      (www.sec.gov/Archives)        (section split,      (nomic-embed-     (vector store)
                                     citation metadata)    text)                │
                                                                                ▼
                                                              agent.py (LangGraph) ──► app.py
                                                              LLM intent-parse →       (Streamlit,
                                                              route → self-check        6 tabs)
```

## Why these technology choices

| Choice | Why |
|--------|-----|
| **SEC XBRL API for numbers** | Authoritative, machine-readable, filer-tagged. Eliminates numeric hallucination at the source. Free, no key. |
| **Local Ollama** (`qwen2.5:7b-instruct`, `nomic-embed-text`) | $0, no API keys, reproducible by any reviewer, keeps filing text on-device. The brief explicitly encourages small models and low cost. |
| **LangGraph** | Explicit, inspectable state machine. The routing/self-check/refuse logic is auditable, and we surface the reasoning trace in the UI. |
| **ChromaDB** | Lightweight, persistent, local vector store. No server to run. |
| **Streamlit** | Fast path to a clean, interactive dashboard — the required deliverable. |

## Files at a glance

```
AIIntern/
├── run.py              # one-command orchestrator (doctor → ingest → launch)
├── run.bat / Makefile  # OS launchers
├── src/
│   ├── config.py       # dynamic company universe (no hardcoded list)
│   ├── sec_client.py   # SEC EDGAR: XBRL facts + filings + deep-links
│   ├── metrics.py      # derived metrics with formula + provenance
│   ├── rag.py          # chunk → embed → store → retrieve
│   ├── agent.py        # LangGraph: route → tool → self-check → refuse
│   ├── linkage.py      # material moves linked to explaining narrative
│   ├── conflicts.py    # restatement/scale/fiscal/tag conflict taxonomy
│   ├── briefing.py     # grounded exec briefing + competitive scorecard
│   ├── ingest.py       # idempotent corpus builder
│   ├── theme.py        # visual system: palette, CSS, SVG icons, Plotly style
│   └── app.py          # Streamlit dashboard (6 tabs, icon headers)
├── eval/
│   ├── questions.json  # labeled eval set
│   └── run_eval.py     # scoring: correctness, citation, hallucination
├── data/               # cache/, chroma/, filings/ (generated; git-ignored)
└── docs/               # this documentation
```

## Design principles, stated plainly

1. **Never invent a number.** Missing input → refuse. Requested year unavailable →
   refuse and list available years. Narrative answer fails grounding → withhold.
2. **Everything traces to source.** Figures carry accession + formula + a deep link
   to the statement table; passages carry their filing + section.
3. **Adapt, don't assume.** Coverage is discovered per company; a bank and a
   chipmaker get different metric sets because they report different things.
4. **Surface conflicts, don't paper over them.** Restatements and anomalies are
   shown, with the resolution rule stated.
