"""
Source-agnostic data extractor powered by Claude API.

Receives raw filing text (HTML or plaintext) and returns structured
data extracted by Claude. Works for filings from any source — SEC EDGAR,
Companies House (UK), EU Merger Cases — by passing a source_hint that
gives Claude context about the document type.

Two extraction modes:
    - extract_deal_details():       deal terms (price, structure, advisors, fees)
    - extract_target_financials():  financial statements of the target,
                                    sourced from 10-K filings (NOT proxies)

EBITDA is intentionally NOT extracted — see README methodology section.
The tool focuses on consistently-available metrics: revenue, EBIT,
net income, balance sheet items.
"""

import json
import re
from typing import Optional

from anthropic import Anthropic


# ──────── Configuration ────────

MODEL = "claude-sonnet-4-5"
MAX_INPUT_CHARS = 80_000
MAX_OUTPUT_TOKENS_DEAL = 2048
MAX_OUTPUT_TOKENS_FINANCIALS = 2048


# ──────── Prompt: deal details ────────

SYSTEM_PROMPT_DEAL_DETAILS = """\
You are a senior M&A analyst at an elite investment banking boutique.
You extract structured deal terms from regulatory filings (SEC 8-Ks,
S-4s, DEFM14As, UK Companies House announcements, EU merger notifications).

You output ONLY valid JSON, no commentary. Use null for any field that
cannot be determined with high confidence from the filing text. Never
guess — accuracy matters more than completeness.

Distinguish FINANCIAL advisors (investment banks, M&A boutiques like
Goldman Sachs, Morgan Stanley, Qatalyst Partners, Tidal Partners) from
LEGAL advisors (law firms, typically ending in "LLP" — Skadden, Sullivan
& Cromwell, Cravath, Simpson Thacher).

All monetary amounts must be in USD millions (convert if needed).
Premium percentages must be expressed as decimals (e.g. 0.42 for 42%).
"""

USER_PROMPT_TEMPLATE_DEAL_DETAILS = """\
Source: {source_hint}

Extract the following fields from the filing below. Output JSON matching
this exact schema:

{{
  "acquirer_name": string or null,
  "target_name": string or null,
  "is_spac": boolean,
  "deal_type": "merger" | "tender_offer" | "asset_purchase" | "other" | null,
  "equity_value_usd_mm": number or null,
  "price_per_share_usd": number or null,
  "consideration_type": "all_cash" | "all_stock" | "mixed" | null,
  "stock_exchange_ratio": number or null,
  "unaffected_price_usd": number or null,
  "premium_pct": number or null,
  "financial_advisors_acquirer": [string],
  "legal_advisors_acquirer": [string],
  "financial_advisors_target": [string],
  "legal_advisors_target": [string],
  "termination_fee_acquirer_usd_mm": number or null,
  "termination_fee_target_usd_mm": number or null,
  "expected_close_date": string or null,
  "deal_status": "announced" | "closed" | "terminated" | null
}}

Filing text:
\"\"\"
{filing_text}
\"\"\"
"""


# ──────── Prompt: target financials (10-K-focused) ────────

SYSTEM_PROMPT_FINANCIALS = """\
You are a senior M&A analyst extracting target company financials from
a 10-K annual report.

You output ONLY valid JSON, no commentary. Use null for any field that
cannot be determined with high confidence. Never guess. Accuracy over
completeness.

All amounts in USD millions (convert if filing uses thousands).

IMPORTANT: Do NOT attempt to extract or compute EBITDA. EBITDA is
intentionally excluded from this schema due to definitional inconsistency
across sectors (Reported vs Adjusted vs Non-GAAP Operating Income, all
reported differently). We focus on metrics with unambiguous definitions
in 10-K consolidated statements.

Definitions (use these exactly):
    - LTM Revenue   = "Total revenues" or "Total net revenues" from
                      Consolidated Statements of Operations, most recent FY
    - LTM EBIT      = "Operating income" or "Income from operations" 
                      (Revenue - COGS - OpEx, before interest and taxes)
    - LTM Net Income = "Net income" or "Net income attributable to the company"
    - Net Debt       = Total Debt - Cash & Cash Equivalents
                       (Use balance sheet figures, most recent FY-end)
    - Cash           = "Cash and cash equivalents" + "Short-term investments"
    - Total Debt     = "Long-term debt" + "Current portion of long-term debt"
                       + any "Notes payable" / "Senior notes"

If you cannot find a value with high confidence, use null and note it
in data_completeness_note.
"""

