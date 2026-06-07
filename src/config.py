"""
config.py — Dynamic company registry.

There are NO hardcoded company lists in the application logic. The active universe
is loaded from data/universe.json (editable) and can be changed at runtime — add
any of the ~10,000 SEC filers by ticker. The four semiconductor names are only a
default seed, not a constraint.

Anything in the app that needs "the companies" reads active_tickers() here, so a
reviewer can analyze Walmart vs Target or JPMorgan vs BofA without touching code.
"""
from __future__ import annotations

import json
from pathlib import Path

from sec_client import resolve_cik

_UNIVERSE_FILE = Path(__file__).resolve().parent.parent / "data" / "universe.json"
_DEFAULT_SEED = ["NVDA", "AMD", "INTC", "AVGO"]  # seed only; fully replaceable


def _load() -> dict:
    if _UNIVERSE_FILE.exists():
        try:
            return json.loads(_UNIVERSE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"tickers": list(_DEFAULT_SEED), "sector": "Semiconductors"}


def _save(state: dict) -> None:
    _UNIVERSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _UNIVERSE_FILE.write_text(json.dumps(state, indent=2))


def active_tickers() -> list[str]:
    return _load()["tickers"]


def sector_label() -> str:
    return _load().get("sector", "Custom")


def add_ticker(ticker: str) -> tuple[bool, str]:
    """Validate against SEC's live ticker map, then add. Returns (ok, message)."""
    tk = ticker.strip().upper()
    if not tk:
        return False, "Empty ticker."
    try:
        info = resolve_cik(tk)
    except ValueError:
        return False, f"{tk} not found in SEC's filer registry."
    state = _load()
    if tk in state["tickers"]:
        return False, f"{tk} already in the universe."
    state["tickers"].append(tk)
    _save(state)
    return True, f"Added {tk} ({info['title']})."


def remove_ticker(ticker: str) -> None:
    tk = ticker.strip().upper()
    state = _load()
    state["tickers"] = [t for t in state["tickers"] if t != tk]
    _save(state)


def set_universe(tickers: list[str], sector: str = "Custom") -> dict:
    """Replace the whole universe (validates each ticker)."""
    valid, rejected = [], []
    for t in tickers:
        try:
            resolve_cik(t.upper())
            valid.append(t.upper())
        except ValueError:
            rejected.append(t.upper())
    _save({"tickers": valid, "sector": sector})
    return {"accepted": valid, "rejected": rejected}


def company_title(ticker: str) -> str:
    try:
        return resolve_cik(ticker)["title"]
    except ValueError:
        return ticker


def search_companies(query: str, limit: int = 8) -> list[tuple[str, str]]:
    """
    Type-ahead search over all ~10,000 SEC filers by ticker OR company name.
    Returns [(ticker, title), …] ranked: exact ticker, ticker-prefix, then
    name matches. Powers the autocomplete in the sidebar.
    """
    from sec_client import load_ticker_map
    q = query.strip().upper()
    if not q:
        return []
    tmap = load_ticker_map()
    exact, tk_prefix, name_hits = [], [], []
    ql = query.strip().lower()
    for tk, info in tmap.items():
        title = info["title"]
        if tk == q:
            exact.append((tk, title))
        elif tk.startswith(q):
            tk_prefix.append((tk, title))
        elif ql in title.lower():
            name_hits.append((tk, title))
        if len(tk_prefix) > 50 and len(name_hits) > 50:
            break
    ranked = exact + sorted(tk_prefix) + sorted(name_hits, key=lambda x: len(x[1]))
    # de-dup, preserve order
    seen, out = set(), []
    for tk, title in ranked:
        if tk not in seen:
            seen.add(tk)
            out.append((tk, title))
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    print("Active universe:", active_tickers(), "| sector:", sector_label())
