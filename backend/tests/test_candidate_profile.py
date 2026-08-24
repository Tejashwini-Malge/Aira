"""Pure-mapping tests for candidate_profile — no network, no LLM.

Covers the defects behind "the first questions are pitched at someone I'm not":
a student was handed manager-level scenarios (product owners, budgets, direct
reports) because nothing set a seniority ceiling; the stated onboarding goal was
carried into the prompt as inert context with no rule telling the model to act on
it; and the seniority itself came from an LLM guess that production data showed
to be unreliable.

The ladder is two bands — student and fresher. An `experienced` band and a
transition axis were removed on 2026-08-07 for want of any users.
"""
import pytest

from candidate_profile import (
    BANDS,
    DEFAULT_BAND,
    GOAL_RULES,
    STAKES_RULES,
    CandidateProfile,
    _PROFILE_BY_DECLARATION,
    resolve_profile,
)
from onboarding_schema import CHOICE_FIELDS


# --- the declaration is authoritative ---

@pytest.mark.parametrize("declared,band", [
    ("Still studying", "student"),
    ("Graduated, looking for my first role", "fresher"),
])
def test_declaration_maps_to_a_band(declared, band):
    profile = resolve_profile({"experience": declared}, {})
    assert profile.band == band
    assert profile.source == "declared"


def test_every_onboarding_experience_option_resolves():
    """A dropdown value with no mapping would silently fall through to the
    resume inference — the exact behaviour this question replaced."""
    assert set(CHOICE_FIELDS["experience"]) == set(_PROFILE_BY_DECLARATION)


def test_the_ladder_is_only_the_two_bands_we_have_users_for():
    assert BANDS == ("student", "fresher")
    assert set(_PROFILE_BY_DECLARATION.values()) <= set(BANDS)


def test_declaration_overrides_a_disagreeing_resume():
    """The observed production case: a BTech student whose free-text note called
    them a 'data science professional' was inferred `experienced`."""
    profile = resolve_profile(
        {"experience": "Still studying"}, {"experience_level": "experienced"}
    )
    assert profile.band == "student"
    assert profile.source == "declared"


def test_declaration_is_trusted_upward_too():
    """Not just a safety clamp — someone who says they have graduated is not
    quietly demoted because their resume parsed thin."""
    profile = resolve_profile(
        {"experience": "Graduated, looking for my first role"},
        {"experience_level": "student"},
    )
    assert profile.band == "fresher"


def test_an_answer_from_a_retired_option_falls_through_to_inference():
    """Two test accounts were saved before the experienced options were removed.
    A stored value with no mapping must degrade, not crash."""
    profile = resolve_profile(
        {"experience": "Experienced, switching to a different field"},
        {"experience_level": "student"},
    )
    assert profile.band == "student"
    assert profile.source == "inferred"


# --- resume inference survives as fallback only ---

@pytest.mark.parametrize("level,expected", [
    ("student", "student"),
    ("fresher", "fresher"),
    # Both collapse to the top of the remaining ladder: a fresher question is
    # still answerable by a student, so this is the safer direction than
    # demoting someone whose resume does show real work.
    ("intern", "fresher"),
    ("experienced", "fresher"),
])
def test_resume_inference_still_resolves_for_pre_existing_personas(level, expected):
    profile = resolve_profile({}, {"experience_level": level})
    assert profile.band == expected
    assert profile.source == "inferred"


@pytest.mark.parametrize("level", ["  Fresher  ", "STUDENT"])
def test_inference_is_case_and_whitespace_tolerant(level):
    assert resolve_profile({}, {"experience_level": level}).band == level.strip().lower()


@pytest.mark.parametrize("resume_data", [
    None,
    {},
    {"experience_level": None},
    {"experience_level": ""},
    {"skills": ["Python"]},                       # row predates the field
    {"experience_level": "principal architect"},  # value outside the vocabulary
    # Observed in production: the model echoed the schema's own option list.
    {"experience_level": "student|fresher"},
])
def test_unusable_inference_falls_back_to_the_safest_band(resume_data):
    assert resolve_profile({}, resume_data).band == DEFAULT_BAND


