"""
app.py — ARGUS FinDash dashboard.

Six views:
  Briefing    — competitive scorecard + per-company executive briefing (XBRL only)
  Ask         — agentic Q&A: exact figures, comparisons, trends, causal, narrative
  Compare     — any metric charted across the universe and over time
  Insights    — material moves linked to the filing passage that explains them
  Drill-down  — every figure with formula + source accession + statement deep-link
  Data Health — restatement / scale / fiscal / tag conflicts, surfaced honestly

Run:  streamlit run src/app.py   (or:  python run.py)
Numeric views work from cached XBRL with no Ollama. Ask/Insights narrative needs it.
"""
from __future__ import annotations

import sys
if sys.version_info < (3, 10):
    import streamlit as _st
    _st.error("ARGUS needs Python 3.10+. Please recreate the virtualenv with a "
              "newer Python (e.g. `python3.12 -m venv .venv`).")
    _st.stop()

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import compute_metrics  # noqa: E402
from sec_client import resolve_cik  # noqa: E402
import config  # noqa: E402
import theme  # noqa: E402
import cache  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

st.set_page_config(page_title="ARGUS FinDash", layout="wide",
                   initial_sidebar_state="expanded")
theme.inject_css()


# --------------------------------------------------------------------------
# Cached data accessors (defined before the sidebar, which calls .clear()).
# Caching strategy lives in the DATA layer (metrics.compute_metrics is disk-cached
# with a TTL); these thin Streamlit memos just avoid re-deserializing within a
# session and fetch the universe in PARALLEL (SEC calls are I/O-bound).
# --------------------------------------------------------------------------
@st.cache_data(show_spinner="Loading XBRL facts from SEC…")
def load_all(tickers: tuple):
    def one(tk):
        metrics, facts, conflicts = compute_metrics(tk)
        return tk, {"metrics": metrics, "facts": facts, "conflicts": conflicts,
                    "title": resolve_cik(tk)["title"]}
    data = {}
    # Parallel fetch — ~4x faster than serial for a 4-company universe.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(tickers)))) as ex:
        for tk, d in ex.map(one, tickers):
            data[tk] = d
    return data


@st.cache_data(show_spinner=False)
def cached_brief(tk: str):
    from briefing import build_company_brief
    return build_company_brief(tk)


@st.cache_data(show_spinner=False)
def cached_ranking(tickers: tuple):
    from briefing import cross_company_ranking
    return cross_company_ranking(list(tickers))


@st.cache_data(show_spinner=False)
def cached_moves(tk: str, n: int):
    from linkage import detect_material_moves
    return detect_material_moves(tk)[:n]


@st.cache_data(show_spinner=False)
def cached_conflicts(tk: str):
    from conflicts import all_conflicts
    return all_conflicts(tk)


# --------------------------------------------------------------------------
# Sidebar — dynamic company universe (no hardcoded list)
# --------------------------------------------------------------------------
@st.fragment
def sidebar_universe():
    """Isolated fragment: typing/adding/removing reruns ONLY this, not the whole
    app — so the universe editor feels instant and the type-ahead is live."""
    st.markdown(theme.header("universe", "Company universe", 3),
                unsafe_allow_html=True)
    st.caption("Search ~10,000 SEC filers by ticker or name. Analysis adapts to "
               "whatever each company reports.")

    for tk in config.active_tickers():
        c1, c2 = st.columns([6, 1])
        c1.markdown(f"**{tk}** · {config.company_title(tk)[:22]}")
        if c2.button("✕", key=f"rm_{tk}", help=f"Remove {tk}"):
            config.remove_ticker(tk)
            cache.invalidate("metrics", tk)
            load_all.clear()
            st.rerun()  # full rerun: the active universe changed app-wide

    # Searchable dropdown of every SEC filer — Streamlit's selectbox filters as
    # you type. Selecting a company adds it via a callback that resets the box,
    # so it can't get stuck on a stale selection.
    @st.cache_data(show_spinner=False)
    def _all_company_options():
        from sec_client import load_ticker_map
        tmap = load_ticker_map()
        return {f"{tk} · {info['title']}": tk for tk, info in tmap.items()}

    options = _all_company_options()
    active = set(config.active_tickers())
    PLACEHOLDER = "Search to add a company…"
    choices = [PLACEHOLDER] + [lbl for lbl, tk in options.items() if tk not in active]

    def _do_add():
        sel = st.session_state.get("add_pick", PLACEHOLDER)
        st.session_state["add_pick"] = PLACEHOLDER   # reset immediately
        if sel and sel != PLACEHOLDER:
            tk = options.get(sel)
            if tk:
                ok, msg = config.add_ticker(tk)
                if ok:
                    load_all.clear()
                    st.session_state["_add_msg"] = ("ok", f"Added {tk}.")
                else:
                    st.session_state["_add_msg"] = ("warn", msg)

    st.selectbox("Add company", choices, index=0, key="add_pick",
                 on_change=_do_add)
    _msg = st.session_state.pop("_add_msg", None)
    if _msg:
        (st.success if _msg[0] == "ok" else st.warning)(_msg[1])

    if st.button("Reset to semiconductors"):
        config.set_universe(["NVDA", "AMD", "INTC", "AVGO"], "Semiconductors")
        load_all.clear()
        st.rerun()

    st.divider()
    st.caption("Numbers come from SEC XBRL (filer-tagged). Narrative answers are "
               "retrieved from filing text and cited, or refused.")


