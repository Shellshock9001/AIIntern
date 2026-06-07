#!/usr/bin/env python3
"""
run.py — One command to rule them all.

    python run.py            # doctor -> ingest-if-needed -> launch dashboard
    python run.py up         # same as above (explicit)
    python run.py eval       # run the evaluation suite
    python run.py ingest     # (re)build the corpus only
    python run.py ingest --refresh
    python run.py doctor     # environment preflight only

No more juggling three terminals. The dashboard is the default; ingest runs
automatically only when the corpus is missing or stale. Ollama/model/dependency
problems are reported with the exact fix instead of a mid-run stack trace.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# --- Hard prerequisite: Python version -------------------------------------
# The codebase uses 3.10+ syntax (X | None unions, modern typing). Fail early
# with a clear message instead of a cryptic SyntaxError deep in an import.
MIN_PY = (3, 10)
if sys.version_info < MIN_PY:
    sys.exit(
        f"\nARGUS needs Python {MIN_PY[0]}.{MIN_PY[1]} or newer — you have "
        f"{sys.version_info.major}.{sys.version_info.minor}.\n"
        f"Install a newer Python from https://www.python.org/downloads/ and "
        f"re-create the virtualenv:\n"
        f"  python3.12 -m venv .venv\n")

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

OLLAMA_URL = "http://localhost:11434"
NEEDED_MODELS = ["qwen2.5:7b-instruct", "nomic-embed-text"]

GREEN, RED, YEL, DIM, END = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"
def ok(m): print(f"{GREEN}✓{END} {m}")
def bad(m): print(f"{RED}✗{END} {m}")
def warn(m): print(f"{YEL}!{END} {m}")
def info(m): print(f"{DIM}  {m}{END}")


# ---------------------------------------------------------------------------
# Preflight doctor
# ---------------------------------------------------------------------------
def check_python_deps() -> bool:
    missing = []
    for mod in ["streamlit", "chromadb", "requests", "bs4", "plotly",
                "pandas", "langgraph"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        bad(f"Missing Python packages: {', '.join(missing)}")
        info("Fix:  pip install -r requirements.txt")
        return False
    ok("Python dependencies present")
    return True


def _ollama_installed() -> bool:
    """Is the `ollama` binary on PATH (installed) regardless of running state?"""
    import shutil
    return shutil.which("ollama") is not None


def check_ollama() -> tuple[bool, list[str]]:
    import requests
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=4)
        r.raise_for_status()
    except Exception:
        if _ollama_installed():
            bad("Ollama is installed but not running")
            info("Fix:  run `ollama serve` (or just open the Ollama app)")
        else:
            bad("Ollama is not installed")
            info("Fix:  install from https://ollama.com/download, then `ollama serve`")
        return False, []
    have = [m["name"] for m in r.json().get("models", [])]
    ok("Ollama is running")
    return True, have


def pull_missing_models(have: list[str], auto: bool = False) -> bool:
    """Offer to pull any missing models via the ollama CLI. Returns True if all present."""
    missing = [m for m in NEEDED_MODELS
               if not any(h == m or h.startswith(m.split(":")[0]) for h in have)]
    if not missing:
        return True
    if not _ollama_installed():
        for m in missing:
            warn(f"Model not pulled: {m}  (fix: ollama pull {m})")
        return False
    if not auto:
        resp = input(f"Pull missing model(s) {missing} now? This downloads "
                     f"several GB. [y/N] ").strip().lower()
        if resp != "y":
            for m in missing:
                info(f"Skipped. Later: ollama pull {m}")
            return False
    for m in missing:
        print(f"Pulling {m} … (one time)")
        try:
            subprocess.run(["ollama", "pull", m], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            bad(f"Failed to pull {m}: {e}")
            return False
    return True


def check_models(have: list[str]) -> bool:
    # Ollama may report names with or without :latest; normalize.
    norm = {h.split(":")[0]: h for h in have}
    missing = []
    for m in NEEDED_MODELS:
        base = m.split(":")[0]
        if not any(h == m or h.startswith(base) for h in have):
            missing.append(m)
    if missing:
        for m in missing:
            warn(f"Model not pulled: {m}")
            info(f"Fix:  ollama pull {m}")
        return False
    ok(f"Required models present: {', '.join(NEEDED_MODELS)}")
    return True


def check_corpus() -> dict:
    from rag import corpus_status
    import config
    s = corpus_status()
    active = set(t.upper() for t in config.active_tickers())
    have = set(s["tickers"])
    if s["chunks"] == 0:
        warn("Corpus is empty — will build on launch")
    elif not active.issubset(have):
        warn(f"Corpus missing some active tickers: {sorted(active - have)} "
             f"— will extend on launch")
    else:
        ok(f"Corpus ready: {s['chunks']} chunks across {sorted(have)}")
    return s


def doctor(require_ollama: bool = True, verbose: bool = True) -> dict:
    if verbose:
        print(f"\n{DIM}── preflight ─────────────────────────────────{END}")
        deps = check_python_deps()
        ollama_up, have = check_ollama()
        models_ok = check_models(have) if ollama_up else False
        corpus = check_corpus()
        print(f"{DIM}──────────────────────────────────────────────{END}\n")
    else:
        # Silent checks; only speak if something is actually wrong.
        deps = _quiet_deps()
        ollama_up, have = _quiet_ollama()
        models_ok = _quiet_models(have) if ollama_up else False
        corpus = _quiet_corpus()
        problems = []
        if not deps:
            problems.append("missing Python deps (pip install -r requirements.txt)")
        if not ollama_up:
            problems.append("Ollama not running (Ask/Insights will be limited)")
        elif not models_ok:
            problems.append("a required model isn't pulled")
        if problems:
            for p in problems:
                warn(p)
    return {"deps": deps, "ollama": ollama_up, "models": models_ok,
            "corpus": corpus}


def _quiet_deps() -> bool:
    try:
        for m in ["streamlit", "chromadb", "requests", "bs4", "plotly",
                  "pandas", "langgraph"]:
            __import__(m)
        return True
    except ImportError:
        return False


def _quiet_ollama():
    import requests
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=4)
        r.raise_for_status()
        return True, [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return False, []


def _quiet_models(have) -> bool:
    return all(any(h == m or h.startswith(m.split(":")[0]) for h in have)
               for m in NEEDED_MODELS)


def _quiet_corpus() -> dict:
    from rag import corpus_status
    return corpus_status()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def do_ingest(refresh: bool = False) -> None:
    from ingest import run_ingest
    import config
    print(f"Building corpus for {config.active_tickers()}"
          + (" (full refresh)…" if refresh else " (skipping indexed)…"))
    s = run_ingest(refresh=refresh)
    print(f"\nCorpus: {s['total_chunks']} chunks "
          f"({s['new_chunks']} new, {s['filings_skipped']} already present).")


def do_launch() -> None:
    print(f"\n{GREEN}Launching dashboard → http://localhost:8501{END}")
    print(f"{DIM}(Ctrl+C to stop){END}\n")
    subprocess.run([sys.executable, "-m", "streamlit", "run",
                    str(SRC / "app.py")])


def do_eval() -> None:
    subprocess.run([sys.executable, str(ROOT / "eval" / "run_eval.py")])


def do_up(refresh: bool = False) -> None:
    import config
    state = doctor(verbose=False)
    if not state["deps"]:
        sys.exit("Missing dependencies. Run: pip install -r requirements.txt")
    # If Ollama is up but a model is missing, offer to pull it now.
    if state["ollama"] and not state["models"]:
        import requests
        try:
            have = [m["name"] for m in requests.get(
                f"{OLLAMA_URL}/api/tags", timeout=4).json().get("models", [])]
        except Exception:
            have = []
        if pull_missing_models(have):
            state["models"] = True
    active = set(t.upper() for t in config.active_tickers())
    have = set(state["corpus"]["tickers"])
    needs_ingest = refresh or state["corpus"]["chunks"] == 0 or not active.issubset(have)
    if needs_ingest:
        if not state["ollama"] or not state["models"]:
            warn("Ollama/model unavailable — launching with numeric features only. "
                 "Build the narrative index later with `python run.py ingest`.")
        else:
            missing = sorted(active - have) if not refresh else sorted(active)
            print(f"First-time setup: building narrative index for {missing} "
                  f"(one time only)…")
            do_ingest(refresh=refresh)
    else:
        ok(f"Corpus ready ({state['corpus']['chunks']} chunks) · "
           f"{len(active)} companies · launching")
    do_launch()


# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    cmd = args[0] if args and not args[0].startswith("-") else "up"
    refresh = "--refresh" in args

    if cmd == "doctor":
        doctor()
    elif cmd == "ingest":
        do_ingest(refresh=refresh)
    elif cmd == "eval":
        do_eval()
    elif cmd in ("up", "run", "start"):
        do_up(refresh=refresh)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
