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


# ---------------------------------------------------------------------------
# REGRESSION: BLOCKING #1 — guard over-suppression (anchor-token suppression)
# ---------------------------------------------------------------------------


class TestRegressionBlocking1GuardOverSuppression:
    """Guard over-suppression was the worst defect: the smoke-detector battery
    guard was swallowing INDEPENDENT fire triggers because the proximity trigger
    anchored on the FIRST 'smoke' token (inside the guard phrase) whose full
    match span overlapped the guard span.  The fix: suppression is keyed on
    the ANCHOR TOKEN span, not the full match.  A guard's .{0,80} tail no
    longer extends its suppression range over downstream tokens.

    INVARIANT: if any keyword occurrence satisfies a trigger and that anchor
    token is NOT inside any guard core phrase, hard_hit MUST be True.
    """

    @pytest.mark.unit
    def test_smoke_filling_kitchen_with_detector_mention(self) -> None:
        """Second 'smoke' (filling the kitchen) is independent of detector mention."""
        result = check("smoke detector chirping but smoke is filling the kitchen")
        assert result.hard_hit is True, f"Expected hard_hit=True: {result}"
        assert "fire" in result.categories
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_smoke_detector_beeping_smoke_everywhere(self) -> None:
        """'smoke everywhere' is a standalone trigger; detector/beeping mention guarded."""
        result = check("smoke detector beeping smoke everywhere")
        assert result.hard_hit is True, f"Expected hard_hit=True: {result}"
        assert "fire" in result.categories
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_smoke_detector_low_battery_smoke_filling_unit(self) -> None:
        """Low battery mention + real smoke filling unit — must still fire."""
        result = check("the smoke detector low battery there is smoke filling the unit now help")
        assert result.hard_hit is True, f"Expected hard_hit=True: {result}"
        assert "fire" in result.categories
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_smoke_detector_and_fire_alarm_chirping(self) -> None:
        """Bare 'fire' in 'fire alarm' must NOT be swallowed by the .{0,80} guard window.

        The guard core span is 'smoke (detector|alarm)', not 'smoke ... chirping'.
        'fire' at position ~27 is outside the core span and must fire.
        """
        result = check("the smoke detector and the fire alarm keep chirping")
        assert result.hard_hit is True, f"Expected hard_hit=True: {result}"
        assert "fire" in result.categories
        assert "smoke_detector_battery" in result.guards


# ---------------------------------------------------------------------------
# REGRESSION: BLOCKING #2 — breathing-distress family
# ---------------------------------------------------------------------------


class TestRegressionBlocking2BreathingDistress:
    """The old pattern required 'can t breathe' (with 'e'), missing 'breath'
    (no trailing 'e') and missing 'cannot'/'can not' forms."""

    @pytest.mark.unit
    def test_i_cant_breath(self) -> None:
        """'cant breath' (no 'e') — common informal spelling."""
        _hard("i cant breath", "person")

    @pytest.mark.unit
    def test_cant_breath_short(self) -> None:
        """Shortest form — still must fire."""
        _hard("cant breath", "person")

    @pytest.mark.unit
    def test_cannot_breathe(self) -> None:
        """'cannot breathe' — formal spelling."""
        _hard("cannot breathe", "person")

    @pytest.mark.unit
    def test_she_can_not_breathe(self) -> None:
        """'can not breathe' with space — also must fire."""
        _hard("she can not breathe", "person")

    @pytest.mark.unit
    def test_having_trouble_breathing(self) -> None:
        """Distress proximity form — 'trouble' near 'breathing'."""
        _hard("having trouble breathing", "person")

    @pytest.mark.unit
    def test_struggling_to_breathe(self) -> None:
        """Distress proximity form — 'struggling' near 'breathe'."""
        _hard("struggling to breathe", "person")


# ---------------------------------------------------------------------------
# REGRESSION: BLOCKING #3 — pipe burst (reverse word order)
# ---------------------------------------------------------------------------