with st.sidebar:
    sidebar_universe()


TICKERS = config.active_tickers()
SECTOR = config.sector_label()


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


def fmt_val(v, unit):
    if v is None:
        return "—"
    if unit == "USD":
        return f"${v:,.0f}"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "x":
        return f"{v:.2f}x"
    if unit == "USD/sh":
        return f"${v:.2f}"
    return f"{v:,.2f}"


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
st.markdown(f"# ARGUS FinDash")
st.markdown(f"<span style='color:{theme.MUTED}'>Grounded financial analysis over "
            f"SEC filings — {SECTOR}: {', '.join(TICKERS)}</span>",
            unsafe_allow_html=True)

if not TICKERS:
    st.info("Your company universe is empty. Use the sidebar to search and add a "
            "company (any of ~10,000 SEC filers) to begin.")
    st.stop()

data = load_all(tuple(TICKERS))
df = metrics_df(data)

# Corpus status (only the narrative tabs need the vector index). Cached because
# initializing the ChromaDB client is slow (~12s cold) and this runs on every
# rerun — without caching it blocks EVERY company/metric switch. ttl keeps it
# fresh enough to notice a newly built index.
from rag import corpus_status  # noqa: E402


@st.cache_data(ttl=30, show_spinner=False)
def _corpus_status_cached():
    return corpus_status()


_cs = _corpus_status_cached()
_active, _have = set(t.upper() for t in TICKERS), set(_cs["tickers"])
if _cs["chunks"] == 0 or not _active.issubset(_have):
    miss = sorted(_active - _have) or "all companies"
    with st.container():
        st.markdown(
            f"<div class='argus-card'>The narrative index isn't built for "
            f"<b>{miss}</b>. The numeric views (Briefing, Compare, Drill-down, "
            f"Data Health) work now. The <b>Ask</b> and <b>Insights</b> narrative "
            f"features need it.</div>", unsafe_allow_html=True)
        if st.button("Build narrative index", type="primary"):
            try:
                from ingest import run_ingest
                with st.status("Building…", expanded=True) as s:
                    summ = run_ingest(list(TICKERS), log=lambda m: st.write(m))
                    s.update(label=f"Indexed {summ['new_chunks']} chunks.",
                             state="complete")
                st.cache_data.clear()
                st.rerun()
            except RuntimeError as e:
                st.error(str(e))

tab_brief, tab_ask, tab_cmp, tab_insight, tab_drill, tab_health = st.tabs(
    [":material/dashboard: Briefing",
     ":material/forum: Ask",
     ":material/show_chart: Compare",
     ":material/lightbulb: Insights",
     ":material/search: Drill-down",
     ":material/monitoring: Data Health"])

