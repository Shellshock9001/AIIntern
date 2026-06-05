"""
rag.py — Narrative retrieval layer.

Numbers come from XBRL (metrics.py). This layer handles the QUALITATIVE side:
MD&A, risk factors, business descriptions. Every chunk carries the accession
number, form, filing date, and section so any retrieved passage is citable back
to a specific filing.

Embeddings + generation are LOCAL via Ollama:
  ollama pull nomic-embed-text          # 274MB embedding model
  ollama pull qwen2.5:7b-instruct       # generation / reasoning
Vector store: Chroma (persistent, on disk).
"""
from __future__ import annotations

import re
import logging
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

log = logging.getLogger("rag")

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
CHROMA_DIR = Path(__file__).resolve().parent.parent / "data" / "chroma"
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

# 10-K / 10-Q item headers we care about, in regex form (case-insensitive).
SECTION_PATTERNS = [
    ("Risk Factors", r"item\s*1a\.?\s*risk\s*factors"),
    ("MD&A", r"item\s*[27]\.?\s*management.s\s*discussion"),
    ("Business", r"item\s*1\.?\s*business"),
    ("Legal Proceedings", r"item\s*3\.?\s*legal\s*proceedings"),
    ("Quantitative Market Risk", r"item\s*[37]a\.?\s*quantitative"),
]


@dataclass
class Chunk:
    text: str
    ticker: str
    form: str
    accession: str
    filing_date: str
    report_date: str
    section: str
    chunk_id: str

    def citation(self) -> str:
        return (f"{self.ticker} {self.form} (filed {self.filing_date}, "
                f"period {self.report_date}) — {self.section} [acc {self.accession}]")


# ---------------------------------------------------------------------------
# HTML -> clean text
# ---------------------------------------------------------------------------
def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "table"]):
        # Tables are numeric — those belong to XBRL, not the narrative index.
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse whitespace, strip page-number noise.
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"\d{1,4}", ln)]
    return re.sub(r"\n{2,}", "\n\n", "\n".join(lines))


def split_sections(text: str) -> list[tuple[str, str]]:
    """
    Partition filing text into (section_name, body) using item headers. Anything
    before the first recognized header is labeled 'Front Matter'. Best-effort:
    if no headers match (some HTML is irregular) we return one 'Full Filing' blob.
    """
    matches = []
    for name, pat in SECTION_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            matches.append((m.start(), name))
    if not matches:
        return [("Full Filing", text)]
    matches.sort()
    out = []
    for i, (pos, name) in enumerate(matches):
        end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        body = text[pos:end].strip()
        if len(body) > 200:
            out.append((name, body))
    return out


def chunk_text(body: str, size: int = 1200, overlap: int = 200) -> list[str]:
    """Character-window chunking with overlap, breaking on paragraph boundaries."""
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            if len(p) > size:
                # Hard-split an over-long paragraph.
                for i in range(0, len(p), size - overlap):
                    chunks.append(p[i:i + size])
                cur = ""
            else:
                cur = p
    if cur:
        chunks.append(cur)
    # Add overlap tails between adjacent chunks for retrieval continuity.
    return chunks


def build_chunks(local_path: Path, ref) -> list[Chunk]:
    """ref is a sec_client.FilingRef."""
    html = local_path.read_text(encoding="utf-8", errors="ignore")
    text = html_to_text(html)
    out: list[Chunk] = []
    for section, body in split_sections(text):
        for i, ch in enumerate(chunk_text(body)):
            out.append(Chunk(
                text=ch,
                ticker=ref.ticker,
                form=ref.form,
                accession=ref.accession,
                filing_date=ref.filing_date,
                report_date=ref.report_date,
                section=section,
                chunk_id=f"{ref.ticker}_{ref.accession}_{section.replace(' ', '')}_{i}",
            ))
    return out


# ---------------------------------------------------------------------------
# Ollama embeddings
# ---------------------------------------------------------------------------
def embed(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    """Batch embed via Ollama. Raises a clear error if Ollama isn't running."""
    vectors = []
    for t in texts:
        try:
            r = requests.post(f"{OLLAMA_URL}/api/embeddings",
                              json={"model": model, "prompt": t}, timeout=120)
            r.raise_for_status()
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                "Cannot reach Ollama at localhost:11434. Start it and run "
                f"`ollama pull {model}` first."
            ) from e
        vectors.append(r.json()["embedding"])
    return vectors


# ---------------------------------------------------------------------------
# Chroma index
# ---------------------------------------------------------------------------
def get_collection(name: str = "filings"):
    import chromadb
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


def index_chunks(chunks: list[Chunk], batch: int = 32) -> int:
    col = get_collection()
    added = 0
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        existing = set(col.get(ids=[c.chunk_id for c in part]).get("ids", []))
        part = [c for c in part if c.chunk_id not in existing]
        if not part:
            continue
        vecs = embed([c.text for c in part])
        col.add(
            ids=[c.chunk_id for c in part],
            embeddings=vecs,
            documents=[c.text for c in part],
            metadatas=[{
                "ticker": c.ticker, "form": c.form, "accession": c.accession,
                "filing_date": c.filing_date, "report_date": c.report_date,
                "section": c.section, "citation": c.citation(),
            } for c in part],
        )
        added += len(part)
        log.info("Indexed %d/%d chunks", i + len(part), len(chunks))
    return added


@dataclass
class Retrieved:
    text: str
    citation: str
    section: str
    ticker: str
    distance: float


def retrieve(query: str, k: int = 5, tickers: Optional[list[str]] = None) -> list[Retrieved]:
    col = get_collection()
    qvec = embed([query])[0]
    where = {"ticker": {"$in": [t.upper() for t in tickers]}} if tickers else None
    res = col.query(query_embeddings=[qvec], n_results=k, where=where)
    out = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        out.append(Retrieved(
            text=doc, citation=meta["citation"], section=meta["section"],
            ticker=meta["ticker"], distance=dist,
        ))
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    # Standalone ingest: download filings for the 4 tickers and index them.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sec_client import list_filings, download_filing

    total = 0
    for tk in ["NVDA", "AMD", "INTC", "AVGO"]:
        refs = list_filings(tk, limit=4)  # ~4 filings each -> ~16 docs
        for ref in refs:
            path = download_filing(ref)
            chunks = build_chunks(path, ref)
            total += index_chunks(chunks)
            print(f"{tk} {ref.form} {ref.report_date}: {len(chunks)} chunks")
    print(f"\nTotal newly indexed chunks: {total}")