USER_PROMPT_TEMPLATE_FINANCIALS = """\
Source: {source_hint}

Extract the target company's most recent annual financials from this
10-K filing. Output JSON matching this exact schema:

{{
  "ltm_revenue_usd_mm": number or null,
  "ltm_ebit_usd_mm": number or null,
  "ltm_net_income_usd_mm": number or null,
  "net_debt_usd_mm": number or null,
  "cash_usd_mm": number or null,
  "total_debt_usd_mm": number or null,
  "ltm_period_end": string or null,
  "data_completeness_note": string
}}

The data_completeness_note field is REQUIRED. Use it to briefly state
which of the requested fields you found in the filing and which you
could not (e.g. "All fields extracted from FY2023 10-K. Total Debt
combines $1,200mm long-term notes + $50mm current portion.").

Filing text:
\"\"\"
{filing_text}
\"\"\"
"""


# ──────── Public API ────────

def extract_deal_details(filing_text: str, source_hint: str = "M&A filing") -> dict:
    """Extract structured deal terms from a filing."""
    cleaned = _clean_filing_text(filing_text)
    user_prompt = USER_PROMPT_TEMPLATE_DEAL_DETAILS.format(
        source_hint=source_hint,
        filing_text=cleaned,
    )

    raw_response = _call_claude(
        system_prompt=SYSTEM_PROMPT_DEAL_DETAILS,
        user_prompt=user_prompt,
        max_tokens=MAX_OUTPUT_TOKENS_DEAL,
    )

    return _parse_json_response(raw_response, _empty_deal_details())


def extract_target_financials(filing_text: str, source_hint: str = "10-K") -> dict:
    """
    Extract target company financials from a 10-K filing.

    Args:
        filing_text: Raw HTML or plaintext of the 10-K.
        source_hint: Description of the document (e.g. "Splunk FY2023 10-K").

    Returns:
        Dict matching the target_financials schema. Same defensive contract
        as extract_deal_details — never raises, returns empty dict on failure.

    Note: This function does NOT extract EBITDA — by design. See module
    docstring for rationale.
    """
    cleaned = _clean_filing_text(filing_text)
    user_prompt = USER_PROMPT_TEMPLATE_FINANCIALS.format(
        source_hint=source_hint,
        filing_text=cleaned,
    )

    raw_response = _call_claude(
        system_prompt=SYSTEM_PROMPT_FINANCIALS,
        user_prompt=user_prompt,
        max_tokens=MAX_OUTPUT_TOKENS_FINANCIALS,
    )

    return _parse_json_response(raw_response, _empty_target_financials())


# ──────── Internal helpers ────────

def _call_claude(system_prompt: str, user_prompt: str, max_tokens: int) -> str:
    """Single Claude API call. Returns raw response text."""
    client = Anthropic()

    message = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return message.content[0].text


def _clean_filing_text(text: str) -> str:
    """Strip HTML noise and cap length."""
    text = re.sub(r"<script.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&#160;", " ").replace("&nbsp;", " ")
    text = text.replace("&#8217;", "'").replace("&#8220;", '"').replace("&#8221;", '"')
    text = text.replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > MAX_INPUT_CHARS:
        head = text[: int(MAX_INPUT_CHARS * 0.7)]
        tail = text[-int(MAX_INPUT_CHARS * 0.3) :]
        text = f"{head}\n\n[...truncated middle for length...]\n\n{tail}"

    return text


def _parse_json_response(raw: str, fallback: dict) -> dict:
    """Parse Claude's JSON response defensively."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    print("[extractor] Warning: failed to parse JSON response. Returning empty result.")
    return fallback


def _empty_deal_details() -> dict:
    return {
        "acquirer_name": None,
        "target_name": None,
        "is_spac": False,
        "deal_type": None,
        "equity_value_usd_mm": None,
        "price_per_share_usd": None,
        "consideration_type": None,
        "stock_exchange_ratio": None,
        "unaffected_price_usd": None,
        "premium_pct": None,
        "financial_advisors_acquirer": [],
        "legal_advisors_acquirer": [],
        "financial_advisors_target": [],
        "legal_advisors_target": [],
        "termination_fee_acquirer_usd_mm": None,
        "termination_fee_target_usd_mm": None,
        "expected_close_date": None,
        "deal_status": None,
    }


def _empty_target_financials() -> dict:
    return {
        "ltm_revenue_usd_mm": None,
        "ltm_ebit_usd_mm": None,
        "ltm_net_income_usd_mm": None,
        "net_debt_usd_mm": None,
        "cash_usd_mm": None,
        "total_debt_usd_mm": None,
        "ltm_period_end": None,
        "data_completeness_note": "Extraction failed; no data available.",
    }