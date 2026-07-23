"""Pure-helper tests for session_controller — no Flask app, no network."""
from types import SimpleNamespace

from session_controller import (
    _response_answered,
    _unanswered_ids,
    _assemble_questions,
    DIMENSION_ORDER,
    DIMENSION_COUNT,
    MIN_TEXT_CHARS,
)


def _stub_generator(dimensions, onboarding=None, harder=False):
    """Deterministic stand-in for the real (Groq-backed) generator, so these
    tests exercise the blending/coverage logic without hitting the network."""
    return [
        {"id": f"gen-{d}", "dimension": d, "type": "recall", "text": f"stub question for {d}", "options": None}
        for d in dimensions
    ]


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
    return SimpleNamespace(dimension_questions=None, summary=None, resume_data={
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
    questions = _assemble_questions(None, question_generator=_stub_generator)
    # One generated question per dimension plus a single closing reflection.
    assert len(questions) == len(DIMENSION_ORDER) + 1
    assert questions[-1]["type"] == "reflection"


def test_assemble_with_resume_blends_five_generated_four_resume_one_reflection():
    questions = _assemble_questions(_persona_with_resume(), question_generator=_stub_generator)
    assert len(questions) == DIMENSION_COUNT + 4 + 1
    # The closing question is always the open-ended reflection.
    assert questions[-1]["type"] == "reflection"
    # All four resume-grounded questions made it in (rz- prefixed ids).
    rz = [q for q in questions if str(q["id"]).startswith("rz-")]
    assert len(rz) == 4


def test_assemble_prefers_generated_dimensions_the_resume_missed():
    questions = _assemble_questions(_persona_with_resume(), question_generator=_stub_generator)
    resume_dims = {"problem_solving_behavior", "career_goals", "teamwork_style", "communication_style"}
    generated = [q for q in questions if not str(q["id"]).startswith("rz-") and q["type"] != "reflection"]
    uncovered = [d for d in DIMENSION_ORDER if d not in resume_dims]
    # 4 dimensions are uncovered by the resume; all of them must get a generated question.
    generated_dims = {q["dimension"] for q in generated}
    assert set(uncovered) <= generated_dims


def test_assemble_always_generates_communication_style_recall():
    """Regression test: even when the resume tags an HR (fixed-option) question to
    communication_style — so it counts as 'covered' — the quiz must still get a
    generated free-text recall probe for it, or communication has no open answer."""
    # The fixture's resume tags an hr_question to communication_style.
    questions = _assemble_questions(_persona_with_resume(), question_generator=_stub_generator)
    generated = [q for q in questions if not str(q["id"]).startswith("rz-") and q["type"] != "reflection"]
    generated_dims = {q["dimension"] for q in generated}
    assert "communication_style" in generated_dims


def test_assemble_reuses_cache_during_a_refresh_attempt():
    """Regression test: a persona that already has a summary (i.e. mid-refresh,
    not a first build) must still reuse cached dimension_questions on reload
    instead of regenerating every call — this was the bug where `persona.summary`
    being truthy alone forced a fresh LLM call on every single get-questions
    request during the entire refresh attempt."""
    calls = []

    def counting_generator(dimensions, onboarding=None, harder=False):
        calls.append(dimensions)
        return _stub_generator(dimensions, onboarding, harder)

    persona = _persona_with_resume()
    persona.summary = "An existing Core Persona summary."
    persona.dimension_questions = [
        {"id": "gen-cached", "dimension": "work_culture_preferences", "type": "recall", "text": "cached", "options": None}
    ]

    _assemble_questions(persona, question_generator=counting_generator)
    assert calls == [], "reload during a refresh attempt must reuse the cache, not regenerate"


def test_assemble_regenerates_once_cache_is_cleared():
    """The other half of the fix: once dimension_questions is cleared (as
    generate_persona() now does after every successful build), the next
    get-questions call must generate a fresh set again."""
    persona = _persona_with_resume()
    persona.summary = "An existing Core Persona summary."
    persona.dimension_questions = None

    questions = _assemble_questions(persona, question_generator=_stub_generator)
    assert len(questions) == DIMENSION_COUNT + 4 + 1
    assert persona.dimension_questions is not None


def test_assemble_requests_harder_questions_on_a_refresh():
    """A persona that already has a summary is being re-assessed after real
    practice sessions — the generator must be asked for harder questions, not
    the same first-timer difficulty every time."""
    seen = {}

    def spy_generator(dimensions, onboarding=None, harder=False):
        seen["harder"] = harder
        return _stub_generator(dimensions, onboarding, harder)

    refreshing_persona = _persona_with_resume()
    refreshing_persona.summary = "An existing Core Persona summary."
    refreshing_persona.dimension_questions = None
    _assemble_questions(refreshing_persona, question_generator=spy_generator)
    assert seen["harder"] is True

    first_time_persona = _persona_with_resume()
    _assemble_questions(first_time_persona, question_generator=spy_generator)
    assert seen["harder"] is False
