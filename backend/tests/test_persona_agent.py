"""Pure-helper tests for persona_agent — no network, no Flask app."""
import pytest

from persona_agent import (
    format_session_evidence, _format_context, _answer_quality_hint,
    _build_llm_prompt, _validate, PersonaGenerationError,
)
from llm_schemas import PERSONA_DIMENSIONS


# --- format_session_evidence ---

def test_no_evidence_when_no_sessions():
    assert format_session_evidence({"quizzes": [], "speaking": []}) is None


def test_evidence_includes_session_count():
    report = {"quizzes": [{"topic": "SQL"}], "speaking": [{}], "speaking_averages": None, "recurring_weak_areas": []}
    evidence = format_session_evidence(report)
    assert "2 COMPLETED PRACTICE SESSION" in evidence


def test_evidence_includes_recurring_weak_areas():
    report = {
        "quizzes": [{}], "speaking": [],
        "recurring_weak_areas": [{"area": "communication", "count": 3}],
        "speaking_averages": None,
    }
    evidence = format_session_evidence(report)
    assert "communication (seen 3x)" in evidence


def test_evidence_includes_speaking_averages():
    report = {
        "quizzes": [], "speaking": [{}],
        "speaking_averages": {"fluency": 7.5, "clarity": None},
        "recurring_weak_areas": [],
    }
    evidence = format_session_evidence(report)
    assert "fluency: 7.5/10" in evidence
    assert "clarity" not in evidence  # None scores are omitted, not printed as "None/10"


# --- _format_context ---

def test_format_context_handles_missing_onboarding_and_resume():
    text = _format_context(None, None)
    assert "(none provided)" in text
    assert "(no resume on file)" in text


def test_format_context_includes_provided_details():
    text = _format_context({"target_role": "backend engineer"}, {"skills": ["Python"], "experience_level": "fresher"})
    assert "target role: backend engineer" in text
    assert "Python" in text
    assert "fresher" in text


# --- _answer_quality_hint ---

def test_choice_questions_get_no_hint():
    assert _answer_quality_hint({"type": "situational"}) is None
    assert _answer_quality_hint({"type": "tradeoff"}) is None


def test_blank_free_text_flagged_as_avoidance():
    hint = _answer_quality_hint({"type": "recall", "answer": ""})
    assert "avoidance" in hint


def test_short_free_text_flagged_as_weak():
    hint = _answer_quality_hint({"type": "recall", "answer": "not much to say"})
    assert "weak evidence" in hint


def test_substantial_free_text_gets_no_hint():
    long_answer = " ".join(["word"] * 25)
    assert _answer_quality_hint({"type": "recall", "answer": long_answer}) is None


# --- _validate ---

def _valid_persona_data():
    return {
        "summary": "You showed strong initiative and clear communication throughout.",
        "dimensions": {d: {"level": "Moderate", "note": f"note for {d}"} for d in PERSONA_DIMENSIONS},
    }


def test_validate_accepts_full_valid_data():
    result = _validate(_valid_persona_data())
    assert result["summary"]
    assert len(result["dimensions"]) == len(PERSONA_DIMENSIONS)


def test_validate_failure_log_has_no_persona_content(capsys):
    """str(ValidationError) renders the offending input value — here the model's
    summary and per-dimension notes, i.e. a written assessment of a named person
    derived from their answers and resume. Logs get the shape, not the content."""
    bad = _valid_persona_data()
    bad["summary"] = "You struggled to explain your own SmartAttend OpenCV pipeline."
    bad["dimensions"][PERSONA_DIMENSIONS[0]]["level"] = "High"

    with pytest.raises(PersonaGenerationError):
        _validate(bad)

    log = capsys.readouterr().out
    assert "SmartAttend" not in log and "OpenCV" not in log
    assert "note for" not in log
    # The failure is still diagnosable: which field, and why.
    assert PERSONA_DIMENSIONS[0] in log and "literal_error" in log


def test_validate_rejects_missing_dimension():
    bad = _valid_persona_data()
    del bad["dimensions"][PERSONA_DIMENSIONS[0]]
    with pytest.raises(PersonaGenerationError):
        _validate(bad)


def test_validate_rejects_invalid_level_value():
    bad = _valid_persona_data()
    bad["dimensions"][PERSONA_DIMENSIONS[0]]["level"] = "High"
    with pytest.raises(PersonaGenerationError):
        _validate(bad)


def test_validate_rejects_blank_summary():
    bad = _valid_persona_data()
    bad["summary"] = "   "
    with pytest.raises(PersonaGenerationError):
        _validate(bad)


def test_validate_rejects_non_dict_dimensions():
    bad = _valid_persona_data()
    bad["dimensions"] = ["a", "b"]
    with pytest.raises(PersonaGenerationError):
        _validate(bad)


def test_validate_defaults_missing_note_to_empty_string():
    data = _valid_persona_data()
    del data["dimensions"][PERSONA_DIMENSIONS[0]]["note"]
    result = _validate(data)
    assert result["dimensions"][PERSONA_DIMENSIONS[0]]["note"] == ""


# --- how the verdict is delivered on a first build ---
# A real user's first-ever profile opened by telling them their answers "lacked
# detail"; they abandoned the next quiz and left. The scoring rubric is right and
# stays — only the summary's shape changes for someone who signed up minutes ago.

def test_first_build_asks_for_a_starting_point_not_a_verdict():
    prompt = _build_llm_prompt([], first_build=True)
    assert "STARTING POINT, not a verdict" in prompt
    assert "Open with what they genuinely did well" in prompt
    assert "Do not list every weakness" in prompt


def test_refresh_keeps_the_blunt_read():
    prompt = _build_llm_prompt([], first_build=False)
    assert "earned the blunt read" in prompt
    assert "STARTING POINT, not a verdict" not in prompt


def test_the_scoring_rubric_is_identical_either_way():
    """Softening delivery must not soften the levels — the report card depends on
    them being honest."""
    first = _build_llm_prompt([], first_build=True)
    refresh = _build_llm_prompt([], first_build=False)
    for rule in ("BE STRICT AND HONEST",
                 "never reward effortless or generic answers",
                 "if everything comes out Strong you are not judging hard enough",
                 "A weak, empty, or evasive answer MUST score Developing"):
        assert rule in first and rule in refresh


def test_first_build_still_forbids_inventing_strengths():
    assert "never invent a strength they did not show" in _build_llm_prompt([], first_build=True)


def test_first_build_is_the_default_for_direct_callers():
    assert "STARTING POINT, not a verdict" in _build_llm_prompt([])
