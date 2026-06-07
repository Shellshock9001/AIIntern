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
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            "Could not reach SEC EDGAR (data.sec.gov). Check your internet "
            "connection. If you're behind a proxy/firewall, allow access to "
            "*.sec.gov. The numeric data and filings are fetched from there."
        ) from e
    except requests.exceptions.Timeout as e:
        raise RuntimeError(
            "SEC EDGAR request timed out. The service may be busy — wait a moment "
            "and re-run; previously fetched data is cached locally."
        ) from e
    if resp.status_code == 403:
        raise RuntimeError(
            "SEC EDGAR returned 403 (blocked). SEC requires a descriptive "
            "User-Agent with contact info; edit USER_AGENT in src/sec_client.py "
            "to include your email.")
    if resp.status_code == 429:
        raise RuntimeError(
            "SEC EDGAR rate-limited the request (429). Wait ~30s and re-run — the "
            "client self-throttles, so this is rare and transient.")
    resp.raise_for_status()
    payload = resp.text
    if cache_key:
        (CACHE_DIR / cache_key).write_text(payload, encoding="utf-8")
    return json.loads(payload) if is_json else payload


# ---------------------------------------------------------------------------
# Ticker -> CIK resolution
# ---------------------------------------------------------------------------
from functools import lru_cache


@lru_cache(maxsize=1)
def load_ticker_map() -> dict[str, dict]:
    """Map upper-case ticker -> {cik (10-digit), title}. Memoized in-process:
    parsed once per run, not re-parsed on every resolve_cik call."""
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


@lru_cache(maxsize=64)
def get_company_facts(ticker: str) -> dict:
    cik = resolve_cik(ticker)["cik"]
    return _get(
        f"{SEC_DATA}/api/xbrl/companyfacts/CIK{cik}.json",
        cache_key=f"facts_{ticker.upper()}.json",
    )


# Canonical concept aliases. Companies tag the same economic line differently;
# we try each alias in order and record which one actually hit. This registry is
# intentionally broad — the discovery layer reports which concepts a given filer
# actually reports, so coverage adapts per company/sector rather than assuming a
# fixed set. Add more concepts here and the whole pipeline picks them up.
CONCEPT_ALIASES: dict[str, list[str]] = {
    # --- Income statement ---
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
    "SGA": ["SellingGeneralAndAdministrativeExpense",
            "GeneralAndAdministrativeExpense"],
    "InterestExpense": ["InterestExpense", "InterestExpenseDebt"],
    "IncomeTax": ["IncomeTaxExpenseBenefit"],
    "PretaxIncome": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                     "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "EPS_Diluted": ["EarningsPerShareDiluted"],
    "EPS_Basic": ["EarningsPerShareBasic"],
    "DilutedShares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    # --- Balance sheet ---
    "TotalAssets": ["Assets"],
    "TotalLiabilities": ["Liabilities"],
    "StockholdersEquity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "CashAndEquivalents": ["CashAndCashEquivalentsAtCarryingValue"],
    "CurrentAssets": ["AssetsCurrent"],
    "CurrentLiabilities": ["LiabilitiesCurrent"],
    "Inventory": ["InventoryNet"],
    "AccountsReceivable": ["AccountsReceivableNetCurrent"],
    "Goodwill": ["Goodwill"],
    "LongTermDebt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "SharesOutstanding": ["CommonStockSharesOutstanding"],
    # --- Cash flow ---
    "OperatingCashFlow": ["NetCashProvidedByUsedInOperatingActivities"],
    "CapEx": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "DepreciationAmort": ["DepreciationDepletionAndAmortization",
                          "DepreciationAmortizationAndAccretionNet"],
    "Dividends": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "StockBuyback": ["PaymentsForRepurchaseOfCommonStock"],
}


def discover_concepts(ticker: str) -> dict[str, dict]:
    """
    Report, for this filer, which canonical concepts are actually reported and
    under which XBRL tag — and which are absent. Lets the app adapt coverage per
    company/sector instead of assuming a fixed set (a bank has no Inventory; a
    chipmaker has no loan-loss provisions).
    """
    gaap = get_company_facts(ticker).get("facts", {}).get("us-gaap", {})
    out = {}
    for canon, aliases in CONCEPT_ALIASES.items():
        hit = next((a for a in aliases if a in gaap), None)
        out[canon] = {"available": hit is not None, "tag": hit}
    return out


def coverage_summary(ticker: str) -> dict:
    d = discover_concepts(ticker)
    present = [c for c, v in d.items() if v["available"]]
    missing = [c for c, v in d.items() if not v["available"]]
    return {"present": present, "missing": missing,
            "coverage_pct": round(100 * len(present) / len(d), 1)}


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


# ---------------------------------------------------------------------------
# Page/table-level deep links via FilingSummary.xml (R-files)
# ---------------------------------------------------------------------------
# Map a canonical concept to the financial STATEMENT it lives on, so we can deep
# link to the exact table (R-file) rather than just the filing.
_CONCEPT_TO_STATEMENT = {
    "Revenue": "income", "CostOfRevenue": "income", "GrossProfit": "income",
    "OperatingIncome": "income", "NetIncome": "income", "ResearchDev": "income",
    "TotalAssets": "balance", "TotalLiabilities": "balance",
    "StockholdersEquity": "balance", "CashAndEquivalents": "balance",
    "LongTermDebt": "balance",
    "OperatingCashFlow": "cash", "CapEx": "cash",
}
_STATEMENT_KEYWORDS = {
    "income": ["statements of income", "statements of operations",
               "income statement", "operations"],
    "balance": ["balance sheet"],
    "cash": ["cash flow"],
}


@lru_cache(maxsize=256)
def statement_links(accession: str, cik: str) -> dict[str, str]:
    """
    Return {statement_type: url} deep-linking to the income/balance/cash-flow
    R-file tables for a filing. Parsed from FilingSummary.xml. Cached.
    """
    import re
    acc_nodash = accession.replace("-", "")
    cik_int = str(int(cik))
    base = f"{SEC_WWW}/Archives/edgar/data/{cik_int}/{acc_nodash}"
    cache_key = f"fsummary_{accession}.xml"
    try:
        xml = _get(f"{base}/FilingSummary.xml", cache_key=cache_key, is_json=False)
    except Exception:
        return {}
    reports = re.findall(
        r"<Report[^>]*>.*?<ShortName>(.*?)</ShortName>.*?<HtmlFileName>(.*?)</HtmlFileName>",
        xml, re.DOTALL)
    out: dict[str, str] = {}
    for short, html in reports:
        s = short.lower()
        for stype, kws in _STATEMENT_KEYWORDS.items():
            if stype in out:
                continue
            if any(kw in s for kw in kws) and "parenthetic" not in s \
                    and "(table" not in s and "(detail" not in s:
                out[stype] = f"{base}/{html}"
    return out


def deep_link_for(concept: str, accession: str, ticker: str) -> Optional[str]:
    """Deep link to the specific statement table where `concept` is reported."""
    stype = _CONCEPT_TO_STATEMENT.get(concept)
    if not stype:
        return None
    cik = resolve_cik(ticker)["cik"]
    links = statement_links(accession, cik)
    return links.get(stype)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import config
    for tk in config.active_tickers():
        info = resolve_cik(tk)
        facts = extract_facts(tk, ["Revenue", "NetIncome"])
        print(f"{tk} ({info['title']}): {len(facts)} facts; "
              f"sample={facts[0].concept}={facts[0].value:,.0f} "
              f"FY{facts[0].fy} acc={facts[0].accession}")
