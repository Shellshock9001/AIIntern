"""
sec_client.py — SEC EDGAR data layer.

Two trust tiers:
  1. XBRL CompanyFacts API  -> GROUND TRUTH numbers (filer-tagged, not LLM-parsed).
     Every fact carries its accession number, fiscal year/period, form type, and
     filing date, so any figure is traceable to a specific document.
  2. Filing documents (HTML) -> NARRATIVE text for the RAG layer (MD&A, risk factors).
     The LLM reasons over this text but never sources numbers from it.

No API key required. SEC requires a descriptive User-Agent and <=10 req/s.
Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""
from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("sec_client")

SEC_DATA = "https://data.sec.gov"
SEC_WWW = "https://www.sec.gov"
# SEC asks for a real contact. Override via env if you fork this.
USER_AGENT = "ARGUS-FinDash research contact@shellshockhive.example"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
FILINGS_DIR = Path(__file__).resolve().parent.parent / "data" / "filings"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
FILINGS_DIR.mkdir(parents=True, exist_ok=True)

_LAST_CALL = [0.0]
_MIN_INTERVAL = 0.15  # ~6 req/s, safely under SEC's 10/s ceiling


def _throttle() -> None:
    elapsed = time.time() - _LAST_CALL[0]
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _LAST_CALL[0] = time.time()


def _get(url: str, *, cache_key: Optional[str] = None, is_json: bool = True):
    """GET with throttle + on-disk cache. Filings rarely change, so caching is safe."""
    if cache_key:
        cached = CACHE_DIR / cache_key
        if cached.exists():
            raw = cached.read_text(encoding="utf-8")
            return json.loads(raw) if is_json else raw
    _throttle()
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.text
    if cache_key:
        (CACHE_DIR / cache_key).write_text(payload, encoding="utf-8")
    return json.loads(payload) if is_json else payload


# ---------------------------------------------------------------------------
# Ticker -> CIK resolution
# ---------------------------------------------------------------------------
def load_ticker_map() -> dict[str, dict]:
    """Map upper-case ticker -> {cik_str (10-digit zero-padded), title}."""
    data = _get(f"{SEC_WWW}/files/company_tickers.json", cache_key="company_tickers.json")
    out = {}
    for row in data.values():
        out[row["ticker"].upper()] = {
            "cik": str(row["cik_str"]).zfill(10),
            "title": row["title"],
        }
    return out


def resolve_cik(ticker: str) -> dict:
    m = load_ticker_map()
    t = ticker.upper()
    if t not in m:
        raise ValueError(f"Ticker {ticker!r} not found in SEC ticker map.")
    return m[t]


# ---------------------------------------------------------------------------
# XBRL company facts (ground truth)
# ---------------------------------------------------------------------------
@dataclass
class Fact:
    """A single financial figure, fully traceable to its source filing."""
    ticker: str
    concept: str          # XBRL tag, e.g. "Revenues"
    label: str            # human label from SEC
    value: float
    unit: str             # USD, shares, USD/shares ...
    fy: int               # fiscal year (label of the FILING, not always the value's period)
    fp: str               # FY, Q1, Q2, Q3
    form: str             # 10-K / 10-Q
    period_start: str     # 'start' date (empty for instantaneous balance-sheet items)
    period_end: str       # 'end' date the value covers
    filed: str            # filing date
    accession: str        # ACCESSION NUMBER -> the citation anchor
    frame: Optional[str]  # calendrical frame if present (e.g. CY2023, CY2023Q1)

    def duration_days(self) -> Optional[int]:
        """Span of the reporting period, or None for instantaneous facts."""
        if not self.period_start or not self.period_end:
            return None
        from datetime import date
        try:
            s = date.fromisoformat(self.period_start)
            e = date.fromisoformat(self.period_end)
            return (e - s).days
        except ValueError:
            return None

    def is_annual_period(self) -> bool:
        """True if this fact covers a ~full fiscal year (340-400 days)."""
        d = self.duration_days()
        return d is not None and 340 <= d <= 400

    def is_instant(self) -> bool:
        """Balance-sheet items have no start date — they're point-in-time."""
        return not self.period_start

    def source_url(self) -> str:
        acc_nodash = self.accession.replace("-", "")
        # Resolve CIK lazily from the ticker map at call sites if needed.
        return (f"{SEC_WWW}/cgi-bin/browse-edgar?action=getcompany"
                f"&type={self.form}&dateb=&owner=include&count=40")


def get_company_facts(ticker: str) -> dict:
    cik = resolve_cik(ticker)["cik"]
    return _get(
        f"{SEC_DATA}/api/xbrl/companyfacts/CIK{cik}.json",
        cache_key=f"facts_{ticker.upper()}.json",
    )


# Canonical concept aliases. Companies tag the same economic line differently;
# we try each alias in order and record which one actually hit.
CONCEPT_ALIASES: dict[str, list[str]] = {
    "Revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ],
    "CostOfRevenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "GrossProfit": ["GrossProfit"],
    "OperatingIncome": ["OperatingIncomeLoss"],
    "NetIncome": ["NetIncomeLoss", "ProfitLoss"],
    "ResearchDev": ["ResearchAndDevelopmentExpense"],
    "TotalAssets": ["Assets"],
    "TotalLiabilities": ["Liabilities"],
    "StockholdersEquity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "CashAndEquivalents": ["CashAndCashEquivalentsAtCarryingValue"],
    "LongTermDebt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "OperatingCashFlow": ["NetCashProvidedByUsedInOperatingActivities"],
    "CapEx": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
}


