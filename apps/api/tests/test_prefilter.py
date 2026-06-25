"""Exhaustive unit tests for app/agent/prefilter.py (Tier-0 emergency filter).

Markers:  ``@pytest.mark.unit`` — all tests here are pure, no I/O.

Coverage targets:
- Every HARD pattern in every category fires (hard_hit=True, correct category).
- Every guard suppresses its false positive (hard_hit=False).
- Trigger-beats-guard conflict: guard recorded, hard_hit=True.
- SOFT-only messages: hard_hit=False, soft_annotations populated.
- Plain routine messages: all fields empty/False.
- Mixed HARD + SOFT: both recorded correctly.
- Normalization: punctuation/caps variations still fire.
- Determinism: pure function, same output for same input.
"""

from __future__ import annotations

import pytest

from app.agent.prefilter import PREFILTER_VERSION, check
from app.agent.schemas import PrefilterResult  # noqa: F401  (used in type annotation)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hard(text: str, *expected_categories: str) -> None:
    """Assert text fires a HARD hit containing all expected_categories."""
    result = check(text)
    assert result.hard_hit is True, f"Expected hard_hit=True for: {text!r}\n  got: {result}"
    for cat in expected_categories:
        assert cat in result.categories, (
            f"Expected category {cat!r} for: {text!r}\n  got categories: {result.categories}"
        )


def _not_hard(text: str) -> PrefilterResult:
    """Assert text does NOT fire a HARD hit and return the result."""
    result = check(text)
    assert result.hard_hit is False, f"Expected hard_hit=False for: {text!r}\n  got: {result}"
    assert result.categories == [], (
        f"Expected empty categories for: {text!r}\n  got: {result.categories}"
    )
    return result


def _soft(text: str, *expected_annotations: str) -> None:
    """Assert text has soft annotations but no HARD hit."""
    result = _not_hard(text)
    for ann in expected_annotations:
        assert ann in result.soft_annotations, (
            f"Expected soft annotation {ann!r} for: {text!r}\n  got: {result.soft_annotations}"
        )


# ---------------------------------------------------------------------------
# Version pin
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prefilter_version() -> None:
    """PREFILTER_VERSION must be '1.0' (pinned to rubric v1.0)."""
    assert PREFILTER_VERSION == "1.0"


# ---------------------------------------------------------------------------
# HARD triggers — fire category
# ---------------------------------------------------------------------------


class TestHardFire:
    """Every HARD pattern in the 'fire' category must fire."""

    @pytest.mark.unit
    def test_bare_fire_word(self) -> None:
        _hard("There is a fire on the second floor", "fire")

    @pytest.mark.unit
    def test_fire_exclamations(self) -> None:
        """Normalization: FIRE!!! should match."""
        _hard("FIRE!!!", "fire")

    @pytest.mark.unit
    def test_fire_caps(self) -> None:
        _hard("THERE IS A FIRE IN THE LOBBY", "fire")

    @pytest.mark.unit
    def test_fire_in_sentence(self) -> None:
        _hard("I think there's a fire in 4B", "fire")

    @pytest.mark.unit
    def test_fire_hallway(self) -> None:
        _hard("fire in the stairwell, help", "fire")

    @pytest.mark.unit
    def test_smoke_filling(self) -> None:
        _hard("smoke is filling the hallway", "fire")

    @pytest.mark.unit
    def test_smoke_everywhere(self) -> None:
        _hard("there is smoke everywhere", "fire")

    @pytest.mark.unit
    def test_smoke_smell(self) -> None:
        _hard("I smell smoke in the apartment", "fire")

    @pytest.mark.unit
    def test_smoke_smell_reversed(self) -> None:
        """smoke + smell in reversed order still fires."""
        _hard("there's a smell of smoke coming from upstairs", "fire")

    @pytest.mark.unit
    def test_burning_smell(self) -> None:
        _hard("there is a burning smell in the kitchen", "fire")

    @pytest.mark.unit
    def test_smell_of_burning(self) -> None:
        _hard("I notice a smell of burning near the outlet", "fire")

    @pytest.mark.unit
    def test_burning_smell_caps_punct(self) -> None:
        _hard("Burning Smell!!! Coming from the walls", "fire")


