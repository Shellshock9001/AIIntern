"""
app.py — ARGUS FinDash Streamlit dashboard.

Tabs:
  1. Ask         — agentic Q&A with citations and a reasoning trace
  2. Compare     — metrics charted across companies and over time
  3. Drill-down  — every figure with its formula + source accession (provenance)
  4. Data Health — restatement conflicts + tag coverage (the "messy reality" view)

Run:  streamlit run src/app.py
Requires Ollama running locally for the Ask tab (charts/drill-down work offline
from cached XBRL facts).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import compute_metrics  # noqa: E402
from sec_client import resolve_cik  # noqa: E402

TICKERS = ["NVDA", "AMD", "INTC", "AVGO"]
SECTOR = "Semiconductors"

st.set_page_config(page_title="ARGUS FinDash", layout="wide",
                   initial_sidebar_state="expanded")


@st.cache_data(show_spinner="Loading XBRL facts from SEC…")
def load_all():
    data = {}
    for tk in TICKERS:
        metrics, facts, conflicts = compute_metrics(tk)
        data[tk] = {"metrics": metrics, "facts": facts, "conflicts": conflicts,
                    "title": resolve_cik(tk)["title"]}
    return data


def metrics_df(data) -> pd.DataFrame:
    rows = []
    for tk, d in data.items():
        for m in d["metrics"]:
            rows.append({"ticker": tk, "metric": m.name, "fy": m.fy,
                         "value": m.value, "unit": m.unit, "formula": m.formula,
                         "inputs": "; ".join(
                             f"{p.concept}={p.value:,.0f} (acc {p.accession})"
                             for p in m.inputs)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
st.title("ARGUS FinDash")
st.caption(f"Grounded financial analysis over SEC filings · {SECTOR}: "
           f"{', '.join(TICKERS)}")

data = load_all()
df = metrics_df(data)

tab_ask, tab_cmp, tab_drill, tab_health = st.tabs(
    ["💬 Ask", "📊 Compare", "🔎 Drill-down", "🩺 Data Health"])

# ---- Ask tab --------------------------------------------------------------
with tab_ask:
    st.subheader("Ask the filings")
    st.caption("Numeric questions answer from XBRL ground truth; narrative "
               "questions retrieve filing text. Unanswerable questions are refused.")
    q = st.text_input("Question",
                      placeholder="e.g. Compare operating margin between NVDA and INTC")
    if st.button("Ask", type="primary") and q:
        try:
            from agent import ask
            with st.spinner("Routing · retrieving · self-checking…"):
                out = ask(q)
            if out.get("refused"):
                st.warning(out["answer"])
            else:
                st.markdown(out["answer"])
            with st.expander("Evidence / citations"):
                for e in out.get("evidence", []):
                    if e["type"] == "metric":
                        st.markdown(f"- **{e.get('value','')}** — {e['formula']} "
                                    f"· `{e['citation']}`")
                    else:
                        st.markdown(f"- *{e['section']}* — {e['citation']}")
                        st.caption(e["text"])
            with st.expander("Reasoning trace"):
                st.code("\n".join(out.get("trace", [])))
        except RuntimeError as ex:
            st.error(str(ex))

# ---- Compare tab ----------------------------------------------------------
with tab_cmp:
    st.subheader("Comparative analysis")
    metric_choices = sorted(df["metric"].unique())
    metric = st.selectbox("Metric", metric_choices,
                          index=metric_choices.index("Gross Margin")
                          if "Gross Margin" in metric_choices else 0)
    sub = df[df["metric"] == metric].sort_values("fy")
    if not sub.empty:
        unit = sub["unit"].iloc[0]
        fig = go.Figure()
        for tk in TICKERS:
            s = sub[sub["ticker"] == tk]
            if not s.empty:
                fig.add_trace(go.Scatter(x=s["fy"], y=s["value"], mode="lines+markers",
                                         name=tk))
        ylab = {"%": "Percent", "x": "Ratio (x)", "USD": "USD"}.get(unit, unit)
        fig.update_layout(height=460, xaxis_title="Fiscal Year",
                          yaxis_title=f"{metric} ({ylab})", hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)

        latest = sub.sort_values("fy").groupby("ticker").tail(1)
        st.markdown("**Latest available fiscal year per company**")
        show = latest[["ticker", "fy", "value", "unit"]].copy()
        show["value"] = show.apply(
            lambda r: f"${r['value']:,.0f}" if r["unit"] == "USD"
            else f"{r['value']:,.2f}{r['unit']}", axis=1)
        st.dataframe(show.rename(columns={"ticker": "Company", "fy": "FY",
                                          "value": metric}).drop(columns="unit"),
                     hide_index=True, use_container_width=True)

# ---- Drill-down tab -------------------------------------------------------
with tab_drill:
    st.subheader("Provenance — every number traces to a filing")
    c1, c2 = st.columns(2)
    tk_sel = c1.selectbox("Company", TICKERS)
    msel = c2.selectbox("Metric", sorted(df["metric"].unique()), key="drill_m")
    view = df[(df["ticker"] == tk_sel) & (df["metric"] == msel)].sort_values(
        "fy", ascending=False)
    for _, r in view.iterrows():
        val = (f"${r['value']:,.0f}" if r["unit"] == "USD"
               else f"{r['value']:,.2f}{r['unit']}")
        with st.expander(f"FY{int(r['fy'])} — {msel}: {val}"):
            st.markdown(f"**Formula:** `{r['formula']}`")
            st.markdown(f"**Inputs (with source accession):** {r['inputs']}")

# ---- Data Health tab ------------------------------------------------------
with tab_health:
    st.subheader("Restatements & data conflicts")
    st.caption("When two 10-K filings report different values for the same period, "
               "we compute from the latest-filed but surface the disagreement here "
               "rather than hiding it.")
    any_conflict = False
    for tk, d in data.items():
        if d["conflicts"]:
            any_conflict = True
            st.markdown(f"### {tk} — {len(d['conflicts'])} conflict(s)")
            for c in d["conflicts"]:
                rows = pd.DataFrame(c["values"])
                st.markdown(f"**{c['concept']} · period ending {c['period_end']}** "
                            f"(using {c['chosen']})")
                st.dataframe(rows, hide_index=True, use_container_width=True)
    if not any_conflict:
        st.success("No material restatement conflicts detected in the 10-K corpus.")
