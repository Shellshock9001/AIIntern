# 10 — Operations: Running, Setup, and the UI

How to run and operate the dashboard day-to-day, and how the UI/visual layer is built.

## One command

```bash
# Windows
run.bat

# macOS / Linux
make           # or: python run.py
```

`run.py` is the orchestrator. The default action (`up`) does, in order:
1. **Silent preflight** — checks Python deps, Ollama, models, and corpus state
   *quietly*; it only prints a line if something is wrong, or a single
   `✓ Corpus ready (… chunks) · N companies · launching` when all is well.
2. **Ingest only if needed** — builds the narrative index just for companies not
   already indexed. A complete corpus launches in ~2 seconds with no re-ingest.
3. **Launch** the Streamlit dashboard.

### Subcommands

```bash
python run.py doctor          # full verbose environment report
python run.py ingest          # (re)build corpus, skipping what exists
python run.py ingest --refresh  # full rebuild from scratch
python run.py eval            # run the evaluation suite
python run.py up              # explicit default (doctor + ingest-if-needed + launch)
```

## The doctor

`doctor(verbose=…)` checks four things and, in quiet mode, speaks only on problems:
- **Python deps** — `pip install -r requirements.txt` if missing.
- **Ollama** — reachable at `localhost:11434`. Without it, numeric features work;
  Ask/Insights narrative is limited.
- **Models** — `qwen2.5:7b-instruct` and `nomic-embed-text` pulled.
- **Corpus** — how many chunks are indexed and for which tickers.

The standalone `python run.py doctor` is verbose (the full ✓/✗ report) for debugging.
The launch path uses the quiet variant so you don't see a wall of text every time.

## Idempotent ingest

`ingest.run_ingest(tickers, refresh)` skips any filing already in the vector store
(`rag.indexed_filings()` tracks `(ticker, accession)` pairs). Consequences:
- Re-running ingest after it completes does nothing — it's safe and instant.
- Adding a company indexes only that company.
- `--refresh` drops the collection and rebuilds everything.

This is why the app launches fast once built: there's no work to repeat.

## Performance architecture (how it stays fast)

The app is built so interactions feel instant despite Streamlit's full-script-rerun
model:

- **`st.fragment` isolation.** The sidebar universe editor, the per-company
  briefing, the Compare view, and the Ask panel are each wrapped in `@st.fragment`.
  Interacting with one (switching company, changing the compared metric, asking a
  question) reruns **only that fragment**, not the whole app or the other five tabs.
- **Data-layer disk cache.** `metrics.compute_metrics` is cached to disk with a TTL
  (`cache.py`), so a cold start is fast and every consumer (app, agent, eval)
  benefits. Surgical invalidation drops only a changed company.
- **Parallel SEC fetches.** `load_all` pulls the universe with a `ThreadPoolExecutor`.
- **Singleton ChromaDB client + cached corpus status.** The vector-store client is
  created once per process (not per call), and `corpus_status()` is cached, so the
  one-time ChromaDB init doesn't repeat on every rerun.
- **Heuristic-first agent routing.** Common questions skip the LLM (see
  [06-agent.md](06-agent.md)), so numeric/comparison/ranking answers are near-instant.

## Adding companies (type-ahead)

The sidebar has a searchable dropdown over all ~10,000 SEC filers (ticker or company
name). Selecting a company adds it to the universe and refreshes; the metric data is
computed on demand and cached. Removing a company invalidates only its cache entry.

## The UI / visual system (`theme.py`)

The dashboard's look is centralized in `theme.py`:
- **Palette** — dark finance-terminal colors with an NVIDIA-style green accent; semantic
  colors for positive/negative/warning; an 8-color series palette for charts.
- **CSS injection** (`inject_css`) — styles tabs, cards, KPI tiles, pills, code spans,
  buttons, the sidebar, and dataframes; adds fade/slide animations so content (and
  company switches) animate on rerun.
- **Icons** (`icon`, `header`, `ICONS`) — crisp inline **SVG line icons** (not emojis),
  drawn with `currentColor` and the accent palette. `header(name, text, level)` renders a
  section heading with a leading icon. Tab labels use Streamlit's Material Symbols
  (`:material/dashboard:`, `:material/forum:`, etc.).
- **Plotly styling** (`style_fig`) — applies the house template: transparent background,
  hairline gridlines, horizontal legend, thick lines, series colors. Call it on any
  figure before `st.plotly_chart`.

To restyle the whole app, edit the palette constants and `inject_css` in one place.

### Why icons instead of emojis

Emojis render inconsistently across platforms and read as informal. The SVG set is
crisp at any size, matches the palette, and looks like a product rather than a prototype.
Add a new icon by dropping an SVG `path`/`shape` body into the `ICONS` dict and
referencing it via `theme.icon("name")` or `theme.header("name", "Title")`.

## Streamlit gotchas we handle

- **Stale content on selectbox change.** Rendering dynamic content into pre-created
  `st.columns` containers can show the previous run's values. We render per-company
  content in the main flow instead, so the selected company always drives the view.
- **Charts not refreshing.** Plotly charts get a content-keyed `key` (e.g.
  `cmp_chart_{metric}`) so they re-render cleanly when the selection changes.
- **No browser storage in artifacts** isn't relevant here (this is a real Streamlit app),
  but we keep all state in `st.session_state` and the on-disk universe/corpus.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| "Cannot reach Ollama" in Ask/Insights | Start Ollama (`ollama serve` or open the app); numeric tabs still work. |
| Ask returns a refusal for a real metric | The quantity may not be reported by that company (e.g. a bank has no gross margin). Check Data Health / coverage. |
| Re-ingests every launch | Corpus was incomplete (interrupted build). Run `python run.py ingest` once to finish; subsequent launches skip it. |
| Duplicate-ID error during ingest | Fixed — chunk IDs are globally unique; if you see it, you're on an old build. |
| Chart/briefing won't switch, or feels slow | Fixed via `st.fragment` isolation + disk caching; ensure you're on the current build. |
| Added company doesn't appear | It's added to the universe immediately; its numbers compute on first view (cached after). For the Ask/Insights narrative on a new company, build its index with `python run.py ingest`. |
| Want a different sector | Sidebar → search and add tickers (type-ahead), or `config.set_universe([...], "Sector")`, then `python run.py ingest`. |