# ---------------------------------------------------------------------------
# HARD triggers — gas_co category
# ---------------------------------------------------------------------------


class TestHardGasCo:
    @pytest.mark.unit
    def test_gas_smell(self) -> None:
        _hard("I can smell gas in the kitchen", "gas_co")

    @pytest.mark.unit
    def test_gas_leak(self) -> None:
        _hard("there is a gas leak in my unit", "gas_co")

    @pytest.mark.unit
    def test_gas_smells_like(self) -> None:
        _hard("it smells like gas near the stove", "gas_co")

    @pytest.mark.unit
    def test_smell_of_gas(self) -> None:
        _hard("I smell gas coming from the basement", "gas_co")

    @pytest.mark.unit
    def test_leak_of_gas_reversed(self) -> None:
        _hard("there's a leak, smells like gas", "gas_co")

    @pytest.mark.unit
    def test_carbon_monoxide(self) -> None:
        _hard("carbon monoxide detector is going off", "gas_co")

    @pytest.mark.unit
    def test_carbon_monoxide_spelled(self) -> None:
        _hard("I think there is carbon monoxide in the apartment", "gas_co")

    @pytest.mark.unit
    def test_co_alarm(self) -> None:
        _hard("the co alarm is going off", "gas_co")

    @pytest.mark.unit
    def test_co_detector_going_off(self) -> None:
        _hard("co detector going off right now", "gas_co")

    @pytest.mark.unit
    def test_co_detector_sounding(self) -> None:
        _hard("the co detector is sounding", "gas_co")

    @pytest.mark.unit
    def test_alarm_going_off(self) -> None:
        """Generic 'alarm going off' fires — bias rule: alarm sounding = emergency."""
        _hard("alarm going off in the hallway", "gas_co")

    @pytest.mark.unit
    def test_gas_caps_punct(self) -> None:
        _hard("GAS LEAK!!!!", "gas_co")


# ---------------------------------------------------------------------------
# HARD triggers — water category
# ---------------------------------------------------------------------------


class TestHardWater:
    @pytest.mark.unit
    def test_flood(self) -> None:
        _hard("there is a flood in the basement", "water")

    @pytest.mark.unit
    def test_flooding(self) -> None:
        _hard("the bathroom is flooding", "water")

    @pytest.mark.unit
    def test_burst_pipe(self) -> None:
        _hard("a burst pipe in the kitchen", "water")

    @pytest.mark.unit
    def test_burst_pipe_hyphen(self) -> None:
        """Normalization: Burst-Pipe should still match."""
        _hard("there's a Burst-Pipe under the sink", "water")

    @pytest.mark.unit
    def test_water_pouring(self) -> None:
        _hard("water is pouring through the ceiling", "water")

    @pytest.mark.unit
    def test_water_gushing(self) -> None:
        _hard("water is gushing out from the wall", "water")

    @pytest.mark.unit
    def test_water_coming_through(self) -> None:
        _hard("water coming through the wall", "water")

    @pytest.mark.unit
    def test_water_through_ceiling(self) -> None:
        _hard("water through the ceiling in my bedroom", "water")

    @pytest.mark.unit
    def test_sewage(self) -> None:
        _hard("sewage is backing up into the bathtub", "water")

    @pytest.mark.unit
    def test_sewage_backup(self) -> None:
        _hard("sewage backup in the basement bathroom", "water")

    @pytest.mark.unit
    def test_flood_caps_punct(self) -> None:
        _hard("FLOODING in unit 3!!!", "water")


# ---------------------------------------------------------------------------
# HARD triggers — security category
# ---------------------------------------------------------------------------


class TestHardSecurity:
    @pytest.mark.unit
    def test_break_in(self) -> None:
        _hard("there was a break in tonight", "security")

    @pytest.mark.unit
    def test_breaking_in(self) -> None:
        _hard("someone is breaking in right now", "security")

    @pytest.mark.unit
    def test_broke_in(self) -> None:
        _hard("someone broke in while I was at work", "security")

    @pytest.mark.unit
    def test_intruder(self) -> None:
        _hard("there is an intruder in the building", "security")

    @pytest.mark.unit
    def test_someone_trying_to_get_in(self) -> None:
        _hard("someone is trying to get in through the window", "security")

    @pytest.mark.unit
    def test_break_in_caps(self) -> None:
        _hard("BREAK IN AT 2AM", "security")


