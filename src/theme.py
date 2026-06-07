"""
theme.py — Visual design system for the dashboard.

Centralizes the look: a dark, professional palette, typography, custom CSS for
Streamlit components, and a consistent Plotly template. No emojis anywhere — the
visual language is color, weight, and spacing instead.
"""
from __future__ import annotations

# Palette — restrained, finance-terminal feel.
INK = "#0b0e14"          # page background
PANEL = "#121620"        # cards / panels
PANEL_2 = "#1a1f2e"      # raised elements
LINE = "#222838"         # hairlines
TEXT = "#e6e9ef"         # primary text
MUTED = "#8b93a7"        # secondary text
ACCENT = "#76b900"       # NVIDIA-style green accent
POS = "#3fb950"          # improvement
NEG = "#f85149"          # deterioration
WARN = "#d29922"         # caution

# Distinct, legible series colors (color-blind-aware-ish, high contrast on dark).
SERIES = ["#76b900", "#58a6ff", "#f0883e", "#bc8cff", "#e3506b", "#3fb9b0",
          "#d2a8ff", "#7ee787"]


def inject_css() -> None:
    import streamlit as st
    st.markdown(f"""
    <style>
      .stApp {{ background: {INK}; color: {TEXT}; }}
      /* Tighter, calmer headings */
      h1, h2, h3, h4 {{ color: {TEXT}; font-weight: 650; letter-spacing: -0.01em; }}
      h1 {{ font-size: 1.9rem; }}
      .block-container {{ padding-top: 2.2rem; max-width: 1400px; }}

      /* Tabs: underline style, no chrome */
      .stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid {LINE}; }}
      .stTabs [data-baseweb="tab"] {{
          background: transparent; color: {MUTED};
          padding: 10px 18px; font-weight: 550; border-radius: 0;
      }}
      .stTabs [aria-selected="true"] {{
          color: {TEXT}; border-bottom: 2px solid {ACCENT}; background: transparent;
      }}

      /* Cards */
      .argus-card {{
          background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px;
          padding: 18px 20px; margin-bottom: 14px;
      }}
      .argus-kpi {{
          background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px;
          padding: 16px 18px; height: 100%;
      }}
      .argus-kpi .label {{ color: {MUTED}; font-size: 0.78rem; text-transform: uppercase;
          letter-spacing: 0.05em; margin-bottom: 6px; }}
      .argus-kpi .value {{ font-size: 1.5rem; font-weight: 680; color: {TEXT}; }}
      .argus-kpi .sub {{ color: {MUTED}; font-size: 0.75rem; margin-top: 4px; }}
      .argus-kpi .pos {{ color: {POS}; }}
      .argus-kpi .neg {{ color: {NEG}; }}

      /* Pills / badges */
      .pill {{ display:inline-block; padding: 2px 9px; border-radius: 999px;
          font-size: 0.72rem; font-weight: 600; border: 1px solid {LINE}; }}
      .pill-pos {{ color: {POS}; border-color: {POS}33; background: {POS}14; }}
      .pill-neg {{ color: {NEG}; border-color: {NEG}33; background: {NEG}14; }}
      .pill-warn {{ color: {WARN}; border-color: {WARN}33; background: {WARN}14; }}
      .pill-accent {{ color: {ACCENT}; border-color: {ACCENT}33; background: {ACCENT}14; }}

      /* Provenance code */
      code {{ background: {PANEL_2}; color: {ACCENT}; border: 1px solid {LINE};
          padding: 1px 6px; border-radius: 6px; font-size: 0.82rem; }}

      /* Buttons */
      .stButton button {{ border-radius: 8px; border: 1px solid {LINE};
          background: {PANEL_2}; color: {TEXT}; font-weight: 550; }}
      .stButton button:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}

      /* Sidebar */
      section[data-testid="stSidebar"] {{ background: {PANEL}; border-right: 1px solid {LINE}; }}

      /* Dataframe polish */
      .stDataFrame {{ border: 1px solid {LINE}; border-radius: 10px; }}

      /* Hide Streamlit chrome */
      #MainMenu, footer {{ visibility: hidden; }}

      /* Smooth fade/slide when content re-renders (e.g. switching company) */
      @keyframes argusFade {{
          from {{ opacity: 0; transform: translateY(10px); }}
          to   {{ opacity: 1; transform: translateY(0); }}
      }}
      @keyframes argusPop {{
          0%   {{ opacity: 0; transform: translateY(12px) scale(.98); }}
          100% {{ opacity: 1; transform: translateY(0) scale(1); }}
      }}
      .fade-in {{ animation: argusFade 0.38s cubic-bezier(.2,.7,.2,1); }}
      .argus-kpi {{
          animation: argusPop 0.42s cubic-bezier(.2,.7,.2,1) backwards;
          transition: border-color .2s, transform .15s, box-shadow .2s;
      }}
      .argus-kpi:hover {{
          border-color: {ACCENT}77; transform: translateY(-2px);
          box-shadow: 0 6px 20px rgba(0,0,0,.35);
      }}
      /* Stagger the KPI grid so cards cascade in (professional, not abrupt) */
      .argus-kpi:nth-child(1) {{ animation-delay: 0s; }}
      .argus-kpi:nth-child(2) {{ animation-delay: .05s; }}
      .argus-kpi:nth-child(3) {{ animation-delay: .10s; }}
      .argus-kpi:nth-child(4) {{ animation-delay: .15s; }}
      .argus-kpi:nth-child(5) {{ animation-delay: .20s; }}
      .argus-kpi:nth-child(6) {{ animation-delay: .25s; }}
      .argus-kpi:nth-child(7) {{ animation-delay: .30s; }}
      .argus-kpi:nth-child(8) {{ animation-delay: .35s; }}
      /* Animate metric blocks, charts, and cards on every rerun */
      div[data-testid="stMetric"], .stPlotlyChart, .argus-card {{
          animation: argusFade 0.36s cubic-bezier(.2,.7,.2,1);
      }}
      /* Selectbox gets an accent ring so changing it feels responsive */
      div[data-baseweb="select"] > div {{ transition: border-color .2s, box-shadow .2s; }}
      div[data-baseweb="select"] > div:focus-within {{
          border-color: {ACCENT} !important; box-shadow: 0 0 0 2px {ACCENT}33; }}
      /* Remove (×) buttons: compact, centered, no text wrap */
      section[data-testid="stSidebar"] .stButton button {{
          padding: 2px 0; min-height: 32px; white-space: nowrap;
      }}
    </style>
    """, unsafe_allow_html=True)


