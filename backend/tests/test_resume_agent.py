"""Pure-helper tests for resume_agent — no upload handling, no network."""
import pytest

from resume_agent import _build_prompt, _normalize, build_resume_questions, ResumeError


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


def test_normalize_resolves_dimension_aliases():
    """Near-misses the model actually returns are normalized to the real
    dimension rather than defaulted away from their meaning."""
    odd = _valid_agent_output()
    odd["technical_questions"][0]["dimension"] = "Problem Solving"
    odd["hr_questions"][0]["dimension"] = "leadership"
    data = _normalize(odd)
    assert data["technical_questions"][0]["dimension"] == "problem_solving_behavior"
    assert data["hr_questions"][0]["dimension"] == "leadership_tendencies"


def test_normalize_rejects_unresolvable_dimension():
    """Regression test: an unrecognised tag used to silently become
    problem_solving_behavior, crediting that dimension with evidence it never
    earned and leaving the real dimension with no question at all."""
    odd = _valid_agent_output()
    odd["technical_questions"][0]["dimension"] = "made_up_dimension"
    with pytest.raises(ResumeError):
        _normalize(odd)


def test_normalize_rejects_untagged_question():
    """A missing dimension must not fall through to a model default either."""
    odd = _valid_agent_output()
    odd["technical_questions"][0].pop("dimension")
    with pytest.raises(ResumeError):
        _normalize(odd)


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


# --- the behavioural questions test the resume's CLAIMS ---
# They used to be written to a hardcoded topic list — "(teamwork, conflict,
# motivation, ownership)" — which steered 2 of every user's 10 questions into
# team friction regardless of what their resume actually said. A real
# experienced-switcher set came back 8/10 collaboration-shaped.

RESUME = "Led the payments migration. Mentored two juniors. Built an OpenCV pipeline."


def test_the_hardcoded_topic_list_is_gone():
    prompt = _build_prompt(RESUME, {"experience": "Experienced, continuing in my field"})
    assert "(teamwork,\n     conflict, motivation, ownership)" not in prompt
    assert "behavioural/HR-interview style (teamwork" not in prompt


def test_behavioural_questions_are_pointed_at_the_resume_claims():
    prompt = _build_prompt(RESUME, {})
    assert "A resume is a set of CLAIMS" in prompt
    assert "TEST those claims" in prompt
    assert "separate someone who has really done" in prompt


def test_a_resume_with_no_interpersonal_claim_must_not_get_a_stock_team_question():
    """Absence is signal. Inventing a claim to test would measure nothing."""
    prompt = _build_prompt(RESUME, {})
    assert "do NOT fall\n       back to a stock teamwork question" in prompt
    assert "The absence is itself signal" in prompt


def test_both_questions_may_not_be_team_friction():
    assert "Do not default both questions to team friction" in _build_prompt(RESUME, {})


# --- seniors get weight, students do not ---

def test_a_fresher_gets_the_tougher_stakes_rule():
    prompt = _build_prompt(RESUME, {"experience": "Graduated, looking for my first role"})
    assert "textbook answer" in prompt
    assert "real tension" in prompt


def test_a_student_gets_the_ceiling_and_not_the_tougher_stakes():
    prompt = _build_prompt(RESUME, {"experience": "Still studying"})
    assert "no professional job experience yet" in prompt
    assert "textbook answer" not in prompt


def test_an_absent_declaration_falls_back_to_the_safe_band():
    """resume_data does not exist yet at this point in the flow, so the
    declaration is the only signal — and its absence must not crash or promote."""
    prompt = _build_prompt(RESUME, {})
    assert "no professional job experience yet" in prompt


def test_prompt_builds_with_no_onboarding_at_all():
    assert _build_prompt(RESUME, None)


def test_the_four_questions_are_asked_to_probe_four_different_dimensions():
    """Duplicate tags shrink the resume's real coverage and used to leave a
    dimension with no question anywhere in the assembled set."""
    prompt = _build_prompt(RESUME, {})
    assert "ALL FOUR MUST PROBE FOUR DIFFERENT DIMENSIONS" in prompt


def test_the_spread_rule_forbids_relabelling_to_satisfy_itself():
    """A tag is coupled to the question's content — relabelling to look spread
    credits a dimension with evidence that was never about it."""
    assert "never relabel a question just to satisfy this rule" in _build_prompt(RESUME, {})
