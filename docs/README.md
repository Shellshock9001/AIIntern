# ARGUS FinDash — Documentation

Full technical documentation for the dashboard. Each document is self-contained;
read in any order, but this is the suggested path.

| Doc | What it covers | Read if you want to… |
|-----|----------------|----------------------|
| [01-architecture.md](01-architecture.md) | System design, the two-tier trust model, data flow, why each choice was made | Understand the whole system in 10 minutes |
| [02-data-sources.md](02-data-sources.md) | What SEC EDGAR data we use, XBRL explained, what the numbers mean, the endpoints | Understand the data and where it comes from |
| [03-sec-client.md](03-sec-client.md) | `sec_client.py` — fetching facts/filings, concept aliases, provenance, deep-links | Extend data extraction or add concepts |
| [04-metrics.md](04-metrics.md) | `metrics.py` — every derived metric, its formula, inputs, and the annual-period logic | Add or audit a financial metric |
| [05-rag.md](05-rag.md) | `rag.py` — chunking, embeddings, the vector store, retrieval | Build or tune the RAG / narrative side |
| [06-agent.md](06-agent.md) | `agent.py` — the LangGraph agent, routing, self-check, refusal | Modify routing or add a new question type |
| [07-linkage-conflicts-briefing.md](07-linkage-conflicts-briefing.md) | The analytical layers: material-move linkage, conflict taxonomy, executive briefing | Extend the analysis beyond retrieval |
| [08-evaluation.md](08-evaluation.md) | `eval/` — the labeled set, scoring, hallucination rate, how to add cases | Run or expand the evaluation |
| [09-extending.md](09-extending.md) | How to add companies, metrics, concepts, sectors, question types, providers | Build on top of this |
| [10-operations.md](10-operations.md) | `run.py`, the doctor, idempotent ingest, troubleshooting | Run and operate the system |

## The one-paragraph version

ARGUS answers financial questions about public companies by splitting work along a
**trust boundary**: every *number* comes from the SEC's machine-readable XBRL data
(the company itself tagged it; an LLM never generates a digit), while every
*narrative* answer is retrieved from the filing text and must cite its source or be
refused. Derived metrics show their formula and inputs; each input deep-links to the
exact financial-statement table it came from. The system is fully local and free
(Ollama), dynamic (any of ~10,000 SEC filers, not a fixed list), and honest about
what it cannot answer.
