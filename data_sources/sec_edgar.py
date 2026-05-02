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

from datetime import date, datetime
from typing import Optional
from urllib.parse import urlencode

from .base import DataSource, DealSearchFilters
from .models import DealReference, DealDetails, TargetFinancials
from ._edgar_client import EdgarClient


# ──────── Constants ────────

# Full-text search endpoint. Documented at https://efts.sec.gov/LATEST/search-index
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

# Phrase used to identify M&A announcement filings.
# "merger agreement" appears verbatim in virtually every Item 1.01 8-K
# announcing a definitive deal. Quoted to force exact-phrase match.
MA_TRIGGER_PHRASE = '"merger agreement"'


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

    def __init__(self) -> None:
        self._client = EdgarClient()

    # ──────── Contract methods ────────

    def get_source_name(self) -> str:
        return "SEC EDGAR"

    def is_available(self) -> bool:
        return True

    def search_deals(self, filters: DealSearchFilters) -> list[DealReference]:
        """
        Find 8-K filings announcing M&A deals, within the filter window.

        Implementation notes:
            - SEC's full-text search does NOT support filtering by deal size
              directly; size filters are applied client-side after extraction.
            - Sector (SIC) filtering IS applied here when SIC codes are given,
              but only as a coarse pre-filter via the index. Final SIC validation
              also happens in get_deal_details when financials are pulled.
        """
        params = self._build_search_params(filters)
        url = f"{EDGAR_SEARCH_URL}?{urlencode(params)}"

        response = self._client.get_json(url)
        hits = response.get("hits", {}).get("hits", [])

        refs: list[DealReference] = []
        for hit in hits[: filters.max_results]:
            ref = self._hit_to_deal_reference(hit)
            if ref is None:
                continue

            # Client-side SIC pre-filter (when caller specified sectors)
            if filters.sic_codes and ref.sic_code not in filters.sic_codes:
                continue

            refs.append(ref)

        return refs

    def get_deal_details(self, ref: DealReference) -> DealDetails:
        raise NotImplementedError("Implemented in sub-phase 5.4")

    def get_target_financials(self, ref: DealReference) -> TargetFinancials:
        raise NotImplementedError("Implemented in sub-phase 5.5")

    # ──────── Internal helpers ────────

    def _build_search_params(self, filters: DealSearchFilters) -> dict:
        """Translate DealSearchFilters into EDGAR search query parameters."""
        params: dict = {
            "q": MA_TRIGGER_PHRASE,
            "forms": "8-K",
        }

        if filters.date_from:
            params["dateRange"] = "custom"
            params["startdt"] = filters.date_from.isoformat()
        if filters.date_to:
            params["dateRange"] = "custom"
            params["enddt"] = filters.date_to.isoformat()

        return params

    def _hit_to_deal_reference(self, hit: dict) -> Optional[DealReference]:
        """
        Convert a raw EDGAR search hit into a DealReference.
        Returns None if the hit cannot be parsed (defensive).
        """
        source = hit.get("_source", {})
        accession_no = hit.get("_id", "").split(":")[0]      # e.g. "0001104659-23-102594"
        if not accession_no:
            return None

        # Filer info — EDGAR returns this as a list because filings can have
        # multiple filers (e.g. both parties to a merger). We use the first.
        ciks = source.get("ciks", [])
        display_names = source.get("display_names", [])
        sic_codes = source.get("sics", [])

        if not display_names:
            return None

        filer_name = display_names[0]
        filer_cik = ciks[0] if ciks else None
        sic = str(sic_codes[0]) if sic_codes else None

        # Filing date arrives as ISO string ("2023-09-21")
        filing_date_str = source.get("file_date")
        try:
            announcement_date = datetime.fromisoformat(filing_date_str).date()
        except (TypeError, ValueError):
            return None

        # URL to the filing index page (lists all documents in the submission)
        accession_clean = accession_no.replace("-", "")
        raw_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={filer_cik}"
            f"&type=8-K&dateb=&owner=include&count=40"
        ) if filer_cik else None

        return DealReference(
            deal_id=accession_no,
            source_name=self.get_source_name(),
            announcement_date=announcement_date,
            acquirer_name="",                  # populated in get_deal_details
            target_name=filer_name,            # filer of 8-K is usually the target
            sic_code=sic,
            raw_filing_url=raw_url,
        )

    def get_deal_details(self, ref: DealReference) -> DealDetails:
        raise NotImplementedError("Implemented in sub-phase 5.4")

    def get_target_financials(self, ref: DealReference) -> TargetFinancials:
        raise NotImplementedError("Implemented in sub-phase 5.5")