# ==========================================================================
# BRIEFING
# ==========================================================================
with tab_brief:
    from briefing import (cross_company_ranking, build_company_brief,
                          render_markdown_brief, _fmt as _bfmt)

    st.markdown(theme.header("scorecard","Competitive scorecard",3), unsafe_allow_html=True)
    st.caption("Latest fiscal year per company. Every figure is filer-tagged XBRL; "
               "nothing here is model-generated.")

    ranking = cached_ranking(tuple(TICKERS))
    # Build a tidy matrix: rows = metrics, cols = tickers, plus a Leader column.
    rows = []
    for metric, rws in ranking.items():
        rec = {"Metric": metric}
        for r in rws:
            rec[r["ticker"]] = _bfmt(r["value"], r["unit"])
        rec["Leader"] = rws[0]["ticker"] if rws else "—"
        rows.append(rec)
    score_df = pd.DataFrame(rows).set_index("Metric")
    cols = [c for c in TICKERS if c in score_df.columns] + ["Leader"]
    st.dataframe(score_df[cols], use_container_width=True)

    wins = {}
    for _, rws in ranking.items():
        if rws:
            wins[rws[0]["ticker"]] = wins.get(rws[0]["ticker"], 0) + 1
    pills = " ".join(theme.pill(f"{tk}: {n} lead{'s' if n>1 else ''}",
                                "accent" if n == max(wins.values()) else "")
                     for tk, n in sorted(wins.items(), key=lambda x: -x[1]))
    st.markdown(f"**Headline-metric leadership:** {pills}", unsafe_allow_html=True)

    st.divider()

    st.markdown(theme.header("briefing","Per-company briefing",4), unsafe_allow_html=True)

    @st.fragment
    def per_company_briefing():
        # Isolated fragment: changing the company selectbox reruns ONLY this block,
        # not the whole app — so the switch is instant with a clean fade.
        bt = st.selectbox("Company", config.active_tickers(), key="brief_tk",
                          label_visibility="collapsed")
        brief = cached_brief(bt)

        title = f"{bt} — {config.company_title(bt)}"
        subt = f"Executive briefing · latest FY{brief.latest_fy}"
        cards = [(name, _bfmt(h["value"], h["unit"]), f"FY{h['fy']}", "")
                 for name, h in brief.headline.items()]
        grid = theme.kpi_grid(cards, cols=4)

        moves_html = ""
        if brief.top_moves:
            rows = []
            for mv in brief.top_moves:
                tone = "pos" if mv.direction == "improved" else "neg"
                ic = theme.icon("up" if mv.direction == "improved" else "down", 16,
                                theme.POS if mv.direction == "improved" else theme.NEG)
                rows.append(f'<div style="margin:6px 0">{ic}{theme.pill(mv.direction, tone)}'
                            f'&nbsp; {mv.headline()}</div>')
            moves_html = (f'<h4 style="margin-top:18px">Most material recent moves</h4>'
                          + "".join(rows))

        caveats_html = ""
        if brief.caveats:
            lis = "".join(f"<li>{c}</li>" for c in brief.caveats)
            caveats_html = (f'<h4 style="margin-top:18px">Data caveats</h4>'
                            f'<ul style="color:{theme.MUTED}">{lis}</ul>')

        st.markdown(
            f"<div class='fade-in' data-co='{bt}' style='animation:argusFade .35s ease-out'>"
            f"<h3 style='margin-bottom:2px'>{title}</h3>"
            f"<div style='color:{theme.MUTED};margin-bottom:14px'>{subt}</div>"
            f"{grid}{moves_html}{caveats_html}"
            f"</div>",
            unsafe_allow_html=True)

        with st.expander("Provenance — formulas and source accessions"):
            st.markdown(render_markdown_brief(bt, with_llm=False))

    per_company_briefing()


# ==========================================================================
# ASK
# ==========================================================================
with tab_ask:
    st.markdown(theme.header("ask","Ask the filings",3), unsafe_allow_html=True)
    st.caption("Exact figures and derived metrics answer from XBRL ground truth. "
               "Comparisons, rankings, and trends are computed deterministically. "
               "Narrative questions retrieve filing text and cite it. Unanswerable "
               "questions are refused, not guessed.")

    @st.fragment
    def ask_view():
        # Isolated: asking reruns ONLY this block, so the rest of the app (six
        # tabs, corpus check) doesn't re-execute on every question.
        examples = ["What was NVDA's revenue in FY2024?",
                    "Rank the companies by return on equity.",
                    "Has NVDA's asset turnover improved over the past 3 years?",
                    "Why did NVDA's revenue growth drop between FY2022 and FY2023?"]
        ecols = st.columns(len(examples))
        picked = None
        for c, ex in zip(ecols, examples):
            if c.button(ex, key=f"ex_{ex[:12]}"):
                picked = ex
        q = st.text_input("Question", value=picked or "",
                          placeholder="Ask anything about the companies' financials…")
        if (st.button("Ask", type="primary") or picked) and q:
            try:
                from agent import ask
                with st.status("Working…", expanded=True) as status:
                    st.write("Parsing your question…")
                    out = ask(q)
                    route = next((t for t in out.get("trace", []) if "route=" in t), "")
                    st.write(f"Resolved: {route or 'done'}")
                    status.update(label="Done", state="complete", expanded=False)
                refused = out.get("refused")
                border = theme.NEG if refused else theme.ACCENT
                st.markdown(
                    f"<div class='argus-card fade-in' style='border-left:3px solid "
                    f"{border};animation:argusFade .4s cubic-bezier(.2,.7,.2,1)'>"
                    f"{out['answer']}</div>", unsafe_allow_html=True)
                ev = out.get("evidence", [])
                if ev:
                    with st.expander("Evidence and citations", expanded=True):
                        for e in ev:
                            if e["type"] == "metric":
                                st.markdown(f"- **{e.get('value','')}** · "
                                            f"`{e.get('formula','')}` · {e['citation']}")
                            else:
                                st.markdown(f"- *{e.get('section','')}* — {e['citation']}")
                                st.caption(e.get("text", "")[:400])
                with st.expander("Reasoning trace"):
                    st.code("\n".join(out.get("trace", [])))
            except RuntimeError as ex:
                st.error(str(ex))

    ask_view()