@pytest.mark.parametrize("onboarding", [
    None, {}, {"experience": ""}, {"experience": None},
    {"experience": "Chief Vibes Officer"},
])
def test_absent_or_unknown_declaration_falls_through_to_inference(onboarding):
    profile = resolve_profile(onboarding, {"experience_level": "fresher"})
    assert profile.band == "fresher"
    assert profile.source == "inferred"


# --- the band constrains where the scenario sits ---

def test_student_rule_bans_the_props_they_have_never_had():
    """The exact props from the questions real students dropped out on."""
    rule = resolve_profile({"experience": "Still studying"}, {}).band_rule
    for banned in ("product owner", "stakeholders", "budget", "reporting to them"):
        assert banned in rule
    for plausible in ("group project", "hackathon", "club"):
        assert plausible in rule


def test_fresher_rule_allows_workplace_footing_but_not_ownership():
    rule = resolve_profile({"experience": "Graduated, looking for my first role"}, {}).band_rule
    assert "code review" in rule
    assert "no direct reports" in rule
    assert "no budget" in rule


@pytest.mark.parametrize("band", BANDS)
def test_every_band_has_a_rule(band):
    assert CandidateProfile(band=band).band_rule


def test_rules_are_never_empty_for_any_resolvable_input():
    for declared in list(CHOICE_FIELDS["experience"]) + ["", "junk"]:
        assert resolve_profile({"experience": declared}, {}).rules().strip()


# --- how hard the question bites, as distinct from where it is set ---

def test_the_fresher_rule_removes_the_obviously_right_answer():
    rule = resolve_profile({"experience": "Graduated, looking for my first role"}, {}).stakes_rule
    assert "recite the\n    textbook answer" in rule or "textbook answer" in rule
    assert "real tension" in rule
    assert "bounded to what someone at this stage would own" in rule


def test_the_student_rule_keeps_difficulty_in_the_trade_off_not_the_setup():
    rule = resolve_profile({"experience": "Still studying"}, {}).stakes_rule
    assert "easy to picture" in rule
    assert "should not need workplace experience to understand" in rule


@pytest.mark.parametrize("band", BANDS)
def test_every_band_has_a_distinct_stakes_rule(band):
    rules = {b: CandidateProfile(band=b).stakes_rule for b in BANDS}
    assert rules[band]
    assert len(set(rules.values())) == len(BANDS)


def test_stakes_never_outrank_the_scenario_ceiling():
    """A stakes rule that came first would talk a student back into workplace
    scenarios — the ceiling has to bind hardest."""
    profile = CandidateProfile(band="student")
    rules = profile.rules()
    assert rules.index(profile.band_rule) < rules.index(profile.stakes_rule)


def test_an_unknown_band_still_gets_both_rules():
    profile = CandidateProfile(band="nonsense")
    assert profile.stakes_rule == STAKES_RULES[DEFAULT_BAND]
    assert profile.band_rule


# --- goal steers the scenario domain ---

def test_communication_goal_steers_scenarios_toward_communication():
    """The akash case: goal was 'Improving communication', questions were about
    leading a team of 5."""
    profile = resolve_profile({"goal": "Improving communication"}, {})
    assert "COMMUNICATE" in profile.goal_rule
    assert "explaining" in profile.goal_rule


def test_every_onboarding_goal_option_maps_to_a_rule():
    assert set(CHOICE_FIELDS["goal"]) == set(GOAL_RULES)


@pytest.mark.parametrize("onboarding", [None, {}, {"goal": ""}, {"goal": None}])
def test_absent_goal_yields_no_goal_rule_but_still_frames_the_band(onboarding):
    profile = resolve_profile(onboarding, {"experience_level": "student"})
    assert profile.goal_rule == ""
    assert profile.band_rule in profile.rules()


def test_unknown_goal_does_not_raise_or_invent_a_rule():
    profile = resolve_profile({"goal": "Something we have not shipped yet"}, {})
    assert profile.goal_rule == ""
    assert profile.rules().strip()


def test_band_rule_precedes_goal_rule():
    profile = CandidateProfile(band="student", goal="Improving communication")
    rules = profile.rules()
    assert rules.index(profile.band_rule) < rules.index(profile.goal_rule)


def test_a_resolved_profile_cannot_be_mutated_by_a_caller():
    """Frozen so one generator cannot alter another's view of the same user."""
    profile = resolve_profile({"experience": "Still studying"}, {})
    with pytest.raises(Exception):
        profile.band = "fresher"
