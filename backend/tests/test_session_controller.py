"""Pure-helper tests for session_controller — no Flask app, no network."""
from types import SimpleNamespace

from session_controller import (
    _response_answered,
    _unanswered_ids,
    _assemble_questions,
    DIMENSION_ORDER,
    FIXED_COUNT,
    MIN_TEXT_CHARS,
)


# --- _response_answered / _unanswered_ids ---

def test_choice_question_answered_by_selected_label():
    assert _response_answered({"type": "situational", "selected_label": "A"})
    assert _response_answered({"type": "tradeoff", "selected_label": "B"})


def test_choice_question_without_selection_is_unanswered():
    assert not _response_answered({"type": "situational"})
    assert not _response_answered({"type": "tradeoff", "selected_label": ""})


def test_free_text_needs_real_substance():
    long_enough = "x" * MIN_TEXT_CHARS
    assert _response_answered({"type": "recall", "answer": long_enough})
    assert not _response_answered({"type": "recall", "answer": "short"})
    assert not _response_answered({"type": "reflection", "answer": "   "})
    assert not _response_answered({"type": "reflection", "answer": None})


def test_unanswered_ids_collects_only_missing_ones():
    responses = [
        {"id": 1, "type": "situational", "selected_label": "A"},
        {"id": 2, "type": "recall", "answer": ""},
        {"id": 3, "type": "reflection", "answer": "a genuinely substantive answer here"},
        {"id": 4, "type": "tradeoff"},
    ]
    assert _unanswered_ids(responses) == [2, 4]


# --- _assemble_questions ---

def _persona_with_resume():
    return SimpleNamespace(resume_data={
        "technical_questions": [
            {"dimension": "problem_solving_behavior", "text": "Explain your project's design."},
            {"dimension": "career_goals", "text": "Why did you pick that stack?"},
        ],
        "hr_questions": [
            {"dimension": "teamwork_style", "text": "A teammate disagrees...",
             "options": [{"label": "A", "text": "Talk it out", "signal": "collaborative"},
                         {"label": "B", "text": "Escalate", "signal": "process-driven"}]},
            {"dimension": "communication_style", "text": "You must give bad news...",
             "options": [{"label": "A", "text": "Directly", "signal": "candid"},
                         {"label": "B", "text": "Softly", "signal": "diplomatic"}]},
        ],
    })


def test_assemble_without_resume_falls_back_to_one_per_dimension():
    questions = _assemble_questions(None)
    # One fixed scenario per dimension plus a single closing reflection.
    assert len(questions) == len(DIMENSION_ORDER) + 1
    assert questions[-1]["type"] == "reflection"


def test_assemble_with_resume_blends_five_fixed_four_resume_one_reflection():
    questions = _assemble_questions(_persona_with_resume())
    assert len(questions) == FIXED_COUNT + 4 + 1
    # The closing question is always the open-ended reflection.
    assert questions[-1]["type"] == "reflection"
    # All four resume-grounded questions made it in (rz- prefixed ids).
    rz = [q for q in questions if str(q["id"]).startswith("rz-")]
    assert len(rz) == 4


def test_assemble_prefers_fixed_dimensions_the_resume_missed():
    questions = _assemble_questions(_persona_with_resume())
    resume_dims = {"problem_solving_behavior", "career_goals", "teamwork_style", "communication_style"}
    fixed = [q for q in questions if not str(q["id"]).startswith("rz-") and q["type"] != "reflection"]
    uncovered = [d for d in DIMENSION_ORDER if d not in resume_dims]
    # 4 dimensions are uncovered by the resume; all of them must get a fixed question.
    fixed_dims = {q["dimension"] for q in fixed}
    assert set(uncovered) <= fixed_dims
