"""Tests for the eval harness itself (#35/#36).

Two very different kinds of test live in this file:

1. **Default-suite tests** (unmarked / ``@pytest.mark.unit`` -- collected by
   the ordinary ``uv run pytest`` / CI's ``pytest -m "not eval"``): exercise
   the loader, the scoring/assertion logic, and the FULL
   ``run_scenario``/``main()`` pipeline against ``evals.runner.
   make_dry_run_tool_caller`` -- a deterministic stub that never touches the
   network (see ``evals/runner.py``'s module docstring "Dry-run seam").
   These prove the machinery (YAML -> Pydantic -> prompt construction ->
   call -> parse -> assert -> score -> report/snapshot) works end-to-end,
   for FREE, on every CI run. They do NOT prove the product's prompts or
   rubric are good at the task -- only the real, paid gate below does that.

2. **``@pytest.mark.eval`` tests** at the bottom of this file: one
   parametrized test per scenario (11 canonical incl. the prompt-injection
   addition + 9 negative-prefilter = 20), calling the REAL Anthropic API via
   ``evals.runner.make_real_tool_caller``. These are NEVER run by an
   implementer/agent -- ``uv run pytest -m eval`` is the orchestrator's paid
   gate. This file's job is only to make sure they are collected correctly
   (``uv run pytest -m eval --collect-only -q``); this task's own
   instructions explicitly forbid running them here.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

import evals.runner as runner_mod
from app.agent.schemas import Severity
from app.integrations import anthropic as anthropic_mod
from evals.judge import JudgeVerdict
from evals.runner import (
    ScenarioInfraError,
    format_report,
    main,
    make_dry_run_tool_caller,
    make_real_tool_caller,
    reset_pacing_accountant_for_tests,
    reset_sleep_for_tests,
    reset_token_budget_tracker_for_tests,
    run_scenario,
    scenario_result_to_dict,
    tool_caller_factory,
    wrap_with_pacing_and_backoff,
    write_last_run_report,
    write_snapshot,
)
from evals.scenario import Scenario, load_scenarios
from evals.scoring import (
    ClassificationSample,
    DraftCheck,
    ScenarioResult,
    check_classification_sample,
    check_draft,
    expected_actions_for,
    score_results,
)
from evals.types import ToolCaller

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _rate_limit_error() -> anthropic.RateLimitError:
    return anthropic.RateLimitError(
        "rate limited", response=httpx.Response(status_code=429, request=_REQUEST), body=None
    )


def _overloaded_error() -> anthropic.OverloadedError:
    return anthropic.OverloadedError(
        "overloaded", response=httpx.Response(status_code=529, request=_REQUEST), body=None
    )


def _bad_request_error() -> anthropic.BadRequestError:
    return anthropic.BadRequestError(
        "bad request", response=httpx.Response(status_code=400, request=_REQUEST), body=None
    )


def _call_error_from(cause: BaseException) -> anthropic_mod.AnthropicCallError:
    """Build an ``AnthropicCallError`` with ``__cause__`` set exactly like
    ``call_tool_forced``'s own ``raise AnthropicCallError(...) from exc``
    does -- without needing to actually execute a raise/from statement
    each time a fake tool_caller wants to produce one."""
    err = anthropic_mod.AnthropicCallError(f"Anthropic API error: {type(cause).__name__}")
    err.__cause__ = cause
    return err


_FAKE_CLASSIFY_RESULT: anthropic_mod.ToolCallResult = anthropic_mod.ToolCallResult(
    tool_input={
        "severity": "ROUTINE",
        "rules_fired": [],
        "modifier": None,
        "refusal_flags": [],
        "reasoning": ["ok"],
    },
    tokens_in=10,
    tokens_out=10,
    model="fake",
)

# NOTE: deliberately NOT a blanket module-level `pytestmark` -- the
# `@pytest.mark.eval` test at the bottom of this file must carry ONLY that
# marker (never an additional "unit" tag that would muddy `-m eval`/
# `-m "not eval"` selection). Every other test in this file is unmarked,
# which is sufficient: `pytest -m "not eval"` (CI's invocation) already
# selects any test that isn't marked ``eval``, matching every other
# integration-marked test file's convention of only tagging what needs to
# be excluded/included, not tagging every ordinary test as "unit".

ALL_SCENARIOS: list[Scenario] = load_scenarios()
CANONICAL_SCENARIOS: list[Scenario] = load_scenarios(include_tier0_only=False)
NEGATIVE_SCENARIOS: list[Scenario] = [s for s in ALL_SCENARIOS if s.tier0_only]


def _by_id(scenario_id: str) -> Scenario:
    for scenario in ALL_SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(scenario_id)  # pragma: no cover -- test author error


def _minimal_context() -> dict[str, Any]:
    return {"property": "p", "tenant": {"name": "n", "unit": "1"}, "time_local": "9:00"}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TestLoader:
    def test_loads_all_scenarios(self) -> None:
        # 11 canonical (E1-E3, U1-U3, R1-R2, F1-F2, plus the authorized
        # prompt-injection addition E4) + 9 negative-prefilter.
        assert len(CANONICAL_SCENARIOS) == 11
        assert len(NEGATIVE_SCENARIOS) == 9
        assert len(ALL_SCENARIOS) == 20

    def test_expected_canonical_ids_present(self) -> None:
        ids = {s.id for s in CANONICAL_SCENARIOS}
        assert ids == {
            "e1-water-electrical",
            "e2-gas-smell",
            "e3-noheat-extreme-cold-infant",
            "e4-prompt-injection-deescalate",
            "u1-noheat-mild",
            "u2-fridge-dead",
            "u3-lock-broken",
            "r1-faucet-drip",
            "r2-rent-receipt",
            "f1-rent-ltb",
            "f2-access-code",
        }

    def test_ids_are_unique(self) -> None:
        ids = [s.id for s in ALL_SCENARIOS]
        assert len(ids) == len(set(ids))

    def test_e1_and_e2_carry_prefilter_must_fire_true(self) -> None:
        assert _by_id("e1-water-electrical").prefilter_must_fire is True
        assert _by_id("e2-gas-smell").prefilter_must_fire is True

    def test_every_other_canonical_scenario_asserts_prefilter_must_not_fire(self) -> None:
        for scenario in CANONICAL_SCENARIOS:
            if scenario.id in {"e1-water-electrical", "e2-gas-smell"}:
                continue
            assert scenario.prefilter_must_fire is False, scenario.id

    def test_negative_suite_is_tier0_only_and_asserts_must_not_fire(self) -> None:
        for scenario in NEGATIVE_SCENARIOS:
            assert scenario.prefilter_must_fire is False, scenario.id
            assert scenario.category == "routine"

    def test_f1_f2_category_is_refusal_but_severity_is_routine(self) -> None:
        f1 = _by_id("f1-rent-ltb")
        f2 = _by_id("f2-access-code")
        assert f1.category == "refusal"
        assert f1.expect.severity == "routine"
        assert f2.category == "refusal"
        assert f2.expect.severity == "routine"

    def test_unknown_field_rejected(self) -> None:
        raw: dict[str, Any] = {
            "id": "x",
            "category": "routine",
            "context": _minimal_context(),
            "message": "hi",
            "expect": {"severity": "routine"},
            "rationale": "r",
            "this_field_does_not_exist": True,
        }
        with pytest.raises(Exception, match="this_field_does_not_exist|extra"):
            Scenario.model_validate(raw)

    def test_unknown_nested_expect_field_rejected(self) -> None:
        raw: dict[str, Any] = {
            "id": "x",
            "category": "routine",
            "context": _minimal_context(),
            "message": "hi",
            "expect": {"severity": "routine", "made_up_field": 1},
            "rationale": "r",
        }
        with pytest.raises(Exception, match="made_up_field|extra"):
            Scenario.model_validate(raw)

    def test_missing_required_field_rejected(self) -> None:
        raw: dict[str, Any] = {
            "id": "x",
            "category": "routine",
            "context": _minimal_context(),
            "expect": {"severity": "routine"},
            "rationale": "r",
        }
        with pytest.raises(Exception, match="message"):
            Scenario.model_validate(raw)

    def test_duplicate_id_raises(self, tmp_path: Any) -> None:
        yaml_text = (
            "id: dup\n"
            "category: routine\n"
            "context:\n"
            "  property: p\n"
            "  tenant: {name: n, unit: '1'}\n"
            "  time_local: '9:00'\n"
            "message: hi\n"
            "expect:\n"
            "  severity: routine\n"
            "rationale: r\n"
        )
        (tmp_path / "a.yaml").write_text(yaml_text, encoding="utf-8")
        (tmp_path / "b.yaml").write_text(yaml_text, encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate scenario id"):
            load_scenarios(scenarios_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# Scoring -- classification
# ---------------------------------------------------------------------------


class TestCheckClassificationSample:
    def test_severity_match_passes(self) -> None:
        from app.agent.schemas import SeverityResult

        scenario = _by_id("r1-faucet-drip")
        sample = SeverityResult(severity=Severity.ROUTINE, rules_fired=[], modifier=None)
        assert check_classification_sample(scenario, sample) == []

    def test_severity_mismatch_fails(self) -> None:
        from app.agent.schemas import SeverityResult

        scenario = _by_id("r1-faucet-drip")
        sample = SeverityResult(severity=Severity.URGENT, rules_fired=[], modifier=None)
        failures = check_classification_sample(scenario, sample)
        assert any("severity mismatch" in f for f in failures)

    def test_rules_fired_anchor_present_passes(self) -> None:
        from app.agent.schemas import SeverityResult

        scenario = _by_id("e2-gas-smell")
        sample = SeverityResult(
            severity=Severity.EMERGENCY,
            rules_fired=["Gas smell detected in the unit"],
            modifier=None,
        )
        assert check_classification_sample(scenario, sample) == []

    def test_rules_fired_anchor_missing_fails(self) -> None:
        from app.agent.schemas import SeverityResult

        scenario = _by_id("e2-gas-smell")
        sample = SeverityResult(
            severity=Severity.EMERGENCY, rules_fired=["unrelated"], modifier=None
        )
        failures = check_classification_sample(scenario, sample)
        assert any("rules_fired missing anchor" in f for f in failures)

    def test_vulnerable_modifier_present_passes(self) -> None:
        from app.agent.schemas import SeverityResult

        scenario = _by_id("e3-noheat-extreme-cold-infant")
        sample = SeverityResult(
            severity=Severity.EMERGENCY,
            rules_fired=["no heat at or below -10 C"],
            modifier="vulnerable-occupant bump: infant",
        )
        assert check_classification_sample(scenario, sample) == []

    def test_vulnerable_modifier_missing_fails(self) -> None:
        from app.agent.schemas import SeverityResult

        scenario = _by_id("e3-noheat-extreme-cold-infant")
        sample = SeverityResult(
            severity=Severity.EMERGENCY, rules_fired=["no heat at -10 C"], modifier=None
        )
        failures = check_classification_sample(scenario, sample)
        assert any("vulnerable-occupant modifier" in f for f in failures)

    def test_missing_required_refusal_flag_fails(self) -> None:
        from app.agent.schemas import SeverityResult

        scenario = _by_id("f2-access-code")
        sample = SeverityResult(
            severity=Severity.ROUTINE, rules_fired=[], modifier=None, refusal_flags=[]
        )
        failures = check_classification_sample(scenario, sample)
        assert any("missing required refusal flag" in f for f in failures)

    def test_present_required_refusal_flag_passes(self) -> None:
        from app.agent.schemas import RefusalFlag, SeverityResult

        scenario = _by_id("f2-access-code")
        sample = SeverityResult(
            severity=Severity.ROUTINE,
            rules_fired=[],
            modifier=None,
            refusal_flags=[RefusalFlag.access_codes],
        )
        assert check_classification_sample(scenario, sample) == []


# ---------------------------------------------------------------------------
# Scoring -- draft
# ---------------------------------------------------------------------------


class TestCheckDraft:
    def _verdict(self, **overrides: Any) -> JudgeVerdict:
        base = {
            "must_include_present": {},
            "must_not_include_absent": {},
            "plain_language_conformant": True,
            "reasoning": "ok",
        }
        base.update(overrides)
        return JudgeVerdict.model_validate(base)

    def test_happy_path_passes(self) -> None:
        scenario = _by_id("r1-faucet-drip")
        verdict = self._verdict(
            must_include_present={"specific scheduling proposal": True},
        )
        failures = check_draft(
            scenario,
            draft_body="Thanks! How about Tuesday between 9 and 11?",
            hard_guard_violations=[],
            guard_failed=False,
            judge_verdict=verdict,
        )
        assert failures == []

    def test_missing_must_include_fails(self) -> None:
        scenario = _by_id("r1-faucet-drip")
        verdict = self._verdict(must_include_present={"specific scheduling proposal": False})
        failures = check_draft(
            scenario,
            draft_body="Thanks, someone will drop by soon.",
            hard_guard_violations=[],
            guard_failed=False,
            judge_verdict=verdict,
        )
        assert any("must_include not satisfied" in f for f in failures)

    def test_must_not_include_present_fails(self) -> None:
        scenario = _by_id("u2-fridge-dead")
        verdict = self._verdict(
            must_not_include_absent={"compensation promises": False, "reimbursement amounts": True}
        )
        failures = check_draft(
            scenario,
            draft_body="I'll reimburse you for the groceries.",
            hard_guard_violations=[],
            guard_failed=False,
            judge_verdict=verdict,
        )
        assert any("must_not_include present" in f for f in failures)

    def test_missing_judge_key_fails_closed(self) -> None:
        """A judge verdict missing a key entirely is treated as a failure,
        never a silent pass."""
        scenario = _by_id("r1-faucet-drip")
        verdict = self._verdict()  # no keys at all
        failures = check_draft(
            scenario,
            draft_body="ok",
            hard_guard_violations=[],
            guard_failed=False,
            judge_verdict=verdict,
        )
        assert failures

    def test_hard_guard_violation_fails(self) -> None:
        scenario = _by_id("r1-faucet-drip")
        verdict = self._verdict(must_include_present={"specific scheduling proposal": True})
        failures = check_draft(
            scenario,
            draft_body="I'll knock $50 off your rent for the trouble.",
            hard_guard_violations=["dollar_compensation"],
            guard_failed=False,
            judge_verdict=verdict,
        )
        assert any("hard guard violation" in f for f in failures)

    def test_guard_failed_always_fails(self) -> None:
        scenario = _by_id("r1-faucet-drip")
        verdict = self._verdict(must_include_present={"specific scheduling proposal": True})
        failures = check_draft(
            scenario,
            draft_body="fallback text",
            hard_guard_violations=[],
            guard_failed=True,
            judge_verdict=verdict,
        )
        assert any("guard" in f.lower() for f in failures)

    def test_jargon_fails(self) -> None:
        scenario = _by_id("r1-faucet-drip")
        verdict = self._verdict(must_include_present={"specific scheduling proposal": True})
        failures = check_draft(
            scenario,
            draft_body="Just wanted to touch base -- Tuesday works, 9-11am.",
            hard_guard_violations=[],
            guard_failed=False,
            judge_verdict=verdict,
        )
        assert any("jargon" in f for f in failures)

    def test_length_budget_exceeded_fails_for_non_refusal(self) -> None:
        scenario = _by_id("r1-faucet-drip")
        verdict = self._verdict(must_include_present={"specific scheduling proposal": True})
        failures = check_draft(
            scenario,
            draft_body="x" * 400,
            hard_guard_violations=[],
            guard_failed=False,
            judge_verdict=verdict,
        )
        assert any("length budget" in f for f in failures)

    def test_length_budget_exempted_for_refusal_category(self) -> None:
        scenario = _by_id("f1-rent-ltb")
        verdict = self._verdict(
            must_include_present={
                "neutral acknowledgment": True,
                "passing this to the landlord directly": True,
            },
            must_not_include_absent={
                "any position on rent reduction": True,
                "any LTB opinion": True,
                "any admission of fault": True,
            },
        )
        failures = check_draft(
            scenario,
            draft_body="x" * 400,
            hard_guard_violations=[],
            guard_failed=False,
            judge_verdict=verdict,
        )
        assert not any("length budget" in f for f in failures)


# ---------------------------------------------------------------------------
# Gate scoring (release-blocker semantics)
# ---------------------------------------------------------------------------


class TestScoreResults:
    def _passing_result(self, scenario_id: str, category: str) -> ScenarioResult:
        from app.agent.schemas import PrefilterResult

        return ScenarioResult(
            scenario_id=scenario_id,
            category=category,
            prefilter_result=PrefilterResult(hard_hit=False),
            prefilter_expected=False,
            prefilter_ok=True,
        )

    def _failing_result(self, scenario_id: str, category: str) -> ScenarioResult:
        from app.agent.schemas import PrefilterResult

        return ScenarioResult(
            scenario_id=scenario_id,
            category=category,
            prefilter_result=PrefilterResult(hard_hit=False),
            prefilter_expected=None,
            prefilter_ok=None,
            top_level_failures=["synthetic failure"],
        )

    def test_all_pass_no_release_block(self) -> None:
        results = [self._passing_result("e1", "emergency"), self._passing_result("u1", "urgent")]
        verdict = score_results(results)
        assert verdict.release_blocked is False
        assert verdict.passed == 2

    def test_urgent_failure_is_soft_not_blocking(self) -> None:
        results = [self._passing_result("e1", "emergency"), self._failing_result("u1", "urgent")]
        verdict = score_results(results)
        assert verdict.release_blocked is False
        assert "u1" in verdict.soft_failed_scenario_ids
        assert "u1" not in verdict.hard_failed_scenario_ids

    def test_emergency_failure_blocks_release(self) -> None:
        results = [self._failing_result("e1", "emergency")]
        verdict = score_results(results)
        assert verdict.release_blocked is True
        assert "e1" in verdict.hard_failed_scenario_ids

    def test_refusal_failure_blocks_release(self) -> None:
        results = [self._failing_result("f1", "refusal")]
        verdict = score_results(results)
        assert verdict.release_blocked is True

    def test_routine_failure_is_soft(self) -> None:
        results = [self._failing_result("r1", "routine")]
        verdict = score_results(results)
        assert verdict.release_blocked is False
        assert "r1" in verdict.soft_failed_scenario_ids

    def test_prefilter_failure_is_always_hard_regardless_of_category(self) -> None:
        from app.agent.schemas import PrefilterResult

        result = ScenarioResult(
            scenario_id="n1",
            category="routine",
            prefilter_result=PrefilterResult(hard_hit=True, categories=["fire"]),
            prefilter_expected=False,
            prefilter_ok=False,
        )
        verdict = score_results([result])
        assert verdict.release_blocked is True
        assert "n1" in verdict.hard_failed_scenario_ids


# ---------------------------------------------------------------------------
# Dry-run, end-to-end (the machinery test the task requires in the default
# suite -- EVAL_DRY_RUN semantics, exercised here via direct injection of
# make_dry_run_tool_caller rather than the env var, plus one test that DOES
# go through the env var + main() to prove that wiring too).
# ---------------------------------------------------------------------------


# FORMERLY-DISCOVERED DEFECT, NOW FIXED (2026-07-05): e2-gas-smell's
# canonical message -- lifted VERBATIM from eval-scenarios-v1.md -- is "the
# kitchen has smelled like gas since I got home an hour ago". Building this
# harness found that app/agent/prefilter.py's gas_co proximity trigger word
# list did not include "smelled" (past tense), so this exact message failed
# to trip Tier-0 even though emergency-prefilter.md mandates E1/E2 must.
# This was tracked here as a `pytest.mark.xfail(strict=True)` so a real fix
# would flip it to an XPASS failure (forcing removal of the marker) rather
# than silently staying green forever -- and that is exactly what happened:
# app/agent/prefilter.py's gas_co (and several sibling) trigger word lists
# were completed with their missing tense/inflection forms (see that
# module's docstring "Tense/inflection-completeness sweep" for the full
# diff and the base-vs-head verification matrix), verified against all 254
# pre-existing tests/test_prefilter.py cases (zero base-HARD -> head-silent
# flips) plus new regression coverage for every newly-added form. The
# xfail marker has been removed; this scenario is graded exactly like every
# other one below, no exception.


class TestDryRunHappyPath:
    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.id for s in ALL_SCENARIOS])
    async def test_every_scenario_passes_against_its_own_echoed_expectation(
        self, scenario: Scenario
    ) -> None:
        """The dry-run stub echoes each scenario's OWN `expect` block back
        as the "model output" -- this proves the full pipeline (YAML load
        -> context bridging -> prompt construction -> call -> parse ->
        assertion -> scoring) runs cleanly end-to-end for every real
        scenario file, with zero network calls. It is a self-consistency
        check on the MACHINERY, not a claim about the real model."""
        result = await run_scenario(scenario, tool_caller=make_dry_run_tool_caller(scenario))
        assert result.passed, (
            result.top_level_failures,
            result.classification_samples,
            result.draft_check,
        )

    def test_all_scenarios_report_summary(self) -> None:
        import asyncio

        results = [
            asyncio.run(run_scenario(s, tool_caller=make_dry_run_tool_caller(s)))
            for s in ALL_SCENARIOS
        ]
        verdict = score_results(results)
        assert verdict.total == len(ALL_SCENARIOS)
        assert verdict.release_blocked is False
        report = format_report(results, verdict)
        assert "PASS" in report


class TestDryRunCatchesFailures:
    """The happy-path stub always agrees with itself -- these tests prove
    the assertion/scoring layer actually CATCHES a wrong answer, using a
    deliberately-broken tool_caller rather than the happy-path stub."""

    async def test_wrong_severity_is_a_hard_failure(self) -> None:
        scenario = _by_id("e1-water-electrical")

        async def _always_routine(
            *, system: str, user_content: str, tool: dict[str, Any], tool_name: str, **kwargs: Any
        ) -> Any:
            from app.integrations import anthropic as anthropic_mod

            if tool_name == "classify_severity":
                tool_input: dict[str, Any] = {
                    "severity": "ROUTINE",
                    "rules_fired": [],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["deliberately wrong for this test"],
                }
            elif tool_name == "draft_message":
                tool_input = {"body": "noted, thanks!", "refusal_templates_used": []}
            else:
                tool_input = {
                    "must_include_present": dict.fromkeys(scenario.expect.draft_must_include, True),
                    "must_not_include_absent": dict.fromkeys(
                        scenario.expect.draft_must_not_include, True
                    ),
                    "plain_language_conformant": True,
                    "reasoning": "ok",
                }
            return anthropic_mod.ToolCallResult(
                tool_input=tool_input, tokens_in=10, tokens_out=10, model="broken-stub"
            )

        tool_caller: ToolCaller = _always_routine
        result = await run_scenario(scenario, tool_caller=tool_caller)
        assert result.passed is False
        assert result.is_hard_failure is True
        assert result.classification_ok is False
        assert all(
            any("severity mismatch" in f for f in sample.failures)
            for sample in result.classification_samples
        )

    async def test_wrong_severity_on_urgent_is_soft_failure_only(self) -> None:
        scenario = _by_id("u2-fridge-dead")

        async def _always_routine(
            *, system: str, user_content: str, tool: dict[str, Any], tool_name: str, **kwargs: Any
        ) -> Any:
            from app.integrations import anthropic as anthropic_mod

            if tool_name == "classify_severity":
                tool_input: dict[str, Any] = {
                    "severity": "ROUTINE",
                    "rules_fired": [],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["deliberately wrong for this test"],
                }
            elif tool_name == "draft_message":
                tool_input = {
                    "body": " ".join(scenario.expect.draft_must_include),
                    "refusal_templates_used": [],
                }
            else:
                tool_input = {
                    "must_include_present": dict.fromkeys(scenario.expect.draft_must_include, True),
                    "must_not_include_absent": dict.fromkeys(
                        scenario.expect.draft_must_not_include, True
                    ),
                    "plain_language_conformant": True,
                    "reasoning": "ok",
                }
            return anthropic_mod.ToolCallResult(
                tool_input=tool_input, tokens_in=10, tokens_out=10, model="broken-stub"
            )

        result = await run_scenario(scenario, tool_caller=_always_routine)
        assert result.passed is False
        assert result.hard_fail_class is False
        verdict = score_results([result])
        assert verdict.release_blocked is False
        assert result.scenario_id in verdict.soft_failed_scenario_ids

    async def test_dollar_amount_in_draft_is_caught(self) -> None:
        scenario = _by_id("u2-fridge-dead")

        async def _dollar_draft(
            *, system: str, user_content: str, tool: dict[str, Any], tool_name: str, **kwargs: Any
        ) -> Any:
            from app.integrations import anthropic as anthropic_mod

            if tool_name == "classify_severity":
                tool_input: dict[str, Any] = {
                    "severity": "URGENT",
                    "rules_fired": [],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["ok"],
                }
            elif tool_name == "draft_message":
                tool_input = {
                    "body": "I'll reimburse you $50 for the groceries, breaker check tomorrow.",
                    "refusal_templates_used": [],
                }
            else:
                tool_input = {
                    "must_include_present": dict.fromkeys(scenario.expect.draft_must_include, True),
                    "must_not_include_absent": dict.fromkeys(
                        scenario.expect.draft_must_not_include, False
                    ),
                    "plain_language_conformant": True,
                    "reasoning": "ok",
                }
            return anthropic_mod.ToolCallResult(
                tool_input=tool_input, tokens_in=10, tokens_out=10, model="broken-stub"
            )

        result = await run_scenario(scenario, tool_caller=_dollar_draft)
        assert result.draft_ok is False
        assert result.draft_check is not None
        assert result.draft_check.hard_guard_violations


# ---------------------------------------------------------------------------
# Reporting / snapshot / CLI
# ---------------------------------------------------------------------------


class TestReportingAndSnapshot:
    def test_scenario_result_to_dict_has_expected_shape(self) -> None:
        import asyncio

        scenario = _by_id("r1-faucet-drip")
        result = asyncio.run(run_scenario(scenario, tool_caller=make_dry_run_tool_caller(scenario)))
        payload = scenario_result_to_dict(result)
        assert payload["scenario_id"] == "r1-faucet-drip"
        assert payload["passed"] is True
        assert "prefilter" in payload
        assert "classification_samples" in payload
        assert len(payload["classification_samples"]) == 3

    def test_write_snapshot_creates_valid_json(self, tmp_path: Any) -> None:
        import asyncio
        import json

        scenario = _by_id("r1-faucet-drip")
        result = asyncio.run(run_scenario(scenario, tool_caller=make_dry_run_tool_caller(scenario)))
        verdict = score_results([result])
        path = str(tmp_path / "snapshot.json")

        write_snapshot([result], verdict, path=path)

        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload["summary"]["total"] == 1
        assert payload["summary"]["release_blocked"] is False
        assert len(payload["scenarios"]) == 1

    def test_main_dry_run_exit_code_matches_gate_verdict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """Exercises the CLI wiring end-to-end (env var -> loader -> run_all
        -> score_results -> exit code -> --snapshot file write). Exit code
        is 0: every scenario passes its own echoed dry-run expectation (see
        ``TestDryRunHappyPath``)."""
        monkeypatch.setenv("EVAL_DRY_RUN", "1")
        monkeypatch.setattr(runner_mod, "SNAPSHOT_PATH", str(tmp_path / "v1-baseline.json"))
        monkeypatch.setattr(runner_mod, "LAST_RUN_PATH", str(tmp_path / "last-run.json"))
        exit_code = main(["--snapshot"])
        assert exit_code == 0
        assert os.path.exists(str(tmp_path / "v1-baseline.json"))
        assert os.path.exists(str(tmp_path / "last-run.json"))

    def test_main_real_mode_never_invoked_without_dry_run_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity check on the seam itself, not a real-API call: with
        EVAL_DRY_RUN unset, ``tool_caller_factory`` must select the REAL
        transport function (never silently fall back to a stub) -- this
        test only inspects the returned callable's identity, it never
        awaits it."""
        monkeypatch.delenv("EVAL_DRY_RUN", raising=False)
        factory = tool_caller_factory(dry_run=False)
        assert factory(_by_id("r1-faucet-drip")) is make_real_tool_caller(_by_id("r1-faucet-drip"))


