"""Pure-helper tests for question_agent — no network.

Covers the two defects behind "the questions are vague and don't relate to my
resume": the prompt never carried resume context, and a dropped dimension fell
back to a template built from the dimension's own name.
"""
from llm_schemas import PERSONA_DIMENSIONS, normalize_dimension
from question_agent import (
    FALLBACK_QUESTIONS,
    _build_prompt,
    _format_resume_context,
    _normalize,
)

RESUME_DATA = {
    "skills": ["FastAPI", "PostgreSQL", "Docker", "OpenCV"],
    "projects": [
        {"name": "SmartAttend", "what_they_did": "Face-recognition attendance system"},
        {"name": "Medical Imaging System", "what_they_did": "CT scan segmentation pipeline"},
    ],
    "experience_level": "fresher",
    "likely_gaps": ["system design"],
}


# --- resume reaches the prompt ---

def test_prompt_contains_resume_projects_and_skills():
    prompt = _build_prompt(list(PERSONA_DIMENSIONS[:5]), {"goal": "backend developer"},
                           resume_data=RESUME_DATA)
    for fact in ("SmartAttend", "Medical Imaging System", "FastAPI", "PostgreSQL",
                 "Docker", "OpenCV", "fresher"):
        assert fact in prompt, f"{fact!r} missing from the prompt sent to Groq"


def test_prompt_instructs_the_model_to_use_the_resume():
    prompt = _build_prompt(list(PERSONA_DIMENSIONS[:5]), {}, resume_data=RESUME_DATA)
    assert "GROUND THE SCENARIOS IN THEIR OWN WORK" in prompt


def test_prompt_omits_the_resume_section_entirely_when_there_is_none():
    prompt = _build_prompt(list(PERSONA_DIMENSIONS[:5]), {"goal": "backend developer"})
    assert "THEIR RESUME" not in prompt
    assert "GROUND THE SCENARIOS" not in prompt


def test_format_resume_context_is_empty_for_empty_input():
    assert _format_resume_context(None) == ""
    assert _format_resume_context({}) == ""
    # Present-but-useless values must not produce a heading with nothing under it.
    assert _format_resume_context({"projects": [], "skills": [""], "likely_gaps": []}) == ""


def test_format_resume_context_survives_malformed_entries():
    ctx = _format_resume_context({
        "projects": [{"name": "Solo"}, {"what_they_did": "unnamed work"}, "not-a-dict", {}],
        "skills": ["Python", None, "  "],
    })
    assert "Solo" in ctx and "unnamed work" in ctx
    assert "Technical skills: Python" in ctx
    # Regression: filtering on str(v) let a null through as the literal "None".
    assert "None" not in ctx


def test_soft_skills_render_separately_from_technical_ones():
    ctx = _format_resume_context({"skills": ["FastAPI"], "soft_skills": ["mentoring juniors"]})
    assert "Technical skills: FastAPI" in ctx
    assert "Soft skills: mentoring juniors" in ctx


def test_resume_context_tolerates_rows_stored_before_soft_skills_existed():
    ctx = _format_resume_context({"skills": ["FastAPI"], "projects": []})
    assert "Technical skills: FastAPI" in ctx
    assert "Soft skills" not in ctx


# --- fallback questions are natural and do not leak the dimension ---

def test_every_dimension_has_a_natural_fallback():
    assert set(FALLBACK_QUESTIONS) == set(PERSONA_DIMENSIONS)


# Words that would give away the internal taxonomy. Deliberately excludes ordinary
# English a real interviewer uses anyway ("work", "decision", "problem") — the rule
# is that the candidate can't tell which trait is being measured, not that the
# question must avoid common words.
_GIVEAWAY_WORDS = ("culture", "teamwork", "leadership", "communication", "career", "values")


def test_fallback_questions_never_leak_the_dimension_name():
    """The whole assessment depends on the candidate not knowing which trait a
    question measures. The old template printed it verbatim."""
    for dim, text in FALLBACK_QUESTIONS.items():
        lowered = text.lower()
        assert dim not in lowered
        assert dim.replace("_", " ") not in lowered
        for word in _GIVEAWAY_WORDS:
            assert word not in lowered, f"{dim} fallback leaks {word!r}: {text!r}"


def test_fallback_questions_read_like_real_interview_questions():
    """Guards the actual regression: the old fallback was a single template with
    the dimension slotted in, so every one of them was the same sentence."""
    texts = list(FALLBACK_QUESTIONS.values())
    assert len(set(texts)) == len(texts), "fallbacks must not be one shared template"
    assert not any(t.startswith("Tell me about a recent moment that shows how you handle")
                   for t in texts)
    # Real interview prompts are either questions or imperatives ("Tell me about...",
    # "Describe..."), so only substance and terminal punctuation are asserted here.
    assert all(t.strip().endswith(("?", ".")) and len(t.split()) >= 12 for t in texts)


def test_dropped_dimensions_get_their_own_fallback_question():
    requested = list(PERSONA_DIMENSIONS[:4])
    out = _normalize([], requested)
    assert [q["dimension"] for q in out] == requested
    assert [q["text"] for q in out] == [FALLBACK_QUESTIONS[d] for d in requested]
    assert all(q["type"] == "recall" and q["options"] is None for q in out)


def test_valid_model_questions_are_kept_over_the_fallback():
    requested = list(PERSONA_DIMENSIONS[:3])
    out = _normalize([{
        "dimension": requested[1], "type": "situational", "text": "A real generated question",
        "options": [{"label": "A", "text": "one", "signal": "s"},
                    {"label": "B", "text": "two", "signal": "s"}],
    }], requested)
    assert out[1]["text"] == "A real generated question"
    assert out[0]["text"] == FALLBACK_QUESTIONS[requested[0]]


def test_near_miss_dimension_tags_are_resolved_not_dropped():
    """'leadership' used to fail the `dim not in requested_dims` check and be
    replaced by a fallback, silently discarding a perfectly good question."""
    requested = ["leadership_tendencies", "communication_style"]
    out = _normalize([{"dimension": "leadership", "type": "recall",
                       "text": "A real generated leadership question"}], requested)
    assert out[0]["text"] == "A real generated leadership question"


def test_unresolvable_dimension_is_ignored_rather_than_misfiled():
    requested = list(PERSONA_DIMENSIONS[:2])
    out = _normalize([{"dimension": "vibes", "type": "recall", "text": "off-target"}], requested)
    assert all(q["text"] != "off-target" for q in out)


# --- shared normalizer ---

def test_normalize_dimension_handles_casing_spacing_and_junk():
    assert normalize_dimension("Decision-Making") == "decision_making_approach"
    assert normalize_dimension("  TEAMWORK  ") == "teamwork_style"
    assert normalize_dimension("communication_style") == "communication_style"
    assert normalize_dimension("made_up") is None
    assert normalize_dimension("") is None
    assert normalize_dimension(None) is None
    assert normalize_dimension(42) is None