def style_fig(fig, *, height: int = 460, ytitle: str = "", ylabel_pct=False):
    """Apply the house Plotly style to a figure in-place and return it."""
    fig.update_layout(
        height=height,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, -apple-system, Segoe UI, sans-serif",
                  color=TEXT, size=13),
        margin=dict(l=60, r=30, t=30, b=50),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right",
                    x=1, bgcolor="rgba(0,0,0,0)", font=dict(size=12)),
        xaxis=dict(showgrid=False, zeroline=False, linecolor=LINE,
                   tickfont=dict(color=MUTED)),
        yaxis=dict(showgrid=True, gridcolor=LINE, zeroline=False,
                   tickfont=dict(color=MUTED), title=ytitle,
                   ticksuffix="%" if ylabel_pct else ""),
    )
    # Recolor traces to the house series palette and thicken lines.
    for i, tr in enumerate(fig.data):
        col = SERIES[i % len(SERIES)]
        if hasattr(tr, "line"):
            tr.line.width = 2.5
            tr.line.color = col
        if hasattr(tr, "marker"):
            tr.marker.size = 6
            tr.marker.color = col
    return fig


def kpi_card(label: str, value: str, sub: str = "", tone: str = "") -> str:
    cls = {"pos": "pos", "neg": "neg"}.get(tone, "")
    sub_html = f'<div class="sub {cls}">{sub}</div>' if sub else ""
    return (f'<div class="argus-kpi"><div class="label">{label}</div>'
            f'<div class="value">{value}</div>{sub_html}</div>')


