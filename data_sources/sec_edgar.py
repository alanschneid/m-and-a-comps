"""
SEC EDGAR data source adapter.

Implements the DataSource contract using SEC's EDGAR system as backend.
EDGAR is free and public — no credentials required, but every request
must include a User-Agent identifying the requester (SEC compliance rule).

EDGAR coverage: only deals where at least one party is US-listed.
This is approximately 60-70% of globally relevant M&A activity.

API endpoints used:
    - https://efts.sec.gov/LATEST/search-index   (full-text filing search)
    - https://www.sec.gov/cgi-bin/browse-edgar   (company-specific filings)
    - https://www.sec.gov/Archives/edgar/data/   (raw filing documents)

Reference: https://www.sec.gov/os/accessing-edgar-data
"""

from datetime import date
from typing import Optional

from .base import DataSource, DealSearchFilters
from .models import DealReference, DealDetails, TargetFinancials


class SECEdgarSource(DataSource):
    """
    Free, public M&A data source backed by SEC EDGAR.

    Strategy:
        - search_deals():           query full-text search for 8-Ks containing
                                    "merger agreement" within filter window
        - get_deal_details():       fetch 8-K, extract terms via Claude
        - get_target_financials():  fetch related S-4/DEFM14A, extract LTM
                                    financials of target via Claude
    """

    # SEC requires identifying the requester in the User-Agent header.
    # Format convention: "Project Name contact@email.com"
    USER_AGENT = "M&A Comps Auto-Refresh alanschneid1@gmail.com"

    # SEC's documented rate limit: 10 requests/second across all endpoints.
    # We stay well below to avoid throttling on bursts.
    REQUESTS_PER_SECOND = 8

    def __init__(self) -> None:
        # HTTP session and rate limiter will be initialized in sub-phase 5.2
        self._session = None
        self._last_request_ts: Optional[float] = None

    # ──────── Contract methods (DataSource) ────────

    def get_source_name(self) -> str:
        return "SEC EDGAR"

    def is_available(self) -> bool:
        # EDGAR is always available — no credentials needed.
        # Network reachability check will be added in sub-phase 5.2.
        return True

    def search_deals(self, filters: DealSearchFilters) -> list[DealReference]:
        raise NotImplementedError("Implemented in sub-phase 5.3")

    def get_deal_details(self, ref: DealReference) -> DealDetails:
        raise NotImplementedError("Implemented in sub-phase 5.4")

    def get_target_financials(self, ref: DealReference) -> TargetFinancials:
        raise NotImplementedError("Implemented in sub-phase 5.5")