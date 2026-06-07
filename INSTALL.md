# INSTALL — Setting up ARGUS FinDash

A step-by-step guide with prerequisites, exact commands per platform, and a
troubleshooting matrix. If you just want the short version, the project `README.md`
has the one-command quick start; this document covers the details and edge cases.

---

## System requirements

| Resource | Minimum | Notes |
|----------|---------|-------|
| **Python** | 3.10+ | Uses modern typing syntax. 3.11/3.12 recommended. `run.py` checks this and exits with guidance if too old. |
| **RAM** | ~8 GB free | `qwen2.5:7b-instruct` needs ~5–6 GB to run. A GPU helps but isn't required. |
| **Disk** | ~7 GB | ~5 GB models + ~1 GB filings/index + deps. |
| **Network** | Yes (first run) | Fetches SEC filings and pulls Ollama models once; cached afterward. Outbound access to `*.sec.gov` and `ollama.com`. |
| **OS** | Windows, macOS, Linux | Launchers provided for each. |

The **numeric features** (Briefing, Compare, Drill-down, Data Health) need only
Python + internet to SEC. **Ask** and **Insights** narrative features also need Ollama.

---

## Prerequisites

### 1. Python 3.10+

- **Windows:** install from <https://www.python.org/downloads/> and **tick "Add
  python.exe to PATH"** during setup. Verify: `py --version`.
- **macOS:** `brew install python@3.12` (or python.org). Verify: `python3 --version`.
- **Linux:** `sudo apt install python3 python3-venv python3-pip` (Debian/Ubuntu).

### 2. Ollama (for Ask/Insights)

Install from <https://ollama.com/download>. After install it runs a background
service on `localhost:11434`. Verify: `ollama list`.

You can pull the models manually now, or let the launcher offer to pull them:

```bash
ollama pull qwen2.5:7b-instruct      # ~4.7 GB — generation/reasoning
ollama pull nomic-embed-text         # ~274 MB — embeddings
```

---

## Install & run

### Windows

```powershell
cd C:\path\to\AIIntern
.\run.bat
```

`run.bat` finds your Python (`py`/`python`/`python3`), creates the virtualenv,
installs dependencies on first run, and launches. If Python isn't found it prints
exactly what to install.

**PowerShell alternative** (if you prefer a native script):

```powershell
.\run.ps1
# If blocked by execution policy, first run:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### macOS / Linux

```bash
cd /path/to/AIIntern
make            # creates venv, installs, doctor, ingest-if-needed, launch
```

No `make`? Use the manual steps below.

### Manual (any platform)

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py                        # doctor -> ingest-if-needed -> launch
```

The dashboard opens at <http://localhost:8501>.

---

## First-run sequence (what to expect)

1. **Preflight** runs silently and only speaks if something needs attention. A
   healthy system prints one line: `✓ Corpus ready (… chunks) · 4 companies · launching`.
   On a *fresh* machine it will instead report what's missing.
2. If models are missing and Ollama is running, the launcher **offers to pull them**
   (a multi-GB, one-time download — you confirm).
3. On first launch it **builds the narrative index** (downloads ~16 filings, chunks
   and embeds them — a few minutes). This is one-time; later launches skip it.
4. The dashboard opens. Numeric tabs work immediately; Ask/Insights work once the
   index is built and Ollama is running.

Run a full environment report any time:

```bash
python run.py doctor
```

---

## Verifying it works

- **Briefing tab** should show a competitive scorecard with real numbers and KPI
  cards — this confirms the SEC/XBRL path.
- **Ask tab** → "What was NVDA's revenue in FY2024?" should return `$60,922,000,000`
  with a formula and accession — this confirms the agent + ground-truth path.
- **Evaluation:** `python run.py eval` writes `eval/results.json` — this is how you
  generate the honest correctness/hallucination numbers.

---

## Troubleshooting matrix

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ARGUS needs Python 3.10 or newer` | Old Python | Install 3.11/3.12; recreate venv: `python3.12 -m venv .venv` |
| `Python was not found` (Windows) | Not on PATH | Reinstall Python with "Add to PATH", or use `py -m venv .venv` |
| Ask tab: "Ollama is installed but not running" | Service stopped | `ollama serve`, or open the Ollama app |
| Ask tab: "Ollama is not installed" | No Ollama | Install from ollama.com, then `run.bat` again |
| "Model not pulled" | Models missing | `ollama pull qwen2.5:7b-instruct` and `ollama pull nomic-embed-text`, or accept the launcher's prompt |
| "Could not reach SEC EDGAR" | No internet / proxy | Check connection; allow `*.sec.gov` through firewall/proxy |
| SEC 403 | Missing User-Agent | Edit `USER_AGENT` in `src/sec_client.py` to include your email (SEC requires it) |
| SEC 429 | Rate-limited | Wait ~30s and re-run; the client self-throttles, so this is rare |
| `pip install` fails on a package | Build tools / network | Upgrade pip (`python -m pip install --upgrade pip`), retry; on Linux ensure `python3-dev` |
| Slow first launch | Building the index | One-time; subsequent launches are instant (idempotent ingest) |
| Re-ingests every launch | Interrupted prior build | Run `python run.py ingest` once to finish, then it skips |
| Out-of-memory running the model | <8 GB free RAM | Close apps, or use a smaller Ollama model (set `GEN_MODEL` in `src/agent.py`) |
| Dashboard didn't open | Browser/port | Open <http://localhost:8501> manually; or another app is on 8501 |

---

## Uninstall / reset

```bash
# Wipe generated data (filings, vector index, cache) — keeps your code:
rm -rf data/cache/* data/chroma/* data/filings/* data/universe.json   # PowerShell: Remove-Item -Recurse -Force
# Remove the environment entirely:
rm -rf .venv
# Remove pulled models (frees ~5 GB):
ollama rm qwen2.5:7b-instruct nomic-embed-text
```

All data is regenerated from public SEC sources on the next run.
