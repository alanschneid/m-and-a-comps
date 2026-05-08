"""
User-defined deal context for the comparability mode.

Define the deal you are working on (target, optionally acquirer, optionally
deal thesis). The tool will assess each deal in the brief against this
context and rate its comparability.

Free-form strings — describe your deal as you would in conversation.
Claude parses the natural language. Don't over-structure.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class MyDeal:
    """
    The deal the user is currently working on, used as context for
    comparability scoring on each deal in the brief.

    Only `my_target` is required. Acquirer and thesis are optional but
    enrich the analysis significantly when provided.
    """
    my_target: str
    my_acquirer: Optional[str] = None
    deal_thesis: Optional[str] = None


# ──────── Example: edit this for your use case ────────

EXAMPLE_DEAL = MyDeal(
    my_target=(
        "Vertical SaaS provider for mid-market manufacturers. ~$400M ARR, "
        "growing 25% YoY, ~80% gross margins, slightly EBITDA-negative due "
        "to S&M reinvestment. North America focused, B2B subscription model. "
        "Founded 2014, took $200M in venture funding."
    ),
    my_acquirer=(
        "Diversified industrial conglomerate, $25B market cap, looking to "
        "build a software platform on top of its manufacturing customer "
        "base. No prior major SaaS acquisitions."
    ),
    deal_thesis=(
        "Strategic acquisition to enable software-enabled services revenue "
        "and lock in the customer base ahead of competitor consolidation. "
        "Target EV ~$2.5B (6x ARR)."
    ),
)