# ---------------------------------------------------------------------------
# HARD triggers — person category
# ---------------------------------------------------------------------------


class TestHardPerson:
    @pytest.mark.unit
    def test_911(self) -> None:
        _hard("calling 911 right now", "person")

    @pytest.mark.unit
    def test_ambulance(self) -> None:
        _hard("we need an ambulance in unit 5", "person")

    @pytest.mark.unit
    def test_cant_breathe(self) -> None:
        _hard("my tenant can't breathe", "person")

    @pytest.mark.unit
    def test_cant_breathe_no_apostrophe(self) -> None:
        """Normalization strips apostrophe — 'cant breathe' must still match."""
        _hard("I cant breathe please help", "person")

    @pytest.mark.unit
    def test_unconscious(self) -> None:
        _hard("tenant is unconscious on the floor", "person")

    @pytest.mark.unit
    def test_heart_attack(self) -> None:
        """Bias rule: 'heart attack' clearly implied by rubric 'medical emergency'."""
        _hard("he's having a heart attack", "person")

    @pytest.mark.unit
    def test_seizure(self) -> None:
        """Bias rule: 'seizure' clearly implied by rubric 'medical emergency'."""
        _hard("tenant is having a seizure", "person")

    @pytest.mark.unit
    def test_not_breathing(self) -> None:
        _hard("she is not breathing", "person")

    @pytest.mark.unit
    def test_elevator_trapped(self) -> None:
        """Elevator entrapment is EMERGENCY per rubric."""
        _hard("I am trapped in the elevator", "person")

    @pytest.mark.unit
    def test_elevator_entrapment(self) -> None:
        _hard("elevator entrapment on floor 6", "person")

    @pytest.mark.unit
    def test_911_caps(self) -> None:
        _hard("CALL 911 NOW", "person")


# ---------------------------------------------------------------------------
# Guards — suppress false positives
# ---------------------------------------------------------------------------


class TestGuards:
    @pytest.mark.unit
    def test_smoke_detector_battery_chirping(self) -> None:
        """Rubric ROUTINE: smoke detector battery chirp must NOT fire."""
        result = _not_hard("smoke detector battery chirping")
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_smoke_alarm_keeps_beeping_low_battery(self) -> None:
        """Classic tenant text — must be guarded as ROUTINE."""
        result = _not_hard("the smoke alarm keeps beeping low battery")
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_smoke_alarm_beeping(self) -> None:
        result = _not_hard("smoke alarm is beeping, I think the battery is dead")
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_smoke_detector_chirping(self) -> None:
        result = _not_hard("smoke detector is chirping")
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_smoke_detector_low_battery(self) -> None:
        result = _not_hard("smoke detector says low battery")
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_battery_chirp_before_smoke_detector(self) -> None:
        """Guard also fires when battery/chirp precedes 'smoke detector'."""
        result = _not_hard("chirping sound from the smoke alarm — battery?")
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_fire_drill_at_2pm(self) -> None:
        """'fire drill at 2pm today' must NOT fire."""
        result = _not_hard("fire drill at 2pm today")
        assert "fire_drill" in result.guards

    @pytest.mark.unit
    def test_fire_drill_scheduled(self) -> None:
        result = _not_hard("just a reminder there is a fire drill at 9am")
        assert "fire_drill" in result.guards

    @pytest.mark.unit
    def test_annual_fire_alarm_test(self) -> None:
        """Annual fire alarm test must NOT fire."""
        result = _not_hard("annual fire alarm test tomorrow morning")
        assert "fire_alarm_test" in result.guards

    @pytest.mark.unit
    def test_fire_alarm_testing(self) -> None:
        result = _not_hard("fire alarm testing scheduled for Tuesday")
        assert "fire_alarm_test" in result.guards

    @pytest.mark.unit
    def test_fire_alarm_test_notice(self) -> None:
        result = _not_hard("notice: fire alarm test this afternoon")
        assert "fire_alarm_test" in result.guards