class TestRegressionBlocking3PipeBurst:
    """The old pattern only had 'burst pipe'; 'pipe burst' is equally common."""

    @pytest.mark.unit
    def test_pipe_burst_bare(self) -> None:
        """'pipe burst' — most common tenant shorthand."""
        _hard("pipe burst", "water")

    @pytest.mark.unit
    def test_the_pipe_just_burst(self) -> None:
        _hard("the pipe just burst", "water")

    @pytest.mark.unit
    def test_a_pipe_has_burst(self) -> None:
        _hard("a pipe has burst", "water")


# ---------------------------------------------------------------------------
# REGRESSION: BLOCKING #4 — elevator entrapment phrasings
# ---------------------------------------------------------------------------


class TestRegressionBlocking4ElevatorEntrapment:
    """The old patterns required 'elevator stuck/trapped' or 'trapped in elevator';
    'stuck in the elevator' and 'elevator is stuck' were missed."""

    @pytest.mark.unit
    def test_stuck_in_the_elevator(self) -> None:
        _hard("stuck in the elevator", "person")

    @pytest.mark.unit
    def test_im_stuck_in_an_elevator(self) -> None:
        _hard("im stuck in an elevator", "person")

    @pytest.mark.unit
    def test_the_elevator_is_stuck(self) -> None:
        """'elevator is stuck' — gap between 'elevator' and 'stuck'."""
        _hard("the elevator is stuck", "person")


# ---------------------------------------------------------------------------
# REGRESSION: BLOCKING #5 — broke into / breaking into
# ---------------------------------------------------------------------------


class TestRegressionBlocking5BrokeInto:
    """The old patterns ended at 'in' ('\bbreaking in\b'); '\bin\b' word
    boundary prevented matching 'into' because '\b' sits before the 'to'."""

    @pytest.mark.unit
    def test_broke_into_apartment(self) -> None:
        _hard("someone broke into the apartment", "security")

    @pytest.mark.unit
    def test_broke_into_my_place(self) -> None:
        _hard("they broke into my place", "security")

    @pytest.mark.unit
    def test_breaking_into_apartment(self) -> None:
        _hard("someone is breaking into my apartment", "security")

    @pytest.mark.unit
    def test_breaking_into_unit(self) -> None:
        _hard("hes breaking into the unit", "security")


# ---------------------------------------------------------------------------
# REGRESSION: Recommended additions (safety-reviewer)
# ---------------------------------------------------------------------------


class TestRegressionRecommendedAdditions:
    """Additional patterns recommended by safety review per bias rule."""

    @pytest.mark.unit
    def test_gas_is_leaking(self) -> None:
        """'gas is leaking' — verb form not covered by old gas + leak proximity."""
        _hard("gas is leaking", "gas_co")

    @pytest.mark.unit
    def test_gas_leaking_from_stove(self) -> None:
        _hard("gas leaking from the stove", "gas_co")

    @pytest.mark.unit
    def test_smoke_alarm_blaring(self) -> None:
        """Continuous alarm — rubric: a CONTINUOUS alarm is EMERGENCY."""
        _hard("the smoke alarm is blaring", "fire")

    @pytest.mark.unit
    def test_smoke_alarm_wont_stop(self) -> None:
        _hard("smoke alarm wont stop", "fire")

    @pytest.mark.unit
    def test_nonstop_smoke_alarm(self) -> None:
        """'nonstop' before 'smoke alarm' — reverse-order form."""
        _hard("nonstop smoke alarm going off", "fire")

    @pytest.mark.unit
    def test_continuous_smoke_alarm(self) -> None:
        """'continuous' before 'smoke alarm' — reverse-order form."""
        _hard("continuous smoke alarm", "fire")

    @pytest.mark.unit
    def test_water_dripping_onto_outlet(self) -> None:
        """Water + electrical contact = EMERGENCY per rubric."""
        _hard("water is dripping onto the outlet", "water")

    @pytest.mark.unit
    def test_water_near_breaker_panel(self) -> None:
        _hard("water near the breaker panel", "water")

    @pytest.mark.unit
    def test_water_near_wiring(self) -> None:
        _hard("water touching the wiring in the wall", "water")

    @pytest.mark.unit
    def test_overdosed(self) -> None:
        """'overdosed' (past tense) — must match 'overdose(d)?'."""
        _hard("she overdosed on something", "person")

    @pytest.mark.unit
    def test_overdose(self) -> None:
        """Bare 'overdose' (present form)."""
        _hard("tenant may have had an overdose", "person")

    @pytest.mark.unit
    def test_collapsed(self) -> None:
        """'collapsed' — strong medical emergency signal."""
        _hard("he collapsed in the hallway", "person")


