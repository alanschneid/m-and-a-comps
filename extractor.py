"""
Source-agnostic data extractor powered by Claude API.

Receives raw filing text (HTML or plaintext) and returns structured
data extracted by Claude. Works for filings from any source — SEC EDGAR,
Companies House (UK), EU Merger Cases, etc. — by passing a source_hint
that gives Claude context about the document type.

Two extraction modes:
    - extract_deal_details(): deal terms (price, structure, advisors, fees)
    - extract_target_financials(): LTM financials of the target

Both modes use Claude with JSON-mode-style structured output for reliability.
"""

import json
import os
import re
from typing import Optional

from anthropic import Anthropic


# ──────── Configuration ────────

# Same model used in Project 1 — strong cost/quality balance.
MODEL = "claude-sonnet-4-5"

# Filings can be very long. We cap input to control cost while keeping
# enough context. 80k chars ≈ 20k tokens ≈ $0.06 per call. 8-Ks rarely
# exceed this; S-4s often do, so for those we'll truncate to the most
# relevant sections (handled in get_target_financials in sub-phase 5.5).
MAX_INPUT_CHARS = 80_000

MAX_OUTPUT_TOKENS_DEAL = 2048
MAX_OUTPUT_TOKENS_FINANCIALS = 2048


# ──────── Prompt: deal details ────────

SYSTEM_PROMPT_DEAL_DETAILS = """\
You are a senior M&A analyst at an elite investment banking boutique.
You extract structured deal terms from regulatory filings (SEC 8-Ks,
UK Companies House announcements, EU merger notifications).

You output ONLY valid JSON, no commentary. Use null for any field that
cannot be determined with high confidence from the filing text. Never
guess — accuracy matters more than completeness.

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
  "is_spac": boolean,                     // true if either party is a SPAC / blank-check vehicle
  "deal_type": "merger" | "tender_offer" | "asset_purchase" | "other" | null,
  "equity_value_usd_mm": number or null,  // total equity consideration
  "price_per_share_usd": number or null,
  "consideration_type": "all_cash" | "all_stock" | "mixed" | null,
  "stock_exchange_ratio": number or null, // shares of acquirer per share of target, if any
  "unaffected_price_usd": number or null, // target's share price before deal rumor/announcement
  "premium_pct": number or null,          // decimal, e.g. 0.42 for 42%
  "advisors_acquirer": [string],          // financial advisors of acquirer (banks)
  "advisors_target": [string],            // financial advisors of target (banks)
  "termination_fee_acquirer_usd_mm": number or null,
  "termination_fee_target_usd_mm": number or null,
  "expected_close_date": string or null,  // ISO format YYYY-MM-DD
  "deal_status": "announced" | "closed" | "terminated" | null
}}

Filing text:
\"\"\"
{filing_text}
\"\"\"
"""


# ──────── Prompt: target financials (stub for sub-phase 5.5) ────────

SYSTEM_PROMPT_FINANCIALS = """\
You are a senior M&A analyst extracting target company financials from
proxy statements (S-4, DEFM14A) or merger documents.

Output ONLY valid JSON. Use null for fields that cannot be determined
with high confidence. All amounts in USD millions.
"""


# ──────── Public API ────────

def extract_deal_details(filing_text: str, source_hint: str = "M&A filing") -> dict:
    """
    Extract structured deal terms from a filing.

    Args:
        filing_text: Raw HTML or plaintext of the filing.
        source_hint: Description of the document type, used to prime Claude
                     (e.g. "SEC 8-K Item 1.01", "UK Rule 2.7 announcement").

    Returns:
        A dict matching the deal_details schema. Fields that cannot be
        extracted are set to None. On parse failure, returns a dict with
        all fields None (degraded mode — never raises).
    """
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


def extract_target_financials(filing_text: str, source_hint: str = "M&A filing") -> dict:
    """Stub — implemented in sub-phase 5.5."""
    raise NotImplementedError("Implemented in sub-phase 5.5")


# ──────── Internal helpers ────────

def _call_claude(system_prompt: str, user_prompt: str, max_tokens: int) -> str:
    """Single Claude API call. Returns raw response text."""
    client = Anthropic()  # uses ANTHROPIC_API_KEY env var

    message = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Claude returns content as a list of blocks; for text-only responses
    # we just take the first block's text.
    return message.content[0].text


def _clean_filing_text(text: str) -> str:
    """
    Reduce filing size by removing HTML noise and capping length.

    SEC filings often arrive as 200KB+ HTML with lots of layout markup,
    style tags, and boilerplate. We strip the noisiest parts and truncate
    to MAX_INPUT_CHARS to control API cost.
    """
    # Remove script and style blocks
    text = re.sub(r"<script.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    if len(text) > MAX_INPUT_CHARS:
        # Keep beginning and end — most key facts are in the first half;
        # legal terms and exhibits often have signal in the tail.
        head = text[: int(MAX_INPUT_CHARS * 0.7)]
        tail = text[-int(MAX_INPUT_CHARS * 0.3) :]
        text = f"{head}\n\n[...truncated middle for length...]\n\n{tail}"

    return text


def _parse_json_response(raw: str, fallback: dict) -> dict:
    """
    Parse Claude's JSON response defensively.
    Returns the fallback dict if parsing fails for any reason.
    """
    # Claude sometimes wraps JSON in ```json fences despite system prompt
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    print(f"[extractor] Warning: failed to parse JSON response. Returning empty result.")
    return fallback


def _empty_deal_details() -> dict:
    """Schema-shaped dict with all None values, used as fallback."""
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
        "advisors_acquirer": [],
        "advisors_target": [],
        "termination_fee_acquirer_usd_mm": None,
        "termination_fee_target_usd_mm": None,
        "expected_close_date": None,
        "deal_status": None,
    }