def kpi_grid(cards: list[tuple], cols: int = 4) -> str:
    """Render a responsive grid of KPI cards as ONE HTML string (so a fade/slide
    animation wraps the whole grid and content can never be half-stale).
    Each card tuple: (label, value, sub, tone)."""
    tiles = "".join(
        kpi_card(lbl, val, sub if len(c) > 2 else "", c[3] if len(c) > 3 else "")
        for c in cards for (lbl, val, sub) in [(c[0], c[1], c[2] if len(c) > 2 else "")]
    )
    return (f'<div style="display:grid;grid-template-columns:repeat({cols},1fr);'
            f'gap:14px">{tiles}</div>')


def pill(text: str, tone: str = "accent") -> str:
    return f'<span class="pill pill-{tone}">{text}</span>'


# --------------------------------------------------------------------------
# SVG icon set — crisp line icons (not emojis), inherit currentColor.
# --------------------------------------------------------------------------
def _svg(body: str, size: int = 20, color: str = None) -> str:
    c = color or ACCENT
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="{c}" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round" style="vertical-align:-4px;margin-right:8px">'
            f'{body}</svg>')


ICONS = {
    # Briefing — document with lines
    "briefing": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    # Ask — chat bubble with question
    "ask": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M9.5 9a2.5 2.5 0 0 1 4 1.5c0 1.5-2 2-2 2.5"/><path d="M12 16h.01"/>',
    # Compare — line chart trending
    "compare": '<path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/>',
    # Insights — lightbulb / spark
    "insights": '<path d="M9 18h6M10 21h4"/><path d="M12 2a6 6 0 0 0-4 10.5c.7.7 1 1.3 1 2.5h6c0-1.2.3-1.8 1-2.5A6 6 0 0 0 12 2z"/>',
    # Drill-down — magnifier
    "drill": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    # Data health — pulse / heartbeat
    "health": '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
    # Scorecard — grid
    "scorecard": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/>',
    # Moves — arrows up/down
    "moves": '<path d="M7 17V7M7 7l-3 3M7 7l3 3"/><path d="M17 7v10M17 17l-3-3M17 17l3-3"/>',
    # Caveat — alert triangle
    "caveat": '<path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
    # Provenance — link
    "provenance": '<path d="M10 14a3.5 3.5 0 0 0 5 0l3-3a3.5 3.5 0 0 0-5-5l-1 1"/><path d="M14 10a3.5 3.5 0 0 0-5 0l-3 3a3.5 3.5 0 0 0 5 5l1-1"/>',
    # Universe — globe
    "universe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.5 2.5 15 0 18M12 3c-2.5 2.5-2.5 15 0 18"/>',
    # Calendar (latest FY)
    "calendar": '<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4"/>',
    # Trend up / down
    "up": '<path d="M3 17l6-6 4 4 8-8"/><path d="M21 7h-6M21 7v6"/>',
    "down": '<path d="M3 7l6 6 4-4 8 8"/><path d="M21 17h-6M21 17v-6"/>',
}


def icon(name: str, size: int = 20, color: str = None) -> str:
    return _svg(ICONS.get(name, ""), size=size, color=color)


def header(name: str, text: str, level: int = 3, color: str = None) -> str:
    """Return HTML for a section header with a leading SVG icon."""
    tag = f"h{level}"
    sz = {2: 26, 3: 22, 4: 18}.get(level, 20)
    return (f'<{tag} style="display:flex;align-items:center;gap:2px">'
            f'{icon(name, size=sz, color=color)}<span>{text}</span></{tag}>')

