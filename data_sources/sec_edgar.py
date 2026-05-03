"""
SEC EDGAR data source adapter.

Implements the DataSource contract using SEC's EDGAR system as backend.
EDGAR is free and public — no credentials required, but every request
must include a User-Agent identifying the requester (SEC compliance rule).

EDGAR coverage: only deals where at least one party is US-listed.
This is approximately 60-70% of globally relevant M&A activity.

API endpoints used:
    - https://efts.sec.gov/LATEST/search-index   (full-text filing search)
    - https://data.sec.gov/submissions/CIK*.json (per-company filings index)
    - https://www.sec.gov/Archives/edgar/data/   (raw filing documents)

Reference: https://www.sec.gov/os/accessing-edgar-data
"""

import re
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
from extractor import extract_deal_details, extract_target_financials


# ──────── Constants ────────

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
MA_TRIGGER_PHRASE = '"merger agreement"'
EDGAR_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
EDGAR_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"

# Pattern for XBRL "R-file" slices (R1.htm, R2.htm, ...) that auto-generate
# from inline XBRL filings. These are NOT the human-readable document.
XBRL_SLICE_PATTERN = re.compile(r"^r\d+\.html?$", re.IGNORECASE)


class SECEdgarSource(DataSource):
    """
    Free, public M&A data source backed by SEC EDGAR.

    Strategy:
        - search_deals():           full-text search for 8-Ks containing
                                    "merger agreement" within filter window
        - get_deal_details():       fetch 8-K, extract terms via Claude
        - get_target_financials():  fetch target's most recent 10-K
                                    before announcement, extract via Claude.
                                    EBITDA intentionally not extracted.
    """

    def __init__(self) -> None:
        self._client = EdgarClient()

    # ──────── Contract methods ────────

    def get_source_name(self) -> str:
        return "SEC EDGAR"

    def is_available(self) -> bool:
        return True

    def search_deals(self, filters: DealSearchFilters) -> list[DealReference]:
        """Find 8-K filings announcing M&A deals within the filter window."""
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
        """Resolve the 8-K, fetch it, run extractor, return DealDetails."""
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
        """
        Locate the target's most recent 10-K filed BEFORE the deal
        announcement, fetch it, and extract financials via Claude.

        Returns gracefully with a note if no 10-K is available (private
        target or recently-listed entity without prior 10-K).
        """
        cik = ref.source_specific_id
        if cik is None:
            return TargetFinancials(
                deal_ref=ref,
                source_filing_type="NOT_FOUND",
                data_completeness_note=(
                    "Target CIK not available. Likely a private target or "
                    "data unavailable in search results."
                ),
            )

        print(f"[sec_edgar] Searching most recent 10-K for target CIK {cik}...")
        ten_k_accession = self._find_most_recent_10k(cik, before=ref.announcement_date)
        if ten_k_accession is None:
            return TargetFinancials(
                deal_ref=ref,
                source_filing_type="NOT_FOUND",
                data_completeness_note=(
                    f"No 10-K found for target (CIK {cik}) before "
                    f"{ref.announcement_date}. Target may be private or "
                    f"recently-listed without prior annual reports."
                ),
            )

        print(f"[sec_edgar] Fetching 10-K {ten_k_accession}...")
        ten_k_url = self._resolve_10k_document_url(cik, ten_k_accession)
        if ten_k_url is None:
            return TargetFinancials(
                deal_ref=ref,
                source_filing_type="NOT_FOUND",
                data_completeness_note=(
                    f"10-K {ten_k_accession} located but primary document "
                    f"could not be resolved."
                ),
            )

        filing_html = self._client.get(ten_k_url)

        print(f"[sec_edgar] Extracting target financials via Claude...")
        extracted = extract_target_financials(
            filing_html,
            source_hint=f"SEC 10-K — {ref.target_name}",
        )

        return self._dict_to_target_financials(ref, extracted, ten_k_url)

    # ──────── Internal helpers — search ────────

    def _build_search_params(self, filters: DealSearchFilters) -> dict:
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

        try:
            announcement_date = datetime.fromisoformat(source.get("file_date")).date()
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

    # ──────── Internal helpers — 8-K resolution ────────

    def _resolve_primary_document_url(self, ref: DealReference) -> Optional[str]:
        """Find the URL of the primary 8-K document within the filing folder."""
        cik = ref.source_specific_id
        if cik is None:
            return None
        accession_clean = ref.deal_id.replace("-", "")
        index_url = f"{EDGAR_ARCHIVES_BASE}/{int(cik)}/{accession_clean}/index.json"

        try:
            index = self._client.get_json(index_url)
        except Exception as exc:
            print(f"[sec_edgar] Failed to fetch index for {ref.deal_id}: {exc}")
            return None

        items = index.get("directory", {}).get("item", [])
        candidates = [
            it["name"] for it in items
            if it.get("name", "").lower().endswith((".htm", ".html"))
            and "index" not in it.get("name", "").lower()
            and not XBRL_SLICE_PATTERN.match(it.get("name", ""))
        ]
        if not candidates:
            return None

        prioritized = sorted(candidates, key=lambda n: ("8k" not in n.lower(), n))
        return f"{EDGAR_ARCHIVES_BASE}/{int(cik)}/{accession_clean}/{prioritized[0]}"

    # ──────── Internal helpers — 10-K resolution ────────

    def _find_most_recent_10k(self, cik: str, before: date) -> Optional[str]:
        """
        Query SEC submissions API for the target's filings, return the
        accession number of the most recent 10-K filed BEFORE `before`.
        Returns None if no 10-K found (likely a private target).
        """
        cik_padded = str(int(cik)).zfill(10)
        url = f"{EDGAR_SUBMISSIONS_BASE}/CIK{cik_padded}.json"

        try:
            data = self._client.get_json(url)
        except Exception as exc:
            print(f"[sec_edgar] Failed to fetch submissions for CIK {cik}: {exc}")
            return None

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])

        before_str = before.isoformat()
        for form, file_date, accession in zip(forms, dates, accessions):
            if form == "10-K" and file_date < before_str:
                return accession

        return None

    def _resolve_10k_document_url(self, cik: str, accession: str) -> Optional[str]:
        """
        Find the primary 10-K HTML document within the filing folder.

        SEC's inline-XBRL 10-Ks contain ~100+ files: the human-readable
        document (one large .htm), XBRL R-file slices (R1.htm, R2.htm, ...),
        XBRL data (.xml), and exhibits. We want the human-readable file:
        it has no R-prefix, no exhibit prefix, and is typically the largest.
        """
        accession_clean = accession.replace("-", "")
        index_url = f"{EDGAR_ARCHIVES_BASE}/{int(cik)}/{accession_clean}/index.json"

        try:
            index = self._client.get_json(index_url)
        except Exception as exc:
            print(f"[sec_edgar] Failed to fetch 10-K index: {exc}")
            return None

        items = index.get("directory", {}).get("item", [])

        # Filter to plausible main-document candidates.
        candidates = [
            it for it in items
            if it.get("name", "").lower().endswith((".htm", ".html"))
            and "index" not in it.get("name", "").lower()
            and "ex-" not in it.get("name", "").lower()        # exclude exhibits
            and not XBRL_SLICE_PATTERN.match(it.get("name", ""))  # exclude XBRL slices
        ]
        if not candidates:
            return None

        # The main 10-K is the largest .htm file matching the criteria above.
        # Inline-XBRL 10-Ks are typically 1-10 MB; XBRL slices are <500 KB.
        candidates.sort(key=lambda it: int(it.get("size", "0") or "0"), reverse=True)
        primary = candidates[0]["name"]

        return f"{EDGAR_ARCHIVES_BASE}/{int(cik)}/{accession_clean}/{primary}"

    # ──────── Internal helpers — dict → dataclass ────────

    def _dict_to_deal_details(self, ref: DealReference, extracted: dict) -> DealDetails:
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

    def _dict_to_target_financials(
        self,
        ref: DealReference,
        extracted: dict,
        source_url: str,
    ) -> TargetFinancials:
        return TargetFinancials(
            deal_ref=ref,
            ltm_revenue_usd_mm=extracted.get("ltm_revenue_usd_mm"),
            ltm_ebit_usd_mm=extracted.get("ltm_ebit_usd_mm"),
            ltm_net_income_usd_mm=extracted.get("ltm_net_income_usd_mm"),
            net_debt_usd_mm=extracted.get("net_debt_usd_mm"),
            cash_usd_mm=extracted.get("cash_usd_mm"),
            total_debt_usd_mm=extracted.get("total_debt_usd_mm"),
            source_filing_url=source_url,
            source_filing_type="10-K",
            ltm_period_end=extracted.get("ltm_period_end"),
            data_completeness_note=extracted.get("data_completeness_note"),
        )