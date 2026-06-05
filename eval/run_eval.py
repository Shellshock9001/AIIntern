"""
run_eval.py — Evaluation harness for ARGUS FinDash.

Runs every labeled question through the REAL agent pipeline (no mocks) and reports:
  - Numeric correctness   : value within tolerance of the ground-truth label
  - Comparative accuracy   : identifies the correct leader
  - Citation accuracy      : narrative answers include >=1 source citation
  - Hallucination rate     : fraction of UNANSWERABLE questions that were
                             answered instead of refused (lower is better)

Usage (with Ollama running locally):
    python eval/run_eval.py
    python eval/run_eval.py --out eval/results.json

Every number this prints comes from a live run on your machine. Nothing here is
simulated; if Ollama is down, the narrative/self-check questions will error and
that is reported honestly rather than faked.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent import ask  # noqa: E402


def _extract_first_number(text: str) -> float | None:
    """
    Extract the answer's numeric VALUE, not an embedded fiscal year. The agent
    formats values as **7.37%**, **0.39x**, or **$96,676,000,000** with a unit
    marker, while years appear as 'FY2024'. Strategy:
      1. Prefer a number immediately followed by %, x, or wrapped after $.
      2. Skip bare 4-digit tokens in the 1800-2099 range (years).
    """
    t = text.replace("−", "-").replace(",", "")
    # 1) number attached to a unit/currency marker
    m = re.search(r"-?\$\s?-?\d+(?:\.\d+)?|-?\d+(?:\.\d+)?\s?%|-?\d+(?:\.\d+)?\s?x\b", t)
    if m:
        raw = m.group(0).replace("$", "").replace("%", "").replace("x", "").strip()
        try:
            return float(raw)
        except ValueError:
            pass
    # 2) first number that is not a plausible calendar year
    for m in re.finditer(r"-?\d+(?:\.\d+)?", t):
        val = float(m.group(0))
        if val == int(val) and 1800 <= val <= 2099:
            continue  # looks like a year
        return val
    return None


def _has_citation(out: dict) -> bool:
    if any(e.get("type") == "chunk" for e in out.get("evidence", [])):
        return True
    return bool(re.search(r"\[\d+\]", out.get("answer", "")))


def grade(item: dict, out: dict) -> dict:
    typ = item["type"]
    refused = bool(out.get("refused"))
    ans = out.get("answer", "")
    res = {"id": item["id"], "type": typ, "refused": refused, "passed": False,
           "detail": ""}

    if typ == "unanswerable":
        res["passed"] = refused
        res["detail"] = "correctly refused" if refused else "HALLUCINATION: answered"
        return res

    if refused:
        res["detail"] = "unexpected refusal"
        return res

    if typ == "numeric":
        got = _extract_first_number(ans)
        exp = item["expect_value"]
        if got is None:
            res["detail"] = "no number parsed from answer"
            return res
        tol = abs(exp) * item.get("tolerance_pct", 1.0) / 100 or 0.01
        ok = abs(got - exp) <= tol
        res["passed"] = ok
        res["detail"] = f"got {got}, expected {exp} (tol ±{tol:.4g})"
        return res

    if typ == "comparative":
        leader = item["expect_leader"]
        # Leader should be named as the winner near 'leader'/'outperform'.
        ok = leader.upper() in ans.upper()
        # Stronger check: leader appears in the explicit "Leader:" line if present.
        m = re.search(r"leader[:\s]+([A-Z]{2,5})", ans, re.IGNORECASE)
        if m:
            ok = m.group(1).upper() == leader.upper()
        res["passed"] = ok
        res["detail"] = f"expected leader {leader}; {'ok' if ok else 'mismatch'}"
        return res

    if typ == "narrative":
        kw = item.get("expect_keywords", [])
        kw_hit = sum(1 for k in kw if k.lower() in ans.lower())
        cite_ok = _has_citation(out) if item.get("expect_citation") else True
        ok = kw_hit >= max(1, len(kw) // 2) and cite_ok
        res["passed"] = ok
        res["citation_ok"] = cite_ok
        res["detail"] = f"keywords {kw_hit}/{len(kw)}, citation={cite_ok}"
        return res

    res["detail"] = "unknown type"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(Path(__file__).parent / "questions.json"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "results.json"))
    args = ap.parse_args()

    qset = json.loads(Path(args.questions).read_text())["questions"]
    results = []
    for item in qset:
        try:
            out = ask(item["q"])
        except Exception as e:  # report errors honestly, don't fake a pass
            results.append({"id": item["id"], "type": item["type"],
                            "passed": False, "detail": f"ERROR: {e}",
                            "refused": None})
            print(f"[{item['id']:9s}] ERROR  {e}")
            continue
        r = grade(item, out)
        results.append(r)
        flag = "PASS" if r["passed"] else "FAIL"
        print(f"[{r['id']:9s}] {flag:4s} {r['type']:12s} {r['detail']}")

    # Aggregate.
    def rate(pred):
        items = [r for r in results if pred(r)]
        if not items:
            return None
        return sum(r["passed"] for r in items) / len(items)

    by_type = {}
    for t in ["numeric", "comparative", "narrative", "unanswerable"]:
        rr = rate(lambda r, t=t: r["type"] == t)
        if rr is not None:
            by_type[t] = round(rr, 3)

    unans = [r for r in results if r["type"] == "unanswerable"]
    hallucinated = [r for r in unans if r.get("refused") is False]
    halluc_rate = (len(hallucinated) / len(unans)) if unans else None

    nar = [r for r in results if r["type"] == "narrative"]
    cite_rate = (sum(bool(r.get("citation_ok")) for r in nar) / len(nar)
                 if nar else None)

    overall = sum(r["passed"] for r in results) / len(results)
    summary = {
        "overall_pass_rate": round(overall, 3),
        "by_type": by_type,
        "hallucination_rate_on_unanswerable": (round(halluc_rate, 3)
                                               if halluc_rate is not None else None),
        "citation_accuracy_narrative": (round(cite_rate, 3)
                                        if cite_rate is not None else None),
        "n_questions": len(results),
    }
    Path(args.out).write_text(json.dumps(
        {"summary": summary, "results": results}, indent=2))

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