# ---------------------------------------------------------------------------
# REGRESSION: Must NOT regress — routine guard cases (confirmed hard_hit=False)
# ---------------------------------------------------------------------------


class TestRegressionMustNotRegress:
    """These four cases were called out explicitly as must-not-fire.
    Run every time to detect any broadening that accidentally breaks guards."""

    @pytest.mark.unit
    def test_smoke_detector_battery_chirping_still_guarded(self) -> None:
        result = _not_hard("smoke detector battery chirping")
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_smoke_alarm_keeps_beeping_low_battery_still_guarded(self) -> None:
        result = _not_hard("the smoke alarm keeps beeping low battery")
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_fire_drill_at_2pm_today_still_guarded(self) -> None:
        result = _not_hard("fire drill at 2pm today")
        assert "fire_drill" in result.guards

    @pytest.mark.unit
    def test_annual_fire_alarm_test_tomorrow_still_guarded(self) -> None:
        result = _not_hard("annual fire alarm test tomorrow")
        assert "fire_alarm_test" in result.guards

    @pytest.mark.unit
    def test_continuous_alarm_guard_does_not_suppress_blaring(self) -> None:
        """Blaring/nonstop/continuous smoke alarm is EMERGENCY, NOT guarded.
        Verify the battery guard does NOT suppress 'blaring' smoke alarm."""
        result = check("the smoke alarm is blaring")
        assert result.hard_hit is True
        assert "fire" in result.categories
        # battery guard must NOT be present (no battery/chirp mention)
        assert "smoke_detector_battery" not in result.guards


# ---------------------------------------------------------------------------
# REGRESSION: Invariant — independent trigger + guard phrase co-location
# ---------------------------------------------------------------------------


class TestAnchorTokenInvariant:
    """Explicit invariant: for every curated 'guard phrase + independent
    trigger keyword' message, hard_hit MUST be True.

    These verify the anchor-token suppression algorithm: a guard may only
    suppress anchor tokens that fall inside its CORE phrase span.
    """

    @pytest.mark.unit
    def test_invariant_smoke_detector_then_smoke_filling(self) -> None:
        """Guard phrase first; independent smoke trigger later."""
        result = check("smoke detector chirping but smoke is filling the kitchen")
        assert result.hard_hit is True
        assert "fire" in result.categories
        assert "smoke_detector_battery" in result.guards

    @pytest.mark.unit
    def test_invariant_smoke_everywhere_after_battery_mention(self) -> None:
        result = check("smoke detector beeping smoke everywhere")
        assert result.hard_hit is True
        assert "fire" in result.categories

    @pytest.mark.unit
    def test_invariant_fire_outside_guard_window(self) -> None:
        """'fire' in 'fire alarm keep chirping' is outside the guard core span."""
        result = check("the smoke detector and the fire alarm keep chirping")
        assert result.hard_hit is True
        assert "fire" in result.categories

    @pytest.mark.unit
    def test_invariant_fire_drill_with_real_fire(self) -> None:
        """'fire drill' guard does not suppress later 'real fire'."""
        result = check("fire drill earlier but real fire in stairwell")
        assert result.hard_hit is True
        assert "fire" in result.categories
        assert "fire_drill" in result.guards

    @pytest.mark.unit
    def test_invariant_fire_alarm_test_with_real_fire(self) -> None:
        """'fire alarm test' guard does not suppress 'fire in 4B'."""
        result = check("fire alarm test today but fire in 4B")
        assert result.hard_hit is True
        assert "fire" in result.categories
        assert "fire_alarm_test" in result.guards

    @pytest.mark.unit
    def test_invariant_guard_recorded_when_trigger_wins(self) -> None:
        """When trigger beats guard, guard is still recorded in guards field."""
        result = check("smoke detector is beeping battery but smoke is everywhere")
        assert result.hard_hit is True
        assert "smoke_detector_battery" in result.guards
