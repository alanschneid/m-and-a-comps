"""
Commentary generator: produces banker-grade strategic analysis on M&A deals.

This module is the differentiated value of the project. Where data sources
(SEC, LSEG, etc.) provide structured deal terms, the commentary generator
synthesizes those terms with sector knowledge to produce the analytical
text a senior banker would write — strategic rationale, premium analysis,
banker positioning, and red flags.

Each commentary type uses a role-specific system prompt to prime Claude
into the right analytical voice. Output is narrative text, not JSON.

Public API (sub-phase 6.1a — only first function implemented):
    generate_strategic_rationale(details, financials) -> str
"""

from typing import Optional

from anthropic import Anthropic

from data_sources.models import DealDetails, TargetFinancials
from my_deal import MyDeal


# ──────── Configuration ────────

MODEL = "claude-sonnet-4-5"
MAX_OUTPUT_TOKENS = 1024


# ──────── Prompt: Strategic Rationale ────────

SYSTEM_PROMPT_STRATEGIC_RATIONALE = """\
You are a senior M&A analyst at an elite European investment banking
boutique (think Rothschild, Lazard, Moelis). You write the "Strategic
Rationale" section of internal deal memoranda — the analytical narrative
that explains why a transaction makes sense.

Your audience is a Managing Director who has been working in M&A for
20+ years. They do not need basic context. They want sharp, specific,
sector-aware analysis. Generic statements like "this acquisition will
create synergies" or "expanding market presence" are USELESS — they
signal junior thinking and waste the MD's time.

Good rationale is:
    - Sector-specific (mention sub-vertical positioning, competitive dynamics)
    - Comparable-aware (reference recent precedent transactions where relevant)
    - Strategically nuanced (motive often goes beyond the public statement)
    - Concise (3-5 sentences, dense in signal)

Bad rationale is:
    - Vague platitudes about synergies, scale, or market expansion
    - Repetition of facts already stated in the deal terms
    - Generic management-quote-style language ("transformational acquisition")

Output is plain prose, no headers, no bullets. Write it as it would
appear in the body of a deal memo.
"""


# ──────── Public API ────────

def generate_strategic_rationale(
    details: DealDetails,
    financials: Optional[TargetFinancials] = None,
) -> str:
    """
    Generate the Strategic Rationale commentary for a deal.

    Args:
        details: extracted deal terms (acquirer, target, structure, etc.)
        financials: target's financials (optional — improves analysis quality
                    when revenue and balance sheet figures are available)

    Returns:
        3-5 sentences of banker-grade strategic rationale prose.
        Returns a fallback message if the API call fails.
    """
    user_prompt = _build_strategic_rationale_prompt(details, financials)

    try:
        response = _call_claude(
            system_prompt=SYSTEM_PROMPT_STRATEGIC_RATIONALE,
            user_prompt=user_prompt,
        )
        return response.strip()
    except Exception as exc:
        print(f"[commentary] Strategic rationale generation failed: {exc}")
        return f"[Strategic rationale unavailable — API error: {exc}]"


# ──────── Internal helpers ────────

def _call_claude(system_prompt: str, user_prompt: str) -> str:
    """Single Claude API call. Returns raw text response."""
    client = Anthropic()

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return message.content[0].text


def _build_strategic_rationale_prompt(
    details: DealDetails,
    financials: Optional[TargetFinancials],
) -> str:
    """
    Construct the user prompt with deal context formatted for analysis.
    """
    ref = details.deal_ref

    # Build the deal facts block
    facts_lines = [
        f"DEAL: {ref.acquirer_name} acquires {ref.target_name}",
        f"Announced: {ref.announcement_date}",
        f"Equity Value: USD {details.equity_value_usd_mm:,.0f} mm" if details.equity_value_usd_mm else None,
        f"Price/Share: USD {details.price_per_share_usd}" if details.price_per_share_usd else None,
        f"Consideration: {details.consideration_type}" if details.consideration_type else None,
        f"Premium: {details.premium_pct:.0%}" if details.premium_pct else None,
        f"Sector (SIC): {ref.sic_code}" if ref.sic_code else None,
    ]
    facts_block = "\n".join(line for line in facts_lines if line)

    # Build the target financials block (if available)
    fin_block = ""
    if financials and financials.ltm_revenue_usd_mm:
        fin_lines = [
            "",
            "TARGET FINANCIALS (most recent 10-K):",
            f"  LTM Revenue: USD {financials.ltm_revenue_usd_mm:,.0f} mm" if financials.ltm_revenue_usd_mm else None,
            f"  LTM EBIT: USD {financials.ltm_ebit_usd_mm:,.0f} mm" if financials.ltm_ebit_usd_mm else None,
            f"  Net Debt: USD {financials.net_debt_usd_mm:,.0f} mm" if financials.net_debt_usd_mm is not None else None,
            f"  Cash: USD {financials.cash_usd_mm:,.0f} mm" if financials.cash_usd_mm else None,
        ]
        fin_block = "\n".join(line for line in fin_lines if line is not None)

    # Build the advisors block (signals about deal complexity / process)
    adv_lines = []
    if details.financial_advisors_acquirer:
        adv_lines.append(f"Acquirer financial advisors: {', '.join(details.financial_advisors_acquirer)}")
    if details.financial_advisors_target:
        adv_lines.append(f"Target financial advisors: {', '.join(details.financial_advisors_target)}")
    adv_block = ("\n" + "\n".join(adv_lines)) if adv_lines else ""

    return f"""\
Write the Strategic Rationale for the following M&A transaction. Focus
on WHY this deal — what is the acquirer trying to achieve, what
competitive or strategic gap is it filling, what does the timing or
structure tell you. 3-5 sentences. No headers. No platitudes.

{facts_block}{fin_block}{adv_block}
"""

