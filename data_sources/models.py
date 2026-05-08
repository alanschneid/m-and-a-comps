"""
Data models for the M&A Comps system.

These dataclasses define the shape of data flowing between data sources,
the orchestrator, the extractor, and the Excel builder. They are
data-source-agnostic — SEC, LSEG, Mergermarket all return objects of
these types.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ──────── Lightweight reference returned by search_deals() ────────

@dataclass
class DealReference:
    """
    Minimal identifier for a deal. Returned by search operations.
    Contains enough info to display a hit list, but not the full deal.
    """
    deal_id: str                      # unique within the source (e.g. EDGAR accession number)
    source_name: str                  # "SEC EDGAR", "LSEG Workspace", etc.
    announcement_date: date
    acquirer_name: str
    target_name: str
    target_ticker: Optional[str] = None
    sic_code: Optional[str] = None    # industry classification (used for sector filtering)
    raw_filing_url: Optional[str] = None
    source_specific_id: Optional[str] = None    # source-specific helper (e.g. CIK for SEC)

# ──────── Full deal details returned by get_deal_details() ────────

@dataclass
class DealDetails:
    """
    Complete deal terms extracted from the announcement filing.
    Equity value, structure, advisors, fees — everything needed to
    understand the transaction without computing multiples yet.
    """
    deal_ref: DealReference

    # Deal economics (in USD millions for consistency)
    equity_value_usd_mm: Optional[float] = None
    enterprise_value_usd_mm: Optional[float] = None
    price_per_share_usd: Optional[float] = None
    consideration_type: Optional[str] = None    # "all_cash", "all_stock", "mixed"

    # Premium analysis
    unaffected_price_usd: Optional[float] = None
    premium_pct: Optional[float] = None         # vs unaffected, expressed as 0.42 = 42%

    # Process
    financial_advisors_acquirer: list[str] = field(default_factory=list)
    legal_advisors_acquirer: list[str] = field(default_factory=list)
    financial_advisors_target: list[str] = field(default_factory=list)
    legal_advisors_target: list[str] = field(default_factory=list)
    termination_fee_acquirer_usd_mm: Optional[float] = None
    termination_fee_target_usd_mm: Optional[float] = None

    # Status
    deal_status: Optional[str] = None           # "announced", "closed", "terminated"
    expected_close: Optional[date] = None


# ──────── Target financials returned by get_target_financials() ────────

@dataclass
class TargetFinancials:
    """
    Target company financials at or near deal date.
    
    Sourced from the most recent 10-K available before the announcement.
    EBITDA is NOT extracted — see project README for rationale (definitional
    ambiguity, frequent absence in tech-target financials).
    
    All figures in USD millions.
    """
    deal_ref: DealReference

    # Income statement (LTM = most recent fiscal year-end at extraction time)
    ltm_revenue_usd_mm: Optional[float] = None
    ltm_ebit_usd_mm: Optional[float] = None
    ltm_net_income_usd_mm: Optional[float] = None

    # Balance sheet
    net_debt_usd_mm: Optional[float] = None       # Total Debt - Cash
    cash_usd_mm: Optional[float] = None
    total_debt_usd_mm: Optional[float] = None

    # Forward (only populated when management projections were disclosed)
    fy1_revenue_usd_mm: Optional[float] = None

    # Provenance
    source_filing_url: Optional[str] = None
    source_filing_type: Optional[str] = None      # "10-K", "10-K (FY2022)", or "NOT_FOUND"
    ltm_period_end: Optional[str] = None
    data_completeness_note: Optional[str] = None  # explains what was/wasn't found