# ---------------------------------------------------------------------------
# Trigger beats guard — conflict cases
# ---------------------------------------------------------------------------


class TestTriggerBeatsGuard:
    """When an independent trigger also matches, hard_hit must be True.
    The matched guard is still recorded in guards."""

    @pytest.mark.unit
    def test_smoke_detector_chirping_but_smoke_filling_kitchen(self) -> None:
        """Guard is present but smoke filling the kitchen is an independent trigger."""
        text = "smoke detector is chirping but there is also smoke filling the kitchen"
        result = check(text)
        assert result.hard_hit is True, f"Expected hard_hit=True: {result}"
        assert "fire" in result.categories
        # Guard matched (the chirping smoke detector part) but was overridden.
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_fire_drill_earlier_but_real_fire_now(self) -> None:
        """Guard suppresses the 'fire drill' match; 'real fire in stairwell' is independent."""
        text = "we had a fire drill earlier but now there is an actual fire in the stairwell"
        result = check(text)
        assert result.hard_hit is True, f"Expected hard_hit=True: {result}"
        assert "fire" in result.categories
        assert "fire_drill" in result.guards

    @pytest.mark.unit
    def test_fire_alarm_test_but_fire_in_4b(self) -> None:
        """Fire alarm test guard fires but 'fire in 4B' is independent."""
        text = "fire alarm test today but there is a real fire in 4B"
        result = check(text)
        assert result.hard_hit is True, f"Expected hard_hit=True: {result}"
        assert "fire" in result.categories
        assert "fire_alarm_test" in result.guards

    @pytest.mark.unit
    def test_fire_drill_with_gas_leak(self) -> None:
        """Fire drill guard suppresses fire, but gas leak is an independent HARD trigger."""
        text = "fire drill at 10am but I smell gas in the hallway"
        result = check(text)
        assert result.hard_hit is True, f"Expected hard_hit=True: {result}"
        assert "gas_co" in result.categories
        assert "fire_drill" in result.guards

    @pytest.mark.unit
    def test_smoke_detector_beeping_but_smoke_everywhere(self) -> None:
        """Smoke detector battery guard fires; 'smoke everywhere' triggers independently."""
        text = "the smoke detector is beeping because of the battery but there is smoke everywhere"
        result = check(text)
        assert result.hard_hit is True, f"Expected hard_hit=True: {result}"
        assert "fire" in result.categories
        assert "smoke_detector_battery" in result.guards


# ---------------------------------------------------------------------------
# SOFT-only messages
# ---------------------------------------------------------------------------


class TestSoftOnly:
    @pytest.mark.unit
    def test_no_heat_since_last_night(self) -> None:
        _soft("no heat since last night", "no_heat")

    @pytest.mark.unit
    def test_freezing_in_apartment(self) -> None:
        _soft("it is freezing in the apartment", "freezing")

    @pytest.mark.unit
    def test_small_leak_under_sink(self) -> None:
        _soft("there is a small leak under the sink", "leak")

    @pytest.mark.unit
    def test_leak_from_faucet(self) -> None:
        _soft("slow leak from the bathroom faucet", "leak")

    @pytest.mark.unit
    def test_locked_out(self) -> None:
        _soft("I am locked out of my apartment", "locked_out")

    @pytest.mark.unit
    def test_locked_out_short(self) -> None:
        _soft("locked out", "locked_out")

    @pytest.mark.unit
    def test_sparks_from_outlet(self) -> None:
        _soft("I saw sparks from the outlet", "sparks")

    @pytest.mark.unit
    def test_sparking_outlet(self) -> None:
        _soft("the outlet is sparking", "sparks")

    @pytest.mark.unit
    def test_no_heat_categories_empty(self) -> None:
        """SOFT-only: categories must be empty."""
        result = check("no heat since yesterday")
        assert result.categories == []

    @pytest.mark.unit
    def test_heat_out(self) -> None:
        _soft("the heat is out in my unit", "no_heat")

    @pytest.mark.unit
    def test_heat_not_working(self) -> None:
        _soft("heat not working since Monday", "no_heat")


# ---------------------------------------------------------------------------
# Plain routine messages — nothing fires
# ---------------------------------------------------------------------------