# ──────── Prompt: Comparability Assessment ────────

SYSTEM_PROMPT_COMPARABILITY = """\
You are a senior M&A analyst at an elite European investment banking
boutique. You evaluate how comparable an executed transaction is to a
new deal a colleague is working on.

Your audience is the deal team — they need a quick, sharp assessment
of whether a precedent transaction is relevant to their work.

Good comparability assessment is:
    - Specific about WHY it's comparable or not (sector, size, structure,
      vintage, acquirer type, strategic logic)
    - Honest about limitations — most deals are partially comparable
    - Useful for downstream analysis — explicitly tells the deal team
      what they CAN and CANNOT use this precedent for

Format requirements:
    - First line: rating in stars (1-5) and a one-word verdict
      Examples: "★★★★☆ Strong", "★★★☆☆ Moderate", "★★☆☆☆ Weak", "★☆☆☆☆ Marginal"
    - Then: 2-4 bullet points on Pros (what makes it comparable)
    - Then: 2-3 bullet points on Cons (where the comparison breaks down)
    - End with one "Bottom line:" sentence stating the practical use

Output is plain text with simple bullet markers (- or •).
Total output: 8-12 lines maximum.
"""


def generate_comparability_assessment(
    brief_deal: DealDetails,
    my_deal: "MyDeal",
    financials: Optional[TargetFinancials] = None,
) -> str:
    """
    Generate a comparability assessment for a deal in the brief, scoring
    its relevance to the user's own deal.

    Args:
        brief_deal: a deal from the brief (DealDetails extracted from filing)
        my_deal: the user's own deal context (MyDeal dataclass)
        financials: optional, target financials of the brief deal

    Returns:
        Star-rated comparability commentary, 8-12 lines.
        Returns fallback message on API error.
    """
    user_prompt = _build_comparability_prompt(brief_deal, my_deal, financials)

    try:
        response = _call_claude(
            system_prompt=SYSTEM_PROMPT_COMPARABILITY,
            user_prompt=user_prompt,
        )
        return response.strip()
    except Exception as exc:
        print(f"[commentary] Comparability assessment failed: {exc}")
        return f"[Comparability unavailable — API error: {exc}]"


def _build_comparability_prompt(
    brief_deal: DealDetails,
    my_deal: "MyDeal",
    financials: Optional[TargetFinancials],
) -> str:
    """Construct the user prompt with both deals' context."""
    ref = brief_deal.deal_ref

    # Brief deal facts
    brief_lines = [
        f"Acquirer: {ref.acquirer_name}",
        f"Target: {ref.target_name}",
        f"Announced: {ref.announcement_date}",
        f"Equity Value: USD {brief_deal.equity_value_usd_mm:,.0f} mm" if brief_deal.equity_value_usd_mm else None,
        f"Consideration: {brief_deal.consideration_type}" if brief_deal.consideration_type else None,
        f"Premium: {brief_deal.premium_pct:.0%}" if brief_deal.premium_pct else None,
    ]
    if financials and financials.ltm_revenue_usd_mm:
        brief_lines.append(f"Target LTM Revenue: USD {financials.ltm_revenue_usd_mm:,.0f} mm")
    if financials and financials.ltm_ebit_usd_mm is not None:
        brief_lines.append(f"Target LTM EBIT: USD {financials.ltm_ebit_usd_mm:,.0f} mm")

    brief_block = "\n  ".join(line for line in brief_lines if line)

    # My deal context
    my_lines = [f"Target: {my_deal.my_target}"]
    if my_deal.my_acquirer:
        my_lines.append(f"Acquirer: {my_deal.my_acquirer}")
    if my_deal.deal_thesis:
        my_lines.append(f"Thesis: {my_deal.deal_thesis}")

    my_block = "\n  ".join(my_lines)

    return f"""\
Assess how comparable the following EXECUTED DEAL (from the brief) is to
MY DEAL (the one I'm working on).

EXECUTED DEAL:
  {brief_block}

MY DEAL:
  {my_block}

Output the comparability assessment in the format described in the system
prompt. Be sharp, specific, and honest about limitations.
"""