"""Prompt package **v2** — FROZEN once merged; to change behavior add v3.

Version bump approved by the founder on 2026-07-06. The ONLY change from
v1 is the refusal-deferral template copy: eval gates 5–7 (LLM judge)
consistently failed the v1 ``legal_rent_ltb`` template against
``docs/02-product/plain-language-rules.md`` — a 29-word legalistic
sentence ("… anything related to the Landlord and Tenant Board on their
behalf") in text that those rules bind (grade-5 reading level, ≤15-word
sentences, never legalistic). ``impersonation`` had the same disease;
``access_codes`` and ``cost_compensation`` were stiff; ``other_tenants``
was already plain and is byte-identical to v1.

Everything else in the package is UNCHANGED from v1 **by construction**:
the classification and drafting system prompts are imported from the
frozen ``v1`` module and re-exported, so they cannot drift from what the
v1 eval baseline measured. Drafts and audit rows written under this
package stamp ``prompt_version = "v2"``.

Template semantics are intentionally identical to v1 (neutral,
non-committal, routes the tenant to the landlord; each template still
carries the hand-off statement itself — the model's ack is told NOT to
repeat it, see ``draft_response._build_user_content``).
"""

from app.agent.prompts.v1 import (
    build_draft_system_prompt,
    get_classify_system_prompt,
)

__all__ = [
    "PROMPT_VERSION",
    "REFUSAL_TEMPLATES",
    "build_draft_system_prompt",
    "get_classify_system_prompt",
    "get_refusal_deferral",
]

PROMPT_VERSION: str = "v2"

# ---------------------------------------------------------------------------
# Refusal-deferral templates (the v2 change).
# When the agent sets a REFUSAL flag the CODE appends the corresponding
# template below verbatim (draft_response._append_deferrals) — the model
# never writes, quotes, or paraphrases this text. Copy conforms to
# plain-language-rules.md: short sentences, no legalistic register.
# ---------------------------------------------------------------------------

REFUSAL_TEMPLATES: dict[str, str] = {
    "access_codes": (
        "For safety, I can't share or reset codes, or let anyone in from "
        "here. Please ask your landlord directly about keys or access."
    ),
    # No time word on the follow-up: plain-language rule 4 bans relative
    # times ("soon"), and a concrete time would be a false commitment made
    # on the landlord's behalf about a topic Stoop refuses to handle.
    "legal_rent_ltb": (
        "I can't discuss rent or legal questions here. That's for your "
        "landlord to work out with you directly. I've passed your message "
        "along so they can follow up with you."
    ),
    "cost_compensation": (
        "I can't make promises about costs, refunds, or rent changes. "
        "Your landlord will talk that through with you directly."
    ),
    # Byte-identical to v1 (already plain, passing the judge).
    "other_tenants": (
        "I can't share information about other tenants. If you have a "
        "concern that involves a neighbour, please reach out to your "
        "landlord directly."
    ),
    "impersonation": (
        "Things like entry permission, lease changes, or approvals need to "
        "come straight from your landlord. I've flagged your request for "
        "them."
    ),
}


def get_refusal_deferral(flag_name: str) -> str:
    """Return the canned neutral deferral text for a given refusal flag.

    Parameters
    ----------
    flag_name:
        One of: ``access_codes``, ``legal_rent_ltb``, ``cost_compensation``,
        ``other_tenants``, ``impersonation``.

    Raises
    ------
    KeyError
        If ``flag_name`` is not a recognised refusal topic.
    """
    return REFUSAL_TEMPLATES[flag_name]