def extract_facts(ticker: str, concept_keys: Optional[list[str]] = None) -> list[Fact]:
    """
    Pull facts for the given canonical concepts.

    A company can switch XBRL tags across years (e.g. NVDA reports revenue under
    RevenueFromContractWithCustomerExcludingAssessedTax in older filings and
    Revenues in newer ones). So we collect from EVERY alias, tag each fact with
    its source alias, then resolve per-period collisions by preferring the alias
    earliest in the priority list — without dropping years only a later alias
    covers. This eliminates silent gaps at tag-switch boundaries.
    """
    facts_json = get_company_facts(ticker)
    gaap = facts_json.get("facts", {}).get("us-gaap", {})
    keys = concept_keys or list(CONCEPT_ALIASES.keys())
    results: list[Fact] = []

    for canon in keys:
        aliases = CONCEPT_ALIASES.get(canon, [canon])
        alias_rank = {a: i for i, a in enumerate(aliases)}
        # period_end -> (rank, Fact); keep highest-priority alias per period.
        best: dict[tuple[str, str, str], tuple[int, Fact]] = {}

        for alias in aliases:
            if alias not in gaap:
                continue
            node = gaap[alias]
            label = node.get("label", alias)
            for unit, entries in node.get("units", {}).items():
                for e in entries:
                    form = e.get("form", "")
                    if form not in ("10-K", "10-Q"):
                        continue
                    if "val" not in e or e.get("fy") is None:
                        continue
                    f = Fact(
                        ticker=ticker.upper(),
                        concept=canon,
                        label=f"{label} [{alias}]",
                        value=float(e["val"]),
                        unit=unit,
                        fy=int(e["fy"]),
                        fp=e.get("fp", ""),
                        form=form,
                        period_start=e.get("start", ""),
                        period_end=e.get("end", ""),
                        filed=e.get("filed", ""),
                        accession=e.get("accn", ""),
                        frame=e.get("frame"),
                    )
                    # Collision key: same form + period + filing => same datapoint.
                    ck = (form, f.period_start, f.period_end)
                    rank = alias_rank[alias]
                    if ck not in best or rank < best[ck][0]:
                        best[ck] = (rank, f)

        results.extend(f for _, f in best.values())
    return results


# ---------------------------------------------------------------------------
# Filing documents (narrative for RAG)
# ---------------------------------------------------------------------------
@dataclass
class FilingRef:
    ticker: str
    form: str
    accession: str
    primary_doc: str
    filing_date: str
    report_date: str

    def doc_url(self, cik: str) -> str:
        acc_nodash = self.accession.replace("-", "")
        cik_int = str(int(cik))  # unpadded for the Archives path
        return (f"{SEC_WWW}/Archives/edgar/data/{cik_int}/"
                f"{acc_nodash}/{self.primary_doc}")

    def index_url(self, cik: str) -> str:
        acc_nodash = self.accession.replace("-", "")
        cik_int = str(int(cik))
        return (f"{SEC_WWW}/Archives/edgar/data/{cik_int}/"
                f"{acc_nodash}/{self.accession}-index.htm")


def list_filings(ticker: str, forms=("10-K", "10-Q"), limit: int = 15) -> list[FilingRef]:
    cik = resolve_cik(ticker)["cik"]
    subs = _get(
        f"{SEC_DATA}/submissions/CIK{cik}.json",
        cache_key=f"subs_{ticker.upper()}.json",
    )
    recent = subs["filings"]["recent"]
    out: list[FilingRef] = []
    for i in range(len(recent["accessionNumber"])):
        if recent["form"][i] not in forms:
            continue
        out.append(FilingRef(
            ticker=ticker.upper(),
            form=recent["form"][i],
            accession=recent["accessionNumber"][i],
            primary_doc=recent["primaryDocument"][i],
            filing_date=recent["filingDate"][i],
            report_date=recent.get("reportDate", recent["filingDate"])[i],
        ))
        if len(out) >= limit:
            break
    return out


def download_filing(ref: FilingRef) -> Path:
    """Download a filing's primary HTML document to data/filings/. Returns local path."""
    cik = resolve_cik(ref.ticker)["cik"]
    url = ref.doc_url(cik)
    fname = f"{ref.ticker}_{ref.form.replace('-', '')}_{ref.report_date}_{ref.accession}.html"
    local = FILINGS_DIR / fname
    if local.exists():
        return local
    html = _get(url, cache_key=None, is_json=False)
    local.write_text(html, encoding="utf-8")
    return local


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for tk in ["NVDA", "AMD", "INTC", "AVGO"]:
        info = resolve_cik(tk)
        facts = extract_facts(tk, ["Revenue", "NetIncome"])
        print(f"{tk} ({info['title']}): {len(facts)} facts; "
              f"sample={facts[0].concept}={facts[0].value:,.0f} "
              f"FY{facts[0].fy} acc={facts[0].accession}")