class TestRoutine:
    @pytest.mark.unit
    def test_dripping_faucet(self) -> None:
        result = check("the kitchen faucet drips")
        assert result.hard_hit is False
        assert result.categories == []
        assert result.soft_annotations == []
        assert result.guards == []

    @pytest.mark.unit
    def test_rent_receipt(self) -> None:
        result = check("can I get a rent receipt for last month")
        assert result.hard_hit is False
        assert result.categories == []
        assert result.soft_annotations == []
        assert result.guards == []

    @pytest.mark.unit
    def test_noise_complaint(self) -> None:
        result = check("the neighbours are loud again tonight")
        assert result.hard_hit is False
        assert result.categories == []

    @pytest.mark.unit
    def test_paint_peeling(self) -> None:
        result = check("paint is peeling in the bedroom")
        assert result.hard_hit is False

    @pytest.mark.unit
    def test_guest_parking(self) -> None:
        result = check("can my guest park in spot 3 this weekend")
        assert result.hard_hit is False

    @pytest.mark.unit
    def test_empty_string(self) -> None:
        result = check("")
        assert result.hard_hit is False
        assert result.categories == []
        assert result.soft_annotations == []
        assert result.guards == []

    @pytest.mark.unit
    def test_campfire_stories(self) -> None:
        """'fire' inside 'campfire' must NOT match (word boundary)."""
        result = check("we were telling campfire stories last night")
        assert result.hard_hit is False

    @pytest.mark.unit
    def test_fireplace(self) -> None:
        """'fire' inside 'fireplace' must NOT match (word boundary)."""
        result = check("can you turn on the fireplace for winter")
        assert result.hard_hit is False

    @pytest.mark.unit
    def test_fired_from_job(self) -> None:
        """'fired' (past tense of fire in employment context) must NOT match
        — 'fired' is not 'fire' (word boundary prevents 'fired' matching \\bfire\\b)."""
        result = check("I just got fired from my job, struggling with rent")
        assert result.hard_hit is False


# ---------------------------------------------------------------------------
# Mixed HARD + SOFT in the same message
# ---------------------------------------------------------------------------


class TestMixed:
    @pytest.mark.unit
    def test_fire_and_no_heat(self) -> None:
        """A message with a HARD fire trigger and a SOFT no_heat annotation."""
        text = "there is a fire in the hallway and no heat in my unit"
        result = check(text)
        assert result.hard_hit is True
        assert "fire" in result.categories
        assert "no_heat" in result.soft_annotations

    @pytest.mark.unit
    def test_burst_pipe_and_locked_out(self) -> None:
        text = "burst pipe in the basement and I got locked out"
        result = check(text)
        assert result.hard_hit is True
        assert "water" in result.categories
        assert "locked_out" in result.soft_annotations

    @pytest.mark.unit
    def test_gas_leak_and_freezing(self) -> None:
        text = "smell gas in the kitchen, also freezing in here"
        result = check(text)
        assert result.hard_hit is True
        assert "gas_co" in result.categories
        assert "freezing" in result.soft_annotations

    @pytest.mark.unit
    def test_multiple_hard_categories(self) -> None:
        """Message triggering both fire and water categories."""
        text = "there is a fire and water pouring through the ceiling"
        result = check(text)
        assert result.hard_hit is True
        assert "fire" in result.categories
        assert "water" in result.categories


# ---------------------------------------------------------------------------
# Normalization edge cases
# ---------------------------------------------------------------------------


class TestNormalization:
    @pytest.mark.unit
    def test_all_caps_with_punctuation(self) -> None:
        """FIRE!!! normalizes to 'fire' and fires."""
        _hard("FIRE!!!", "fire")

    @pytest.mark.unit
    def test_hyphenated_burst_pipe(self) -> None:
        """Burst-Pipe — hyphen stripped to space — fires."""
        _hard("there's a Burst-Pipe under the sink", "water")

    @pytest.mark.unit
    def test_mixed_case_burst_pipe(self) -> None:
        _hard("BURST PIPE in the kitchen", "water")

    @pytest.mark.unit
    def test_punctuation_around_911(self) -> None:
        _hard("911!!!!", "person")

    @pytest.mark.unit
    def test_ellipsis_in_message(self) -> None:
        _hard("there's a fire... please help", "fire")

    @pytest.mark.unit
    def test_semicolon_separated(self) -> None:
        _hard("burst pipe; water everywhere", "water")

    @pytest.mark.unit
    def test_extra_whitespace(self) -> None:
        _hard("  there   is   a   fire   ", "fire")

    @pytest.mark.unit
    def test_newline_in_message(self) -> None:
        _hard("emergency\nfire in the building", "fire")