# ==========================================================================
# COMPARE
# ==========================================================================
with tab_cmp:
    st.markdown(theme.header("compare","Comparative analysis",3), unsafe_allow_html=True)

    @st.fragment
    def compare_view():
        # Isolated: switching the metric reruns ONLY this chart/table, instantly.
        metric_choices = sorted(df["metric"].unique())
        default = metric_choices.index("Gross Margin") if "Gross Margin" in metric_choices else 0
        metric = st.selectbox("Metric", metric_choices, index=default)
        sub = df[df["metric"] == metric].sort_values("fy")
        if sub.empty:
            st.info("No data for this metric.")
            return
        unit = sub["unit"].iloc[0]
        latest = sub.sort_values("fy").groupby("ticker").tail(1)
        kpi_cols = st.columns(len(TICKERS))
        for col, tk in zip(kpi_cols, TICKERS):
            s = sub[sub["ticker"] == tk].sort_values("fy")
            if s.empty:
                col.markdown(theme.kpi_card(tk, "—", "no data"),
                             unsafe_allow_html=True)
                continue
            cur = s.iloc[-1]
            sub_txt, tone = "", ""
            if len(s) >= 2:
                prev = s.iloc[-2]
                d = cur["value"] - prev["value"]
                tone = "pos" if d >= 0 else "neg"
                arrow = "▲" if d >= 0 else "▼"
                if unit in ("%",):
                    sub_txt = f"{arrow} {abs(d):.1f} pts vs FY{int(prev['fy'])}"
                elif unit == "x":
                    sub_txt = f"{arrow} {abs(d):.2f}x vs FY{int(prev['fy'])}"
                else:
                    sub_txt = f"{arrow} {abs(d)/abs(prev['value'])*100:.0f}% vs FY{int(prev['fy'])}" if prev['value'] else ""
            col.markdown(theme.kpi_card(tk, fmt_val(cur["value"], unit),
                                        sub_txt, tone), unsafe_allow_html=True)

        fig = go.Figure()
        for tk in TICKERS:
            s = sub[sub["ticker"] == tk]
            if not s.empty:
                fig.add_trace(go.Scatter(x=s["fy"], y=s["value"],
                                         mode="lines+markers", name=tk))
        ylab = {"%": "Percent", "x": "Ratio (x)", "USD": "USD"}.get(unit, unit)
        theme.style_fig(fig, height=440, ytitle=f"{metric} ({ylab})")
        fig.update_layout(xaxis_title="Fiscal Year")
        st.plotly_chart(fig, use_container_width=True,
                        config={"displayModeBar": False},
                        key=f"cmp_chart_{metric}")

        st.markdown(theme.header("calendar","Latest available fiscal year",4), unsafe_allow_html=True)
        show = latest[["ticker", "fy", "value", "unit"]].copy()
        show["value"] = show.apply(lambda r: fmt_val(r["value"], r["unit"]), axis=1)
        show = show.rename(columns={"ticker": "Company", "fy": "FY",
                                    "value": metric}).drop(columns="unit")
        st.dataframe(show.sort_values("FY", ascending=False),
                     hide_index=True, use_container_width=True)

    compare_view()

