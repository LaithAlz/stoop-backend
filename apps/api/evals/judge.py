"""LLM-as-judge grading for eval-scenario drafts (#35).

GOVERNANCE NOTE -- read before touching this file
--------------------------------------------------
This prompt is EVAL INFRASTRUCTURE, not a product prompt. It never ships to
a tenant or landlord, is never injected into ``app/agent/prompts/v1.py``
(frozen), and is NOT subject to CLAUDE.md's "prompts live in
``prompts/v{n}.py``, frozen -- a change is a new version file + full eval
run" discipline. That discipline exists to keep tenant/landlord-facing
behavior auditable and stable; this module grades that behavior from the
outside; it does not produce it. Concretely:

- This file MAY be edited in place as the eval harness matures (a better
  judge prompt, an extra scoring dimension) without a rubric/prompt
  version bump and without triggering "full eval run" per se -- it changes
  how a run is SCORED, not what ``classify_severity``/``draft_response``
  do.
- It still shares fate with the scenario corpus: eval-scenarios-v1.md's own
  growth rule ("every production misclassification becomes scenario #11,
  #12, ...") governs the SCENARIOS, and this module must keep grading them
  faithfully -- but that is ordinary test-maintenance discipline, not the
  rubric-freeze discipline.
- The judge NEVER sees or influences the rubric (``app/agent/rubric.py``)
  or either frozen system prompt; it only ever sees a scenario's OWN
  ``draft_must_include``/``draft_must_not_include`` list (verbatim from the
  YAML) and the drafted body text.

Call shape
----------
``judge_draft`` makes a SECOND ``call_tool_forced`` call (same transport
function ``classify_severity``/``draft_response`` use in production, see
``app/integrations/anthropic.py``) with a tool schema forcing a structured
verdict -- never free-text grading, for the same reason the product itself
never trusts free-text tool-less output: a forced tool call is
machine-checkable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.integrations import anthropic as anthropic_mod
from evals.types import ToolCaller

# ---------------------------------------------------------------------------
# Judge verdict schema
# ---------------------------------------------------------------------------


class JudgeVerdict(BaseModel):
    """Structured output of the judge tool call.

    ``must_include_present`` / ``must_not_include_absent`` are keyed by the
    EXACT scenario ``draft_must_include`` / ``draft_must_not_include``
    strings (the judge is instructed, in the user content, to use those
    exact strings as keys) -- so a missing key after validation means the
    judge simply didn't grade that item, which the caller treats
    conservatively as a failure (see ``evals/scoring.py``).

    Deliberately a NEW model, not reused from ``app.agent.schemas`` --
    this is eval-infrastructure data (a grading verdict), not a product
    boundary type; keeping it separate keeps the governance line in
    the module docstring true in the type system too.
    """

    model_config = ConfigDict(extra="forbid")

    must_include_present: dict[str, bool] = Field(default_factory=dict)
    must_not_include_absent: dict[str, bool] = Field(default_factory=dict)
    plain_language_conformant: bool
    reasoning: str = Field(min_length=1)


JUDGE_TOOL: dict[str, Any] = {
    "name": "judge_draft",
    "description": (
        "Call this tool to grade a drafted SMS reply against a checklist of "
        "must-include and must-not-include items, plus overall plain-language "
        "conformance. Judge MEANING, not exact wording -- a paraphrase counts as "
        "present."
    ),
    "input_schema": JudgeVerdict.model_json_schema(),
}


# ---------------------------------------------------------------------------
# Judge prompt (eval infra -- see module docstring; editable in place)
# ---------------------------------------------------------------------------

JUDGE_SYSTEM_PROMPT: str = """\
You are a strict grading assistant for an SMS-drafting system aimed at
landlords and tenants. You will be shown a drafted SMS reply and a checklist.
Grade the draft against the checklist ONLY -- do not grade anything not asked.

For each "must include" item: judge whether the draft's MEANING covers it,
even if the wording differs. A paraphrase, synonym, or equivalent concrete
instruction counts as present. Vague gestures that don't actually convey the
required content do NOT count.

For each "must not include" item: judge whether the draft's meaning
introduces that content, even indirectly (a hedge like "I probably can't
promise this, but..." followed by the disallowed content still counts as
present -- i.e. NOT absent). A draft that merely acknowledges a topic exists,
without taking the disallowed position/action, counts as absent.

Plain-language conformance (docs/02-product/plain-language-rules.md, condensed
for grading purposes -- this summary is eval infrastructure, not the product's
own prompt):
- Grade-5 reading level: short words, short sentences, active voice.
- No jargon or idioms ("touch base", "loop in", "ASAP", "per our records").
- Concrete over relative: a specific day/time, never "soon" or "later this
  week", when a next step is being committed to.
- Calm, warm, certain tone -- never scolding, never panicked, never
  legalistic.
- At most one question.

Call the judge_draft tool with your verdict. Use the EXACT checklist item
text (given to you) as the dict keys in must_include_present /
must_not_include_absent -- do not paraphrase the keys themselves, only judge
their content against the draft.
"""


def build_judge_user_content(
    *, draft_body: str, must_include: list[str], must_not_include: list[str]
) -> str:
    include_lines = "\n".join(f'- "{item}"' for item in must_include) or "(none)"
    exclude_lines = "\n".join(f'- "{item}"' for item in must_not_include) or "(none)"
    return (
        f"Drafted SMS reply to grade:\n{draft_body}\n\n"
        f"Must include (meaning must be present):\n{include_lines}\n\n"
        f"Must NOT include (meaning must be absent):\n{exclude_lines}\n\n"
        "Grade plain-language conformance as described in your instructions."
    )


async def judge_draft(
    *,
    draft_body: str,
    must_include: list[str],
    must_not_include: list[str],
    tool_caller: ToolCaller,
) -> tuple[JudgeVerdict, anthropic_mod.ToolCallResult]:
    """Grade *draft_body* against the scenario's own checklist. Returns the
    parsed verdict plus the raw call result (for cost/latency accounting).

    ``tool_caller`` matches ``anthropic_mod.call_tool_forced``'s signature
    exactly -- callers pass the real transport function, or (dry-run mode)
    a stub with the identical signature. See ``evals/runner.py``'s
    docstring for why this is the chosen seam.
    """
    call_result = await tool_caller(
        system=JUDGE_SYSTEM_PROMPT,
        user_content=build_judge_user_content(
            draft_body=draft_body, must_include=must_include, must_not_include=must_not_include
        ),
        tool=JUDGE_TOOL,
        tool_name="judge_draft",
        timeout_seconds=anthropic_mod.CLASSIFICATION_BUDGET_SECONDS,
    )
    verdict = JudgeVerdict.model_validate(call_result.tool_input)
    return verdict, call_result


__all__: list[str] = [
    "JUDGE_SYSTEM_PROMPT",
    "JUDGE_TOOL",
    "JudgeVerdict",
    "build_judge_user_content",
    "judge_draft",
]