# ---------------------------------------------------------------------------
# Determinism — pure function
# ---------------------------------------------------------------------------


class TestDeterminism:
    @pytest.mark.unit
    def test_same_input_same_output_hard(self) -> None:
        text = "there is a fire on the second floor"
        assert check(text) == check(text)

    @pytest.mark.unit
    def test_same_input_same_output_soft(self) -> None:
        text = "no heat and freezing in here"
        assert check(text) == check(text)

    @pytest.mark.unit
    def test_same_input_same_output_routine(self) -> None:
        text = "the kitchen faucet drips"
        assert check(text) == check(text)

    @pytest.mark.unit
    def test_results_are_sorted(self) -> None:
        """categories, soft_annotations, guards must be sorted for stable output."""
        text = "there is a fire and water pouring through the ceiling, no heat"
        result = check(text)
        assert result.categories == sorted(result.categories)
        assert result.soft_annotations == sorted(result.soft_annotations)
        assert result.guards == sorted(result.guards)

    @pytest.mark.unit
    def test_no_global_state_mutation(self) -> None:
        """Calling check() multiple times must not mutate global state."""
        r1 = check("fire")
        r2 = check("the kitchen faucet drips")
        r3 = check("fire")
        assert r1 == r3
        assert r2.hard_hit is False


# ---------------------------------------------------------------------------
# PrefilterResult model conformance
# ---------------------------------------------------------------------------


class TestResultShape:
    @pytest.mark.unit
    def test_returns_prefilter_result_instance(self) -> None:
        result = check("some routine message")
        assert isinstance(result, PrefilterResult)

    @pytest.mark.unit
    def test_hard_hit_false_has_empty_categories(self) -> None:
        result = check("dripping faucet")
        assert result.hard_hit is False
        assert result.categories == []

    @pytest.mark.unit
    def test_hard_hit_true_has_nonempty_categories(self) -> None:
        result = check("there is a fire")
        assert result.hard_hit is True
        assert len(result.categories) > 0

    @pytest.mark.unit
    def test_guards_recorded_on_suppression(self) -> None:
        result = check("fire drill at 9am")
        assert "fire_drill" in result.guards
        assert result.hard_hit is False

    @pytest.mark.unit
    def test_guards_recorded_even_when_trigger_wins(self) -> None:
        """Guard is recorded in guards even when the trigger still wins."""
        text = "fire drill earlier but there is a real fire now"
        result = check(text)
        assert result.hard_hit is True
        assert "fire_drill" in result.guards


# ---------------------------------------------------------------------------
# Eval scenario alignment
# ---------------------------------------------------------------------------
# These are the scenarios from eval-scenarios-v1.md where prefilter_must_fire
# would be True (E1 and E2 per the emergency-prefilter.md spec).


class TestEvalScenarios:
    @pytest.mark.unit
    def test_e1_style_fire_scenario(self) -> None:
        """E-class fire: must trip Tier 0."""
        _hard("there is a fire on the third floor, smoke everywhere", "fire")

    @pytest.mark.unit
    def test_e2_style_gas_scenario(self) -> None:
        """E-class gas: must trip Tier 0."""
        _hard("I smell gas coming from the kitchen, please help", "gas_co")

    @pytest.mark.unit
    def test_r_class_chirp_must_not_fire(self) -> None:
        """R-class smoke-detector chirp: must NOT fire (rubric ROUTINE)."""
        result = _not_hard("smoke detector battery chirping")
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_r_class_chirp_variant(self) -> None:
        """Another R-class variant: continuous 'beeping' with battery mention."""
        result = _not_hard("the smoke alarm keeps beeping, I think it needs a new battery")
        assert "smoke_detector_battery" in result.guards