# ==========================================================================
# INSIGHTS
# ==========================================================================
with tab_insight:
    from linkage import detect_material_moves, link_move_to_narrative, explain_move_llm
    st.markdown(theme.header("insights","Material moves, linked to management's words",3), unsafe_allow_html=True)
    st.caption("Year-over-year swings past a materiality threshold, each linked to "
               "the MD&A/Risk passage from that fiscal year that explains it. "
               "Number from XBRL; explanation retrieved and cited.")

    ci, cn = st.columns([1, 1])
    tk_i = ci.selectbox("Company", config.active_tickers(), key="insight_tk")
    n_moves = cn.slider("Top moves", 3, 8, 5)
    moves = cached_moves(tk_i, n_moves)
    if not moves:
        st.info("No material moves past threshold for this company.")
    for mv in moves:
        tone = "pos" if mv.direction == "improved" else "neg"
        ic = theme.icon("up" if mv.direction=="improved" else "down", 16, theme.POS if mv.direction=="improved" else theme.NEG)
        st.markdown(f"<div class='argus-card'>{ic}{theme.pill(mv.direction, tone)} "
                    f"&nbsp; <b>{mv.headline()}</b></div>", unsafe_allow_html=True)
        cprov, cbtn = st.columns([4, 1])
        cprov.markdown("Verified: " + "; ".join(
            f"`{p.concept}={p.value:,.0f}` (FY{p.fy}, acc {p.accession})"
            for p in mv.inputs_to))
        if cbtn.button("Explain", key=f"ex_{mv.metric}_{mv.fy_to}"):
            with st.spinner("Retrieving the explaining passage…"):
                mv = link_move_to_narrative(mv)
                if mv.narrative and mv.narrative.get("available"):
                    st.markdown(f"**Linkage:** {explain_move_llm(mv)}")
                    st.markdown(f"**Source:** {mv.narrative['citation']} · "
                                f"*{mv.narrative['section']}*")
                    st.caption(mv.narrative["passage"][:600] + "…")
                else:
                    reason = (mv.narrative or {}).get("reason", "index not built")
                    st.warning(f"No explaining passage available ({reason}). The "
                               f"figure stands on its own, verified from XBRL.")

# ==========================================================================
# DRILL-DOWN
# ==========================================================================
with tab_drill:
    st.markdown(theme.header("provenance","Provenance — every number traces to a filing",3), unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    tk_sel = c1.selectbox("Company", config.active_tickers(), key="drill_tk")
    msel = c2.selectbox("Metric", sorted(df["metric"].unique()), key="drill_m")
    view = df[(df["ticker"] == tk_sel) & (df["metric"] == msel)].sort_values(
        "fy", ascending=False)
    from sec_client import deep_link_for
    for _, r in view.iterrows():
        with st.expander(f"FY{int(r['fy'])} — {msel}: {fmt_val(r['value'], r['unit'])}"):
            st.markdown(f"**Formula:** `{r['formula']}`")
            st.markdown(f"**Inputs (with source accession):** {r['inputs']}")
            links = []
            for m_obj in data[tk_sel]["metrics"]:
                if m_obj.name == msel and m_obj.fy == r["fy"]:
                    for p in m_obj.inputs:
                        url = deep_link_for(p.concept, p.accession, tk_sel)
                        if url:
                            links.append(f"[{p.concept} → statement table]({url})")
                    break
            if links:
                st.markdown("**Verify at source:** " + " · ".join(dict.fromkeys(links)))

# ==========================================================================
# DATA HEALTH
# ==========================================================================
with tab_health:
    from conflicts import all_conflicts
    st.markdown(theme.header("health","Data conflicts and quality",3), unsafe_allow_html=True)
    st.caption("Where filings disagree, we compute from the latest-filed value but "
               "surface the disagreement here rather than hiding it.")

    LABELS = {
        "restatement": "Restatements (same period, different values)",
        "scale_anomaly": "Scale / units anomalies (thousands-vs-actual)",
        "unit_inconsistency": "Unit inconsistencies",
        "fiscal_misalignment": "Fiscal-year misalignment (comparability caveat)",
        "tag_switch": "XBRL tag switches (stitched during extraction)",
    }
    sev_tone = {"high": "neg", "medium": "warn", "low": "accent"}

    health_tk = st.selectbox("Company", config.active_tickers(), key="health_tk")
    grouped = cached_conflicts(health_tk)
    total = sum(len(v) for v in grouped.values())
    st.markdown(theme.kpi_card("Conflicts & caveats surfaced", str(total),
                               f"{health_tk}"), unsafe_allow_html=True)
    st.write("")

    for kind, items in grouped.items():
        if not items:
            continue
        st.markdown(f"#### {LABELS.get(kind, kind)} — {len(items)}")
        for c in items:
            st.markdown(f"{theme.pill(c.severity, sev_tone.get(c.severity,''))} "
                        f"&nbsp; **{c.concept}**", unsafe_allow_html=True)
            st.caption(c.detail)

    st.divider()
    st.markdown(f"<span style='color:{theme.MUTED}'><b>On segment vs. consolidated:</b> "
                f"the SEC companyfacts API returns consolidated figures only. "
                f"Per-segment breakdowns carry XBRL member dimensions that live in "
                f"the raw instance documents, not this endpoint. We disclose this "
                f"rather than fabricate segment conflicts — all figures here are "
                f"consolidated.</span>", unsafe_allow_html=True)
