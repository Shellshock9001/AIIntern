# 08 — Evaluation Framework

The brief asks for a labeled question set and honest reporting of **answer correctness,
citation accuracy, and hallucination rate on unanswerable questions** — valuing
transparent evaluation over perfect scores.

## Files

- `eval/questions.json` — the labeled set.
- `eval/run_eval.py` — runs every question through the **real** agent (no mocks) and
  scores it; writes `eval/results.json`.

## The labeled set

Five categories, each testing a different behavior:

| Type | Tests | Pass condition |
|------|-------|----------------|
| `numeric` | exact figure correctness | value within tolerance of the verified label |
| `comparative` | correct ranking | names the right leader |
| `narrative` | grounded prose + citation | expected keywords present **and** a citation attached |
| `causal` | move detected + linked | states the verified move (citation when Ollama up) |
| `trend` | direction over years | states the correct direction + series |
| `unanswerable` | refusal discipline | **must refuse**; answering = hallucination |

The numeric labels were computed from XBRL ground truth and verified at authoring time
(e.g. NVDA FY2024 gross margin = 72.72%). The `unanswerable` set includes: an impossible
year (FY1850), a forward-looking ask (FY2030), a privacy item (CEO home address), a
metric with no usable inputs (AVGO debt-to-equity FY2024), and an out-of-corpus company
(TSMC).

## How scoring works (`run_eval.py`)

```python
ask(question) -> state    # the real pipeline
grade(item, state) -> {passed, detail, …}
```

- **numeric**: `_extract_first_number` pulls the value from the answer (carefully
  skipping the fiscal-year token — a real bug that was fixed: "FY2024" must not be read
  as the value), compares to `expect_value` within `tolerance_pct`.
- **comparative**: checks the named leader (prefers the explicit "Leader:" line).
- **narrative**: counts expected keywords and verifies a citation is attached.
- **causal**: verifies the answer states the move (keywords).
- **unanswerable**: `passed = refused`. Answering one is counted as a hallucination.

## The metrics reported

```json
{
  "overall_pass_rate": …,
  "by_type": {"numeric": …, "comparative": …, "narrative": …, "unanswerable": …},
  "hallucination_rate_on_unanswerable": …,   // fraction of unanswerables ANSWERED (lower better)
  "citation_accuracy_narrative": …,
  "n_questions": …
}
```

## Running it

```bash
python run.py eval         # or: python eval/run_eval.py
```

Requires Ollama running for the narrative/causal/self-check questions. The numeric,
comparative, and numeric-refusal cases are deterministic and pass without Ollama.

> **Honesty note:** if Ollama is down, the Ollama-dependent questions are reported as
> errors, not silently passed or faked. Every number in `results.json` comes from a real
> run on your machine. Paste that summary into the README/WRITEUP — do not copy numbers
> you haven't generated.

## What the numbers mean (interpretation guidance)

- **Numeric ~100%** is expected and not impressive on its own — it's deterministic by
  design (XBRL, not LLM). The point is that it's *structurally* correct, not luckily so.
- **Hallucination rate** is the headline trust metric. The architecture aims for ~0 on
  the numeric-refusal cases (deterministic) and relies on the self-check for the
  narrative ones.
- **Citation accuracy** measures whether narrative answers actually attach sources.

A perfect score is less interesting than an honest one: if a category underperforms,
that's a finding to report, not hide.

## Extending the eval

Add an object to `questions.json`:

```json
{"id": "num-07", "type": "numeric",
 "q": "What was AMD's R&D intensity in fiscal 2024?",
 "expect_value": 23.5, "tolerance_pct": 1.0, "expect_refusal": false}
```

For a new label, compute the ground truth from `metrics.py` first (don't eyeball it),
then encode it. For `unanswerable`, ensure it's genuinely unanswerable from the corpus.
