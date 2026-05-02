"""
SEC EDGAR data source adapter.

Implements the DataSource contract using SEC's EDGAR system as backend.
EDGAR is free and public — no credentials required, but every request
must include a User-Agent identifying the requester (SEC compliance rule).

EDGAR coverage: only deals where at least one party is US-listed.
This is approximately 60-70% of globally relevant M&A activity.

API endpoints used:
    - https://efts.sec.gov/LATEST/search-index   (full-text filing search)
    - https://www.sec.gov/Archives/edgar/data/   (raw filing documents)

Reference: https://www.sec.gov/os/accessing-edgar-data
"""

from datetime import date, datetime
from typing import Optional
from urllib.parse import urlencode

from .base import DataSource, DealSearchFilters
from .models import DealReference, DealDetails, TargetFinancials
from ._edgar_client import EdgarClient

# extractor lives at the project root, not inside data_sources
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extractor import extract_deal_details


# ──────── Constants ────────

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
MA_TRIGGER_PHRASE = '"merger agreement"'
EDGAR_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"


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

            if filters.sic_codes and ref.sic_code not in filters.sic_codes:
                continue

            refs.append(ref)

        return refs

    def get_deal_details(self, ref: DealReference) -> DealDetails:
        """
        Resolve the 8-K's primary document, fetch it, and run the Claude
        extractor on it. Returns a populated DealDetails object.
        """
        primary_url = self._resolve_primary_document_url(ref)
        if primary_url is None:
            print(f"[sec_edgar] Could not resolve primary document for {ref.deal_id}")
            return DealDetails(deal_ref=ref)

        print(f"[sec_edgar] Fetching 8-K for deal {ref.deal_id}...")
        filing_html = self._client.get(primary_url)

        print(f"[sec_edgar] Extracting deal details via Claude...")
        extracted = extract_deal_details(filing_html, source_hint="SEC 8-K Item 1.01")

        if extracted.get("acquirer_name"):
            ref.acquirer_name = extracted["acquirer_name"]
        if extracted.get("target_name"):
            ref.target_name = extracted["target_name"]
        ref.raw_filing_url = primary_url

        return self._dict_to_deal_details(ref, extracted)

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
        """Convert a raw EDGAR search hit into a DealReference."""
        source = hit.get("_source", {})
        accession_no = hit.get("_id", "").split(":")[0]
        if not accession_no:
            return None

        ciks = source.get("ciks", [])
        display_names = source.get("display_names", [])
        sic_codes = source.get("sics", [])

        if not display_names:
            return None

        filer_name = display_names[0]
        filer_cik = ciks[0] if ciks else None
        sic = str(sic_codes[0]) if sic_codes else None

        filing_date_str = source.get("file_date")
        try:
            announcement_date = datetime.fromisoformat(filing_date_str).date()
        except (TypeError, ValueError):
            return None

        return DealReference(
            deal_id=accession_no,
            source_name=self.get_source_name(),
            announcement_date=announcement_date,
            acquirer_name="",
            target_name=filer_name,
            sic_code=sic,
            raw_filing_url=None,
            source_specific_id=filer_cik,
        )

    def _resolve_primary_document_url(self, ref: DealReference) -> Optional[str]:
        """
        Given a DealReference, find the URL of the primary 8-K document.

        EDGAR organizes filings under:
            /Archives/edgar/data/{cik_no_zeros}/{accession_no_dashes}/

        The directory contains an index.json listing all documents. We pick
        the document whose name suggests it's the main 8-K body.
        """
        cik = self._extract_cik_from_accession(ref)
        if cik is None:
            return None

        accession_clean = ref.deal_id.replace("-", "")
        index_url = (
            f"{EDGAR_ARCHIVES_BASE}/{int(cik)}/{accession_clean}/index.json"
        )

        try:
            index = self._client.get_json(index_url)
        except Exception as exc:
            print(f"[sec_edgar] Failed to fetch index for {ref.deal_id}: {exc}")
            return None

        items = index.get("directory", {}).get("item", [])
        if not items:
            return None

        candidates = [
            it["name"] for it in items
            if it.get("name", "").lower().endswith((".htm", ".html"))
            and "index" not in it.get("name", "").lower()
        ]
        if not candidates:
            return None

        prioritized = sorted(candidates, key=lambda n: ("8k" not in n.lower(), n))
        primary = prioritized[0]

        return f"{EDGAR_ARCHIVES_BASE}/{int(cik)}/{accession_clean}/{primary}"

    def _extract_cik_from_accession(self, ref: DealReference) -> Optional[str]:
        """
        Return the filer's CIK, captured during search and stored in
        DealReference.source_specific_id.

        Note: the accession number itself starts with the filing agent's CIK,
        NOT the filer's CIK — they're often different (e.g. EdgarAgents files
        on behalf of many companies). Hence we use the search-time CIK.
        """
        return ref.source_specific_id

    def _dict_to_deal_details(self, ref: DealReference, extracted: dict) -> DealDetails:
        """Convert the dict returned by extractor into a DealDetails object."""
        close_date = None
        ecd_str = extracted.get("expected_close_date")
        if ecd_str:
            try:
                close_date = datetime.fromisoformat(ecd_str).date()
            except (ValueError, TypeError):
                pass

        return DealDetails(
            deal_ref=ref,
            equity_value_usd_mm=extracted.get("equity_value_usd_mm"),
            enterprise_value_usd_mm=None,
            price_per_share_usd=extracted.get("price_per_share_usd"),
            consideration_type=extracted.get("consideration_type"),
            unaffected_price_usd=extracted.get("unaffected_price_usd"),
            premium_pct=extracted.get("premium_pct"),
            advisors_acquirer=extracted.get("advisors_acquirer") or [],
            advisors_target=extracted.get("advisors_target") or [],
            termination_fee_acquirer_usd_mm=extracted.get("termination_fee_acquirer_usd_mm"),
            termination_fee_target_usd_mm=extracted.get("termination_fee_target_usd_mm"),
            deal_status=extracted.get("deal_status"),
            expected_close=close_date,
        )