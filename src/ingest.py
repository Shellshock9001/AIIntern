"""
ingest.py — One-shot corpus builder.

Downloads the most recent 10-K/10-Q filings for the four semiconductor companies
and indexes their narrative sections into Chroma using local Ollama embeddings.

Run once after starting Ollama:
    ollama pull nomic-embed-text
    ollama pull qwen2.5:7b-instruct
    python src/ingest.py

XBRL numeric facts are fetched on demand by metrics.py and cached separately, so
this step only builds the RAG text index.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sec_client import list_filings, download_filing  # noqa: E402
from rag import build_chunks, index_chunks  # noqa: E402

TICKERS = ["NVDA", "AMD", "INTC", "AVGO"]
FILINGS_PER_TICKER = 4  # ~16 filings total -> within the 10-15 brief target band


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    total_chunks = 0
    total_filings = 0
    for tk in TICKERS:
        refs = list_filings(tk, forms=("10-K", "10-Q"), limit=FILINGS_PER_TICKER)
        for ref in refs:
            path = download_filing(ref)
            chunks = build_chunks(path, ref)
            added = index_chunks(chunks)
            total_filings += 1
            total_chunks += added
            print(f"{tk:5s} {ref.form:5s} {ref.report_date}  "
                  f"{len(chunks):3d} chunks ({added} new)")
    print(f"\nIndexed {total_filings} filings, {total_chunks} new chunks.")
    print("Vector store: data/chroma/  |  Numeric facts cache: data/cache/")


if __name__ == "__main__":
    main()
