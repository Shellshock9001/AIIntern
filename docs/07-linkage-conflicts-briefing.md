# 07 — Analytical Layers: Linkage, Conflicts, Briefing

These three modules are where ARGUS goes "beyond retrieval" into the analytical depth
the brief most rewards. All build on the verified numbers from `metrics.py`.

---

## `linkage.py` — Quant-to-Narrative Linkage

The brief: *"when a metric moves materially, connect it to the relevant narrative."*

### Material-move detection

```python
detect_material_moves(ticker) -> list[MaterialMove]
```

Scans each metric's annual series for year-over-year swings beyond a per-metric
threshold:

```python
MATERIALITY = {
  "Gross Margin": 5.0, "Operating Margin": 7.0, "Net Margin": 7.0,
  "Revenue YoY Growth": 15.0,        # percentage points
  "Debt-to-Equity": 0.25,            # ratio units
  "Free Cash Flow": 0.40,            # 40% relative change
}
```

Each `MaterialMove` records both years, both values, the delta, direction
(improved/deteriorated, with leverage inverted since lower is better), and the
provenance of the later value. Sorted biggest-first.

### Linking to the explaining passage

```python
link_move_to_narrative(move, k=3) -> move  # fills move.narrative
```

Builds a query from the metric and direction (e.g. "reasons for decline in Net Margin
fiscal 2024 …"), retrieves from that company's filings, and prefers a passage whose
citation references the target fiscal year. Degrades gracefully (records why) if the
RAG index isn't built.

```python
explain_move_llm(move) -> str  # one-sentence linkage, constrained to the passage
```

The LLM writes a single sentence tying the verified number to the cited text — never
restating the number as if it computed it.

**Example output (real data):** *"INTC Net Margin fell from 3.1% to −35.3%
(FY2023→FY2024, −38.4 pts)"* linked to the MD&A passage on impairments and
restructuring charges, cited to the accession.

---

## `conflicts.py` — Data-Conflict Taxonomy

The brief: *"when filings disagree … does your system silently pick one, or surface the
conflict?"* We surface every type detectable from XBRL, and are honest about the rest.

```python
all_conflicts(ticker) -> {kind: [Conflict]}
```

| Kind | What it detects | Resolution |
|------|-----------------|------------|
| `restatement` | Same concept+period, values >0.5% apart across filings | Compute from latest-filed; show all |
| `scale_anomaly` | A value ~100×+ off **both** adjacent years (thousands-vs-units bug signature) | Flag high-severity |
| `unit_inconsistency` | A concept tagged under >1 monetary unit | Flag medium |
| `fiscal_misalignment` | Fiscal year not ending in December (comparability caveat) | Disclose the offset |
| `tag_switch` | Same economic line reported under multiple XBRL tags over time | Stitched in extraction; surfaced |

### Why scale-anomaly detection is careful

A naive "far from the median" check fires on every company that had a genuinely bad
year. Intel's FY2023 operating income was really $93M (a real collapse), not a units
error. So we require the anomaly to be ~100×+ off **both** adjacent years — the actual
signature of a thousands-vs-actual mistake — which lets real business swings through and
catches only true reporting errors. (This was a diagnosed false-positive; see WRITEUP.)

### The honest boundary: segment vs. consolidated

The `companyfacts` API returns **consolidated** figures only. Per-segment breakdowns
carry explicit XBRL member dimensions that live in the raw instance documents, not this
endpoint. Rather than fabricate segment conflicts, the Data Health tab states this
limitation outright. Honesty over a fake feature.

---

## `briefing.py` — Grounded Executive Briefing & Scorecard

The brief: *"would you trust this in front of an executive?"* This module produces the
one-glance answer.

### Cross-company scorecard

```python
cross_company_ranking(tickers=None) -> {metric: [ranked rows with accession]}
```

For each headline metric, ranks the active universe by latest value (leverage inverted),
each row carrying its accession. Produces the "who leads, on what, by how much" view —
e.g. NVDA leads every margin metric; INTC trails with negative operating margin.

### Per-company briefing

```python
build_company_brief(ticker) -> CompanyBrief
render_markdown_brief(ticker, with_llm=False) -> str
```

Assembles headline metrics (each with accession + formula), the top material moves, and
the data-health caveats into a single briefing. With `with_llm=True`, an LLM writes a
3-sentence summary **using only the verified facts provided** — it cannot introduce a
number not already present.

### Why this is the landing tab

It demonstrates the entire pipeline cooperating — verified metrics + material-move
detection + conflict caveats — in a form an executive would actually read, with every
figure traceable. It's the strongest single answer to "is this trustworthy?"

---

## How they compose

```
metrics.compute_metrics ──► linkage.detect_material_moves ──► (causal agent route)
        │                          │
        │                          └──► briefing top_moves
        ├──► briefing.cross_company_ranking ──► Briefing tab scorecard
        └──► conflicts.all_conflicts ──► Data Health tab + briefing caveats
```

All three are pure functions of the verified XBRL numbers, so they inherit the same
traceability guarantees as the metrics themselves.
