"""Pure-helper tests for resume_agent — no upload handling, no network."""
import pytest

from resume_agent import _normalize, build_resume_questions, ResumeError


def _valid_agent_output():
    return {
        "skills": ["Python", "Flask"],
        "projects": [{"name": "Aira", "what_they_did": "Built a coach app"}],
        "experience_level": "fresher",
        "likely_gaps": ["system design"],
        "technical_questions": [
            {"dimension": "problem_solving_behavior", "text": "How did you structure Aira?"},
            {"dimension": "career_goals", "text": "Why Flask over Django?"},
        ],
        "hr_questions": [
            {"dimension": "teamwork_style", "text": "Conflict question?",
             "options": [{"label": "A", "text": "Talk", "signal": "open"},
                         {"label": "B", "text": "Wait", "signal": "passive"}]},
            {"dimension": "communication_style", "text": "Feedback question?",
             "options": [{"label": "A", "text": "Direct", "signal": "candid"},
                         {"label": "B", "text": "Gentle", "signal": "soft"}]},
        ],
    }


# --- _normalize ---

def test_normalize_passes_through_valid_output():
    data = _normalize(_valid_agent_output())
    assert data["experience_level"] == "fresher"
    assert len(data["technical_questions"]) == 2
    assert len(data["hr_questions"]) == 2
    assert all(len(q["options"]) == 2 for q in data["hr_questions"])


def test_normalize_rejects_missing_questions():
    bad = _valid_agent_output()
    bad["technical_questions"] = bad["technical_questions"][:1]
    with pytest.raises(ResumeError):
        _normalize(bad)


def test_normalize_rejects_hr_question_with_too_few_options():
    bad = _valid_agent_output()
    bad["hr_questions"][0]["options"] = bad["hr_questions"][0]["options"][:1]
    with pytest.raises(ResumeError):
        _normalize(bad)


def test_normalize_rejects_blank_question_text():
    bad = _valid_agent_output()
    bad["technical_questions"][0]["text"] = "   "
    with pytest.raises(ResumeError):
        _normalize(bad)


def test_normalize_maps_unknown_dimension_to_default():
    odd = _valid_agent_output()
    odd["technical_questions"][0]["dimension"] = "made_up_dimension"
    data = _normalize(odd)
    assert data["technical_questions"][0]["dimension"] == "problem_solving_behavior"


def test_normalize_fills_missing_option_labels():
    odd = _valid_agent_output()
    for o in odd["hr_questions"][0]["options"]:
        o.pop("label")
    data = _normalize(odd)
    assert [o["label"] for o in data["hr_questions"][0]["options"]] == ["A", "B"]


# --- build_resume_questions ---

def test_build_resume_questions_matches_fixed_bank_shape():
    data = _normalize(_valid_agent_output())
    questions = build_resume_questions(data)
    assert [q["id"] for q in questions] == ["rz-tech-1", "rz-tech-2", "rz-hr-1", "rz-hr-2"]
    # Technical questions are free-text; HR questions carry their options.
    assert all(q["type"] == "recall" and q["options"] is None for q in questions[:2])
    assert all(q["type"] == "situational" and q["options"] for q in questions[2:])


def test_build_resume_questions_handles_empty_data():
    assert build_resume_questions({}) == []