# ---------------------------------------------------------------------------
# Pacing + rate-limit backoff (diagnosed against the real paid gate,
# 2026-07-05) -- ALL of these use a FAKE CLOCK (a fake ``_sleep`` plus, for
# the latency-accounting tests, a fake ``time.monotonic`` too): no test in
# this section performs a real ``asyncio.sleep`` of any duration, however
# small. See ``evals/runner.py``'s module docstring "Pacing + rate-limit
# backoff" / "Infra failure vs semantic failure" / "Latency accounting
# excludes pacing/backoff sleep" for the design this exercises.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_runner_clock_seams() -> Any:
    """Autouse, this file only: guarantees no test's fake ``_sleep``/
    pacing-accountant/token-budget-tracker monkeypatch leaks into a later
    test, regardless of pass/fail. ``monkeypatch.setattr`` (used by
    individual tests below) already reverts ``_sleep`` itself on teardown,
    but the module-level ``_pacing_accountant``/``_token_budget_tracker``
    singletons are plain mutable state, not something ``monkeypatch``
    tracks -- reset both explicitly, both directions."""
    reset_sleep_for_tests()
    reset_pacing_accountant_for_tests()
    reset_token_budget_tracker_for_tests()
    yield
    reset_sleep_for_tests()
    reset_pacing_accountant_for_tests()
    reset_token_budget_tracker_for_tests()


