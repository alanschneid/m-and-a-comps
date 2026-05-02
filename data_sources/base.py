"""
Abstract base class for M&A data sources.

Every data source (SEC EDGAR, LSEG Workspace, Mergermarket, Capital IQ,
Bloomberg) must implement this interface. The orchestrator interacts only
with this contract, not with concrete sources — making the system pluggable.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .models import DealReference, DealDetails, TargetFinancials


# ──────── Filter object passed to search_deals() ────────

@dataclass
class DealSearchFilters:
    """
    Search criteria applied across all data sources.
    Each source translates these into its own query syntax.
    """
    sic_codes: list[str] = field(default_factory=list)      # e.g. ["7372"] for software
    min_equity_value_usd_mm: Optional[float] = None
    max_equity_value_usd_mm: Optional[float] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    deal_status: Optional[str] = None                       # "announced", "closed", "all"
    max_results: int = 100                                  # safety cap


# ──────── The contract every data source must fulfill ────────

class DataSource(ABC):
    """
    Contract for any M&A deal data source.

    Subclasses must implement all four methods. The orchestrator treats
    every source identically — adding a new source means writing a new
    subclass, not modifying anything else.
    """

    @abstractmethod
    def get_source_name(self) -> str:
        """Human-readable identifier (e.g. 'SEC EDGAR', 'LSEG Workspace')."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """
        Returns True if this source can be queried right now (credentials
        present, network reachable, etc.). Used by orchestrator to skip
        unconfigured sources gracefully.
        """
        ...

    @abstractmethod
    def search_deals(self, filters: DealSearchFilters) -> list[DealReference]:
        """
        Find deals matching the filters. Returns lightweight references —
        no financials yet. Cheap operation, suitable for showing a hit list.
        """
        ...

    @abstractmethod
    def get_deal_details(self, ref: DealReference) -> DealDetails:
        """
        Extract full deal terms from announcement filing(s).
        Expensive operation — only call for deals the user wants to analyze.
        """
        ...

    @abstractmethod
    def get_target_financials(self, ref: DealReference) -> TargetFinancials:
        """
        Extract LTM financials of the target company. Typically requires
        a separate filing from the announcement (e.g. S-4 vs 8-K).
        Most expensive operation — call last.
        """
        ...