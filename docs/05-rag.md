# 05 — `rag.py`: The Retrieval System

This is the narrative side: turning filing prose into a searchable, citable index.
If you want to rebuild or tune the RAG pipeline, this is the document.

## Pipeline overview

```
filing HTML ──► html_to_text ──► split_sections ──► chunk_text ──► build_chunks
                (strip tags,       (MD&A, Risk,       (1200-char     (Chunk objects
                 tables, noise)     Business…)         windows,        w/ citation
                                                       200 overlap)     metadata)
                                                                          │
                                          Ollama nomic-embed-text ◄───────┤
                                                  │                       │
                                                  ▼                       │
                                          ChromaDB (cosine) ◄─────────────┘
                                                  │
                              retrieve(query, k, tickers) ──► Retrieved[]  (text + citation + distance)
```

## Why tables are stripped

```python
for tag in soup(["script", "style", "table"]):
    tag.decompose()
```

We deliberately remove `<table>` elements before indexing. **Numbers belong to XBRL,
not to the text index.** Stripping tables keeps the RAG layer focused on prose
(narrative explanations, risk language) and prevents the LLM from being tempted to
quote a number out of a poorly-parsed HTML table. This is the trust boundary enforced
at the data level.

## Section splitting: `split_sections`

10-K/10-Q text is partitioned by item headers using regex:

```python
SECTION_PATTERNS = [
  ("Risk Factors",  r"item\s*1a\.?\s*risk\s*factors"),
  ("MD&A",          r"item\s*[27]\.?\s*management.s\s*discussion"),
  ("Business",      r"item\s*1\.?\s*business"),
  …
]
```

Each chunk is tagged with its section, so a retrieved passage can say "from MD&A" or
"from Risk Factors." If no headers match (irregular HTML), the whole filing becomes one
"Full Filing" section — a graceful fallback rather than a crash.

> **Note:** a header term can legitimately appear more than once in a filing (e.g. in
> the table of contents and again at the real section). This is why chunk IDs include a
> global running index — see "Chunk IDs" below. This was a real bug found during first
> ingest; documented in the WRITEUP.

## Chunking: `chunk_text`

- Target ~1200 characters per chunk, ~200 overlap.
- Splits on paragraph boundaries to keep ideas intact; hard-splits any paragraph longer
  than the window.
- Overlap preserves continuity so a sentence spanning a boundary is still retrievable.

These are tunable. Larger chunks = more context per hit but coarser retrieval; smaller =
more precise but more fragments. 1200/200 is a reasonable default for dense filing prose.

## The `Chunk` object and citations

```python
@dataclass
class Chunk:
    text, ticker, form, accession, filing_date, report_date, section, chunk_id
    def citation(self):
        return f"{ticker} {form} (filed {filing_date}, period {report_date}) — {section} [acc {accession}]"
```

Every chunk carries everything needed to cite it. The citation string is what surfaces
in the UI and what the agent attaches to narrative answers.

### Chunk IDs (uniqueness guarantee)

```python
chunk_id = f"{ticker}_{accession}_{section}_{sec_idx}_{i}_{gi}"
```

`sec_idx` (which occurrence of the section), `i` (chunk within section), and `gi` (a
global running counter across the whole filing) together guarantee uniqueness even when
a section header repeats. `index_chunks` also dedups defensively before writing to
Chroma.

## Embeddings: `embed`

```python
embed(texts) -> list[list[float]]   # via Ollama POST /api/embeddings, nomic-embed-text
```

Local, free, 274 MB model. If Ollama isn't running, it raises a clear `RuntimeError`
telling you to start it and pull the model — no silent failure. One vector per text;
batched by the caller.

## Vector store: ChromaDB

```python
get_collection()  # PersistentClient at data/chroma/, cosine space, collection "filings"
```

Persistent on disk, no server. Cosine similarity over the nomic embeddings.

### Corpus state (powers idempotent ingest)

```python
corpus_status()   -> {chunks: int, tickers: set, filings: set of (ticker, accession)}
indexed_filings() -> set of (ticker, accession)
```

These let `ingest.py` skip filings already indexed and let the app show a "build index"
prompt only when needed.

## Indexing: `index_chunks`

- Global dedup of chunk IDs (defensive).
- Per-batch check against existing IDs (skip already-stored).
- Embeds and adds new chunks with full metadata.
- Returns the count of newly added chunks.

Idempotent: running it twice adds nothing the second time.

## Retrieval: `retrieve`

```python
retrieve(query, k=5, tickers=["NVDA"]) -> list[Retrieved]
```

Embeds the query, searches Chroma, optionally filters by ticker (`where` clause), and
returns `Retrieved(text, citation, section, ticker, distance)`. Distance is cosine
distance — lower is closer. The agent uses these as the *only* context the LLM may
answer from.

## Tuning / extending

| Want to… | Do this |
|----------|---------|
| Retrieve more/less context | change `k` in `retrieve`, or chunk size in `chunk_text` |
| Index more filings | raise `limit` in `ingest.run_ingest` / `list_filings` |
| Add a new section type | add a pattern to `SECTION_PATTERNS` |
| Swap the embedding model | change `EMBED_MODEL` (must be an Ollama embed model) |
| Add hybrid keyword search | wrap `retrieve` to also do a BM25 pass and merge (a known next step — see WRITEUP "what I'd fix first") |
| Add a reranker | re-score the top-k `Retrieved` before returning |

## Why this design

It's a deliberately *simple, inspectable* RAG: section-aware chunking + a compact local
embedder + a persistent local store. No external services, no cost, fully reproducible.
The sophistication is concentrated where it matters for trust — the citation metadata on
every chunk and the agent's grounding self-check — rather than in retrieval cleverness.
