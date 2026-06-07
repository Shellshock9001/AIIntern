"""
ingest.py — Idempotent corpus builder.

Only does work that's missing: filings already in the vector store are skipped, so
re-running is cheap and safe. Callable as a function (used by run.py and the app's
first-launch auto-build) or from the CLI.

    python src/ingest.py                 # ingest active universe, skip what exists
    python src/ingest.py --refresh       # rebuild everything
    python src/ingest.py --tickers WMT TGT
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sec_client import list_filings, download_filing  # noqa: E402
from rag import build_chunks, index_chunks, indexed_filings, get_collection  # noqa: E402
import config  # noqa: E402

FILINGS_PER_TICKER = 4  # ~16 filings for the 4-company seed -> brief's 10-15 band


def run_ingest(tickers=None, refresh: bool = False,
               per_ticker: int = FILINGS_PER_TICKER, log=print) -> dict:
    """
    Build/extend the corpus. Returns a summary dict. Idempotent: skips filings
    already present unless refresh=True.
    """
    tickers = tickers or config.active_tickers()
    if refresh:
        try:
            import chromadb
            from rag import CHROMA_DIR
            client = chromadb.PersistentClient(path=str(CHROMA_DIR))
            client.delete_collection("filings")
        except Exception:
            pass

    already = set() if refresh else indexed_filings()
    total_new_chunks = 0
    filings_done = 0
    filings_skipped = 0

    for tk in tickers:
        refs = list_filings(tk, forms=("10-K", "10-Q"), limit=per_ticker)
        for ref in refs:
            if (tk.upper(), ref.accession) in already:
                filings_skipped += 1
                continue
            path = download_filing(ref)
            chunks = build_chunks(path, ref)
            log(f"  {tk:5s} {ref.form:5s} {ref.report_date}  "
                f"embedding {len(chunks)} chunks…")
            added = index_chunks(
                chunks,
                on_progress=lambda done, tot: log(f"      …{done}/{tot} embedded")
                if done % 128 == 0 or done >= tot else None)
            total_new_chunks += added
            filings_done += 1
            log(f"  {tk:5s} {ref.form:5s} {ref.report_date}  "
                f"{len(chunks):3d} chunks ({added} new)")

    return {
        "tickers": tickers,
        "filings_indexed": filings_done,
        "filings_skipped": filings_skipped,
        "new_chunks": total_new_chunks,
        "total_chunks": get_collection().count(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="Rebuild the entire corpus from scratch.")
    ap.add_argument("--tickers", nargs="*", default=None,
                    help="Override the universe for this ingest.")
    ap.add_argument("--per-ticker", type=int, default=FILINGS_PER_TICKER)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    tickers = [t.upper() for t in args.tickers] if args.tickers else config.active_tickers()
    print(f"Ingesting universe: {tickers}"
          + (" (full refresh)" if args.refresh else " (skipping already-indexed)"))
    s = run_ingest(tickers, refresh=args.refresh, per_ticker=args.per_ticker)
    print(f"\nDone. {s['filings_indexed']} filings indexed, "
          f"{s['filings_skipped']} already present, "
          f"{s['new_chunks']} new chunks. Corpus total: {s['total_chunks']} chunks.")


if __name__ == "__main__":
    main()