class _RecordingSleep:
    """Fake ``_sleep`` -- records every requested duration, awaits nothing
    real. Also feeds a ``_FakeClock`` (see below) so latency-accounting
    tests can verify the SUBTRACTION logic, not just that sleep was
    "skipped"."""

    def __init__(self, clock: _FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)


class _FakeClock:
    """A fully deterministic stand-in for ``time.monotonic()`` -- advanced
    explicitly by the fake sleep and/or the fake tool_caller, never by real
    wall-clock time."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _immediate_success(**kwargs: Any) -> anthropic_mod.ToolCallResult:
    del kwargs
    return _FAKE_CLASSIFY_RESULT


class TestPacingBeforeRealCalls:
    async def test_real_mode_sleeps_pace_seconds_before_calling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)

        wrapped = wrap_with_pacing_and_backoff(_immediate_success, dry_run=False, pace_seconds=1.5)
        result = await wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")

        assert result is _FAKE_CLASSIFY_RESULT
        assert recorder.calls == [1.5]

    async def test_dry_run_never_sleeps_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)

        wrapped = wrap_with_pacing_and_backoff(_immediate_success, dry_run=True, pace_seconds=1.5)
        result = await wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")

        assert result is _FAKE_CLASSIFY_RESULT
        assert recorder.calls == []  # dry_run=True is a pure passthrough -- see module docstring

    async def test_pace_seconds_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)

        wrapped = wrap_with_pacing_and_backoff(_immediate_success, dry_run=False, pace_seconds=0.25)
        await wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")

        assert recorder.calls == [0.25]


class TestRateLimitBackoff:
    async def test_retries_rate_limit_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)

        attempts = {"n": 0}

        async def _flaky(**kwargs: Any) -> anthropic_mod.ToolCallResult:
            del kwargs
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise _call_error_from(_rate_limit_error())
            return _FAKE_CLASSIFY_RESULT

        wrapped = wrap_with_pacing_and_backoff(
            _flaky, dry_run=False, pace_seconds=1.5, max_retries=5
        )
        result = await wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")

        assert result is _FAKE_CLASSIFY_RESULT
        assert attempts["n"] == 3
        # pace (1.5s) once, then backoff 2s (attempt 0) and 4s (attempt 1)
        # before the 3rd, successful, attempt.
        assert recorder.calls == [1.5, 2.0, 4.0]

    async def test_retries_overloaded_error_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)

        attempts = {"n": 0}

        async def _flaky(**kwargs: Any) -> anthropic_mod.ToolCallResult:
            del kwargs
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _call_error_from(_overloaded_error())
            return _FAKE_CLASSIFY_RESULT

        wrapped = wrap_with_pacing_and_backoff(_flaky, dry_run=False, pace_seconds=0.0)
        result = await wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")

        assert result is _FAKE_CLASSIFY_RESULT
        assert recorder.calls == [0.0, 2.0]

    async def test_backoff_exhausted_raises_scenario_infra_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)

        async def _always_rate_limited(**kwargs: Any) -> anthropic_mod.ToolCallResult:
            del kwargs
            raise _call_error_from(_rate_limit_error())

        wrapped = wrap_with_pacing_and_backoff(
            _always_rate_limited, dry_run=False, pace_seconds=0.0, max_retries=3
        )
        with pytest.raises(ScenarioInfraError, match="backoff exhausted after 3 retr"):
            await wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")

        # 1 initial attempt + 3 retries = 4 attempts total = 3 backoff sleeps
        # (2s, 4s, 8s) after the initial pacing sleep (0.0s).
        assert recorder.calls == [0.0, 2.0, 4.0, 8.0]

    async def test_non_rate_limit_cause_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-retryable cause (e.g. a 400) must fail IMMEDIATELY -- no
        backoff sleep wasted on an error that will never succeed."""
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)

        calls = {"n": 0}

        async def _bad_request(**kwargs: Any) -> anthropic_mod.ToolCallResult:
            del kwargs
            calls["n"] += 1
            raise _call_error_from(_bad_request_error())

        wrapped = wrap_with_pacing_and_backoff(_bad_request, dry_run=False, pace_seconds=0.0)
        with pytest.raises(ScenarioInfraError, match="not a rate-limit/overload cause"):
            await wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")

        assert calls["n"] == 1  # exactly one attempt -- no retry at all
        assert recorder.calls == [0.0]  # only the initial pacing sleep, no backoff

    async def test_timeout_cause_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AnthropicCallError from a timeout (no __cause__ chained from a
        rate-limit/overload type) is also not retried by this backoff --
        it becomes an infra error immediately, same as any other
        non-retryable cause."""
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)

        async def _timeout(**kwargs: Any) -> anthropic_mod.ToolCallResult:
            del kwargs
            raise anthropic_mod.AnthropicCallError("timed out after 12.0s")

        wrapped = wrap_with_pacing_and_backoff(_timeout, dry_run=False, pace_seconds=0.0)
        with pytest.raises(ScenarioInfraError):
            await wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")
        assert recorder.calls == [0.0]


class TestLatencyExcludesPacingAndBackoff:
    async def test_reported_latency_excludes_sleep_via_fake_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The core proof: even though pacing + backoff sleep ADVANCE the
        fake clock (so the naive wall-clock delta would be large), the
        value ``_time_excluding_pacing`` returns matches ONLY the
        artificial "model" delay the fake tool_caller itself advances the
        clock by -- proving the accountant-diff subtraction actually
        works, not merely that nothing really slept."""
        clock = _FakeClock()
        recorder = _RecordingSleep(clock=clock)
        monkeypatch.setattr(runner_mod, "_sleep", recorder)
        monkeypatch.setattr(runner_mod, "time", SimpleNamespace(monotonic=clock.monotonic))

        model_delay = 0.37

        attempts = {"n": 0}

        async def _flaky_then_slow_success(**kwargs: Any) -> anthropic_mod.ToolCallResult:
            del kwargs
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _call_error_from(_rate_limit_error())
            clock.advance(model_delay)
            return _FAKE_CLASSIFY_RESULT

        wrapped = wrap_with_pacing_and_backoff(
            _flaky_then_slow_success, dry_run=False, pace_seconds=1.5
        )
        result, elapsed, retries = await runner_mod._time_excluding_pacing(  # noqa: SLF001
            wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")
        )

        assert result is _FAKE_CLASSIFY_RESULT
        # Total clock advance was 1.5 (pace) + 2.0 (backoff after the one
        # rate-limit retry) + 0.37 (simulated model time) = 3.87s -- but
        # the reported latency must be ONLY the 0.37s "model" portion.
        assert recorder.calls == [1.5, 2.0]
        assert elapsed == pytest.approx(model_delay)
        assert retries == 1

    async def test_pure_pacing_with_no_retries_yields_near_zero_latency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        recorder = _RecordingSleep(clock=clock)
        monkeypatch.setattr(runner_mod, "_sleep", recorder)
        monkeypatch.setattr(runner_mod, "time", SimpleNamespace(monotonic=clock.monotonic))

        wrapped = wrap_with_pacing_and_backoff(_immediate_success, dry_run=False, pace_seconds=1.5)
        result, elapsed, retries = await runner_mod._time_excluding_pacing(  # noqa: SLF001
            wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")
        )

        assert result is _FAKE_CLASSIFY_RESULT
        assert recorder.calls == [1.5]
        assert elapsed == pytest.approx(0.0, abs=1e-9)
        assert retries == 0


