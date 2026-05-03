"""
Section-aware pre-filtering for SEC proxy filings (S-4, DEFM14A, PREM14A).

Modern M&A proxies rely on "incorporation by reference" — they do NOT
repeat the target's full income statement or balance sheet. Instead, the
target's financials appear inside the fairness opinion section, where the
financial advisor explains the comparable companies and precedent
transactions analysis used to justify the price.

This module identifies fairness opinion sections and projection sections
(both contain the financial signal we need), then extracts windows around
each anchor.

Patterns are validated against Splunk/Cisco DEFM14A and similar large-cap
M&A proxies.

Public API:
    extract_relevant_sections(filing_text) -> str
"""

import re
from typing import Iterable


# ──────── Configuration ────────

# Window size per anchor — fairness opinion sections are typically
# 15-20k chars. We capture 18k to fit one section without overflowing
# into the next.
WINDOW_SIZE = 18_000

# Two anchors closer than this collapse into one region (de-dupe).
# Larger threshold = more aggressive deduplication, fewer chunks.
DEDUPE_THRESHOLD = 15_000

# Substring used to identify boilerplate page headers we want to skip.
TOC_NOISE = "Table of Contents"


# ──────── Section header patterns ────────

# Anchors that empirically appear at meaningful section boundaries in
# real M&A proxies. Each pattern is generic enough to work across deals
# (any bank's name fits "Opinion of [Bank]").
HEADER_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Priority 1: fairness opinion sections (target's revenue, EBITDA,
    # net debt, and multiples appear here in practice)
    ("opinion", re.compile(
        r"Opinion of\s+(?:the\s+)?(?:[A-Z][\w&.\-]+\s+)+"
        r"(?:Partners|LP|LLC|Inc\.?|& Co\.?|Bank|Capital|Securities|Advisors)",
        re.IGNORECASE,
    )),
    ("opinion", re.compile(
        r"Selected (?:Public )?Companies (?:Analysis|Comparison)",
        re.IGNORECASE,
    )),
    ("opinion", re.compile(
        r"Selected (?:Precedent )?Transactions Analysis",
        re.IGNORECASE,
    )),
    ("opinion", re.compile(
        r"Discounted Cash Flow Analysis",
        re.IGNORECASE,
    )),

    # Priority 2: management projections (forward-looking financials)
    ("projections", re.compile(
        r"Unaudited Prospective Financial Information",
        re.IGNORECASE,
    )),
    ("projections", re.compile(
        r"Management(?:'s|\u2019s)? Projections",
        re.IGNORECASE,
    )),
    ("projections", re.compile(
        r"Certain (?:Unaudited )?Forecast",
        re.IGNORECASE,
    )),

    # Priority 3: numeric anchors — these often mark the actual data tables
    # inside the fairness opinion. We use them as backup if the section
    # headers above are missed.
    ("numeric", re.compile(
        r"Total revenues?\s+(?:were|of|for)",
        re.IGNORECASE,
    )),
    ("numeric", re.compile(
        r"Net debt(?:,| of)",
        re.IGNORECASE,
    )),
]


# ──────── Public API ────────

def extract_relevant_sections(filing_text: str) -> str:
    """
    Extract financially-relevant sections from a long SEC proxy filing.

    Args:
        filing_text: Raw HTML or plaintext of the filing.

    Returns:
        Concatenation of relevant sections separated by markers. If no
        recognized headers are found, returns the original (cleaned) text.
    """
    plain_text = _strip_html(filing_text)

    matches = _find_all_header_matches(plain_text)
    matches = _filter_toc_noise(matches, plain_text)

    if not matches:
        # Fallback: no recognized anchors. Return cleaned text and let
        # the caller's truncation logic handle size.
        return plain_text

    deduped = _dedupe_overlapping(matches)

    # Sort: priority order first (opinion → projections → numeric), then
    # by position in original text.
    priority_order = {"opinion": 0, "projections": 1, "numeric": 2}
    deduped.sort(key=lambda m: (priority_order[m["label"]], m["start"]))

    chunks: list[str] = []
    for m in deduped:
        chunk = plain_text[m["start"] : m["start"] + WINDOW_SIZE]
        marker = (
            f"\n\n=== SECTION: {m['label'].upper()} "
            f"(matched anchor: '{m['matched_text'][:80]}') ===\n\n"
        )
        chunks.append(marker + chunk)

    return "".join(chunks)


# ──────── Internal helpers ────────

def _strip_html(text: str) -> str:
    """HTML cleanup — strip tags, decode common entities, collapse whitespace."""
    text = re.sub(r"<script.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode the most common HTML entities that SEC filings use heavily
    text = text.replace("&#160;", " ").replace("&nbsp;", " ")
    text = text.replace("&#8201;", " ").replace("&#8203;", "")
    text = text.replace("&#8217;", "'").replace("&#8220;", '"').replace("&#8221;", '"')
    text = text.replace("&#8212;", "-").replace("&#8211;", "-")
    text = text.replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _find_all_header_matches(text: str) -> list[dict]:
    """Find all header matches across all patterns."""
    matches: list[dict] = []
    for label, pattern in HEADER_PATTERNS:
        for m in pattern.finditer(text):
            matches.append({
                "start": m.start(),
                "label": label,
                "matched_text": m.group(0),
            })
    return matches


def _filter_toc_noise(matches: list[dict], text: str) -> list[dict]:
    """
    Skip matches whose immediate context is the boilerplate page header
    'Table of Contents'. A match preceded by 'Table of Contents' within
    100 chars is almost certainly a TOC entry, not a real section start.
    """
    filtered: list[dict] = []
    for m in matches:
        ctx_start = max(0, m["start"] - 100)
        context_before = text[ctx_start : m["start"]]
        if TOC_NOISE.lower() in context_before.lower():
            continue
        filtered.append(m)
    return filtered


def _dedupe_overlapping(matches: list[dict]) -> list[dict]:
    """
    Collapse matches within DEDUPE_THRESHOLD chars of each other, keeping
    the higher-priority one (opinion > projections > numeric).
    """
    if not matches:
        return []

    priority_order = {"opinion": 0, "projections": 1, "numeric": 2}
    matches_sorted = sorted(matches, key=lambda m: m["start"])

    deduped: list[dict] = []
    for m in matches_sorted:
        if not deduped:
            deduped.append(m)
            continue

        last = deduped[-1]
        if m["start"] - last["start"] < DEDUPE_THRESHOLD:
            if priority_order[m["label"]] < priority_order[last["label"]]:
                deduped[-1] = m
        else:
            deduped.append(m)

    return deduped