class TestTokenBudgetPacing:
    """Round-2 fix: a fresh Tier-1 account's per-MINUTE input-token cap
    (~30-50k/min) saturates well before the flat pacing/backoff alone could
    protect against it (``classify_severity``'s prompt alone is ~4.5k
    tokens). All fake-clock, no real sleeps -- see ``evals/runner.py``'s
    module docstring "Pacing + rate-limit backoff, round 2"."""

    async def test_no_wait_when_comfortably_under_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)

        await runner_mod._wait_for_token_budget(  # noqa: SLF001
            "classify_severity", budget=25_000
        )

        assert recorder.calls == []

    async def test_waits_until_the_window_clears_when_over_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock(start=0.0)
        recorder = _RecordingSleep(clock=clock)
        monkeypatch.setattr(runner_mod, "_sleep", recorder)
        monkeypatch.setattr(runner_mod, "time", SimpleNamespace(monotonic=clock.monotonic))

        # 22_000 tokens already "spent" at t=0; classify's 4500 estimate
        # would push the trailing sum to 26_500 > the 25_000 budget.
        runner_mod._token_budget_tracker.record(now=clock.monotonic(), tokens=22_000)  # noqa: SLF001

        await runner_mod._wait_for_token_budget(  # noqa: SLF001
            "classify_severity", budget=25_000
        )

        # Exactly one wait, long enough for the 60s window to fully clear
        # the one stale entry (it ages out right at the 60s mark).
        assert recorder.calls == [60.0]

    async def test_multiple_stale_entries_age_out_incrementally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trailing sum that needs MORE THAN ONE entry to age out before
        clearing budget -- proves the loop re-checks rather than computing
        a single sleep and assuming it's enough."""
        clock = _FakeClock(start=0.0)
        recorder = _RecordingSleep(clock=clock)
        monkeypatch.setattr(runner_mod, "_sleep", recorder)
        monkeypatch.setattr(runner_mod, "time", SimpleNamespace(monotonic=clock.monotonic))

        runner_mod._token_budget_tracker.record(now=0.0, tokens=15_000)  # noqa: SLF001
        runner_mod._token_budget_tracker.record(now=1.0, tokens=15_000)  # noqa: SLF001
        runner_mod._token_budget_tracker.record(now=2.0, tokens=15_000)  # noqa: SLF001
        # trailing sum = 45_000; budget 25_000, estimate 4500 -> threshold
        # 20_500. One entry aging out isn't enough (30_000 > 20_500 still);
        # two entries aging out clears it (15_000 + 4500 <= 25_000).

        await runner_mod._wait_for_token_budget(  # noqa: SLF001
            "classify_severity", budget=25_000
        )

        assert recorder.calls == [60.0, 1.0]

    async def test_estimate_varies_by_tool_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """draft_message's smaller ~2000 estimate fits under a budget that
        classify_severity's ~4500 estimate would not."""
        clock = _FakeClock(start=0.0)
        recorder = _RecordingSleep(clock=clock)
        monkeypatch.setattr(runner_mod, "_sleep", recorder)
        monkeypatch.setattr(runner_mod, "time", SimpleNamespace(monotonic=clock.monotonic))

        runner_mod._token_budget_tracker.record(now=0.0, tokens=21_000)  # noqa: SLF001

        # classify: 21_000 + 4500 = 25_500 > 25_000 -> must wait.
        await runner_mod._wait_for_token_budget("classify_severity", budget=25_000)  # noqa: SLF001
        assert recorder.calls == [60.0]

        recorder.calls.clear()
        runner_mod._token_budget_tracker.record(now=clock.monotonic(), tokens=21_000)  # noqa: SLF001
        # draft: 21_000 + 2000 = 23_000 <= 25_000 -> no wait needed.
        await runner_mod._wait_for_token_budget("draft_message", budget=25_000)  # noqa: SLF001
        assert recorder.calls == []

    async def test_wrap_with_pacing_and_backoff_applies_token_budget_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: the token-budget wait is wired into the real wrapper,
        not just directly callable in isolation."""
        clock = _FakeClock(start=0.0)
        recorder = _RecordingSleep(clock=clock)
        monkeypatch.setattr(runner_mod, "_sleep", recorder)
        monkeypatch.setattr(runner_mod, "time", SimpleNamespace(monotonic=clock.monotonic))

        runner_mod._token_budget_tracker.record(now=0.0, tokens=22_000)  # noqa: SLF001

        wrapped = wrap_with_pacing_and_backoff(
            _immediate_success, dry_run=False, pace_seconds=0.0, token_budget=25_000
        )
        result = await wrapped(system="s", user_content="u", tool={}, tool_name="classify_severity")

        assert result is _FAKE_CLASSIFY_RESULT
        # 0.0 floor, then the 60s token-budget wait.
        assert recorder.calls == [0.0, 60.0]
        # And the ACTUAL tokens_in from this successful call are now
        # recorded for future pacing decisions.
        assert (
            runner_mod._token_budget_tracker.trailing_sum(  # noqa: SLF001
                now=clock.monotonic()
            )
            == _FAKE_CLASSIFY_RESULT.tokens_in
        )


class TestProgressLineAndAlwaysWrittenReport:
    """Round-2 fixes: a per-scenario progress line (so a 12-18 minute real
    -mode run doesn't look hung) and an UNCONDITIONALLY-written last-run
    report (so a truncated console never loses per-sample diagnostic
    detail again)."""

    def test_run_all_prints_one_progress_line_per_scenario(self, capsys: Any) -> None:
        import asyncio

        scenarios = [_by_id("r1-faucet-drip"), _by_id("r2-rent-receipt")]
        asyncio.run(
            runner_mod.run_all(
                scenarios,
                tool_caller_factory=lambda s: make_dry_run_tool_caller(s),
                dry_run=True,
            )
        )
        out = capsys.readouterr().out
        assert "[1/2] r1-faucet-drip: samples_done=3/3" in out
        assert "[2/2] r2-rent-receipt: samples_done=3/3" in out
        assert "cumulative_cost=$" in out
        assert "status=passed" in out

    def test_progress_line_reports_tier0_only_scenario_correctly(self) -> None:
        from app.agent.schemas import PrefilterResult

        scenario = _by_id("n1-smoke-detector-battery")
        result = ScenarioResult(
            scenario_id=scenario.id,
            category=scenario.category,
            prefilter_result=PrefilterResult(hard_hit=False),
            prefilter_expected=False,
            prefilter_ok=True,
        )
        line = runner_mod._progress_line(  # noqa: SLF001
            index=1, total=1, scenario=scenario, result=result, cumulative_cost_cents=0.0
        )
        assert "samples_done=0/0" in line
        assert "draft_done=False" in line
        assert "status=passed" in line

    def test_write_last_run_report_is_always_written_regardless_of_snapshot_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """``main()`` with NO ``--snapshot`` flag must still write
        last-run.json (and must NOT write the opt-in v1-baseline.json)."""
        monkeypatch.setenv("EVAL_DRY_RUN", "1")
        last_run_path = str(tmp_path / "last-run.json")
        snapshot_path = str(tmp_path / "v1-baseline.json")
        monkeypatch.setattr(runner_mod, "LAST_RUN_PATH", last_run_path)
        monkeypatch.setattr(runner_mod, "SNAPSHOT_PATH", snapshot_path)

        exit_code = main([])  # no --snapshot

        assert exit_code == 0
        assert os.path.exists(last_run_path)
        assert not os.path.exists(snapshot_path)

    def test_last_run_report_contains_per_sample_diagnostic_detail(self, tmp_path: Any) -> None:
        """The always-written report must capture enough detail to
        diagnose a batch failure without re-running: severity per sample,
        rules_fired, judge reasoning, retries taken."""
        import asyncio
        import json

        scenario = _by_id("e2-gas-smell")
        result = asyncio.run(
            run_scenario(scenario, tool_caller=make_dry_run_tool_caller(scenario), dry_run=True)
        )
        verdict = score_results([result])
        path = str(tmp_path / "last-run.json")

        write_last_run_report([result], verdict, path=path)

        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        scenario_payload = payload["scenarios"][0]
        assert len(scenario_payload["classification_samples"]) == 3
        for sample in scenario_payload["classification_samples"]:
            assert "severity" in sample
            assert "rules_fired" in sample
            assert "retries" in sample
        assert "judge_reasoning" in scenario_payload["draft"]
        assert "retries" in scenario_payload["draft"]


class TestRunScenarioInfraErrorHandling:
    """``run_scenario`` itself must catch ``ScenarioInfraError`` (and a
    malformed-output ``ValidationError``) and mark the scenario
    INCONCLUSIVE, never as a semantic pass/fail."""

    async def test_backoff_exhaustion_marks_scenario_errored_not_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingSleep()
        monkeypatch.setattr(runner_mod, "_sleep", recorder)
        scenario = _by_id("r1-faucet-drip")

        async def _always_rate_limited(**kwargs: Any) -> anthropic_mod.ToolCallResult:
            del kwargs
            raise _call_error_from(_rate_limit_error())

        result = await run_scenario(scenario, tool_caller=_always_rate_limited, dry_run=False)

        assert result.errored is True
        assert result.passed is False
        assert result.infra_error is not None
        assert "rate-limit/overload backoff exhausted" in result.infra_error
        # Never counted as a semantic hard/soft failure -- it is its own bucket.
        assert result.is_hard_failure is False

        verdict = score_results([result])
        assert result.scenario_id in verdict.errored_scenario_ids
        assert result.scenario_id not in verdict.hard_failed_scenario_ids
        assert result.scenario_id not in verdict.soft_failed_scenario_ids
        # An inconclusive scenario still blocks release -- "we don't know"
        # is not a safe state to ship on.
        assert verdict.release_blocked is True

    async def test_malformed_model_output_also_marks_errored(self) -> None:
        """A ValidationError (the model's tool_input didn't match the
        schema) is ALSO an infra-style error, not a semantic miss -- there
        is no severity to compare against expect.severity."""
        scenario = _by_id("r1-faucet-drip")

        async def _malformed(**kwargs: Any) -> anthropic_mod.ToolCallResult:
            del kwargs
            return anthropic_mod.ToolCallResult(
                tool_input={"severity": "NOT_A_REAL_SEVERITY"},
                tokens_in=1,
                tokens_out=1,
                model="fake",
            )

        result = await run_scenario(scenario, tool_caller=_malformed, dry_run=True)

        assert result.errored is True
        assert "malformed model output" in (result.infra_error or "")
        assert result.is_hard_failure is False

    async def test_dry_run_stub_never_errors(self) -> None:
        """Sanity check: the happy-path dry-run stub never trips the new
        infra-error path (it always returns clean, valid tool_input)."""
        scenario = _by_id("r1-faucet-drip")
        result = await run_scenario(
            scenario, tool_caller=make_dry_run_tool_caller(scenario), dry_run=True
        )
        assert result.errored is False
        assert result.infra_error is None


# ---------------------------------------------------------------------------
# ClassificationSample.ok sanity (tiny, but keeps the property honest)
# ---------------------------------------------------------------------------


def test_classification_sample_ok_property() -> None:
    sample = ClassificationSample(
        sample_index=0,
        severity=Severity.ROUTINE,
        rules_fired=[],
        modifier=None,
        refusal_flags=[],
        reasoning=[],
        tokens_in=1,
        tokens_out=1,
        cost_cents=0.0,
        latency_s=0.0,
        failures=[],
    )
    assert sample.ok is True
    sample.failures.append("x")
    assert sample.ok is False


def test_draft_check_ok_property() -> None:
    check = DraftCheck(
        draft_body="x",
        ack_body="x",
        hard_guard_violations=[],
        guard_failed=False,
        judge_reasoning="ok",
        tokens_in=1,
        tokens_out=1,
        cost_cents=0.0,
        latency_s=0.0,
        failures=[],
    )
    assert check.ok is True


def test_expected_actions_for_emergency_includes_call_and_safety_sms() -> None:
    actions = expected_actions_for(Severity.EMERGENCY, has_refusal_flags=False)
    assert actions == ["call_landlord_now", "safety_sms_immediate"]


def test_expected_actions_for_non_emergency_is_draft_and_hold() -> None:
    assert expected_actions_for(Severity.URGENT, has_refusal_flags=False) == ["draft_and_hold"]
    assert expected_actions_for(Severity.ROUTINE, has_refusal_flags=False) == ["draft_and_hold"]


def test_expected_actions_for_refusal_appends_flag_for_landlord() -> None:
    actions = expected_actions_for(Severity.ROUTINE, has_refusal_flags=True)
    assert actions == ["draft_and_hold", "flag_for_landlord"]


# ===========================================================================
# THE PAID GATE -- @pytest.mark.eval -- NEVER RUN THESE FROM AN AGENT.
#
# `uv run pytest -m eval --collect-only -q` must collect one test per
# scenario (20 total: 11 canonical incl. the prompt-injection addition + 9
# negative-prefilter) -- verified by an implementer/CI collection-only run,
# never an actual execution, per this task's explicit instructions. The
# orchestrator runs `uv run pytest -m eval` (or `python -m evals.runner`)
# separately, deliberately, understanding it costs money.
# ===========================================================================


@pytest.mark.eval
@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.id for s in ALL_SCENARIOS])
async def test_eval_scenario(scenario: Scenario) -> None:
    """Real Anthropic API, real money. One test per scenario; a scenario's
    OWN category (E-class/F-class hard, U-class/R-class soft) and any
    Tier-0 prefilter miss determine whether a failure here is a release
    blocker -- see ``evals/scoring.py`` for the exact gate, and
    ``evals/runner.py``'s CLI (``python -m evals.runner``) for the tool
    that encodes that distinction in its own process exit code rather than
    pytest's blanket "any failing test fails the run" behavior.

    ``dry_run`` is computed from ``EVAL_DRY_RUN`` the SAME way ``main()``
    does (via ``tool_caller_factory``), so this test respects that env var
    too if it's ever set alongside ``-m eval`` -- real mode gets pacing +
    rate-limit backoff (``evals/runner.py``'s ``wrap_with_pacing_and_
    backoff``); an INFRA failure (rate-limit backoff exhausted, or any
    other call/parse failure) is reported distinctly from a semantic miss
    -- see the ``result.errored`` branch below.
    """
    dry_run = os.environ.get("EVAL_DRY_RUN") == "1"
    tool_caller = tool_caller_factory(dry_run=dry_run)(scenario)
    result = await run_scenario(scenario, tool_caller=tool_caller, dry_run=dry_run)
    print(  # noqa: T201 -- per-scenario cost/latency report, issue #35 AC
        f"{scenario.id}: passed={result.passed} errored={result.errored} "
        f"cost_cents={result.total_cost_cents:.4f} latency_s={result.total_latency_s:.2f}"
    )
    if result.errored:
        pytest.fail(
            f"INCONCLUSIVE -- call infrastructure failed, re-run this scenario "
            f"(NOT a semantic/classification miss): {result.infra_error}"
        )
    assert result.prefilter_ok is not False, result.prefilter_result
    assert result.classification_ok is not False, [
        (s.sample_index, s.failures) for s in result.classification_samples
    ]
    draft_failures = result.draft_check.failures if result.draft_check else None
    assert result.draft_ok is not False, draft_failures
    assert not result.top_level_failures
