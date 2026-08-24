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


def _stub_generator(dimensions, onboarding=None, harder=False, resume_data=None):
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

    def counting_generator(dimensions, onboarding=None, harder=False, resume_data=None):
        calls.append(dimensions)
        return _stub_generator(dimensions, onboarding, harder, resume_data)

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


def test_assemble_forwards_resume_data_to_the_generator():
    """Regression test for the reported bug: the controller had persona.resume_data
    in hand but never passed it on, so 5 of the 10 questions were generated from
    onboarding alone and could not mention the candidate's projects or skills."""
    seen = {}

    def spy_generator(dimensions, onboarding=None, harder=False, resume_data=None):
        seen["resume_data"] = resume_data
        return _stub_generator(dimensions, onboarding, harder, resume_data)

    persona = _persona_with_resume()
    _assemble_questions(persona, question_generator=spy_generator)
    assert seen["resume_data"] is persona.resume_data


def test_assemble_passes_none_resume_data_when_there_is_no_persona():
    """A user with no persona/resume still gets a quiz — the generator is just
    told there's nothing to ground it in."""
    seen = {}

    def spy_generator(dimensions, onboarding=None, harder=False, resume_data=None):
        seen["resume_data"] = resume_data
        return _stub_generator(dimensions, onboarding, harder, resume_data)

    _assemble_questions(None, question_generator=spy_generator)
    assert seen["resume_data"] is None


def test_assemble_requests_harder_questions_on_a_refresh():
    """A persona that already has a summary is being re-assessed after real
    practice sessions — the generator must be asked for harder questions, not
    the same first-timer difficulty every time."""
    seen = {}

    def spy_generator(dimensions, onboarding=None, harder=False, resume_data=None):
        seen["harder"] = harder
        return _stub_generator(dimensions, onboarding, harder, resume_data)

    refreshing_persona = _persona_with_resume()
    refreshing_persona.summary = "An existing Core Persona summary."
    refreshing_persona.dimension_questions = None
    _assemble_questions(refreshing_persona, question_generator=spy_generator)
    assert seen["harder"] is True

    first_time_persona = _persona_with_resume()
    _assemble_questions(first_time_persona, question_generator=spy_generator)
    assert seen["harder"] is False


# --- where the unavoidable duplicate dimension goes ---
# 5 generated + 4 resume questions over 8 dimensions means one dimension is
# probed twice whenever the resume covers four. The resume's behavioural
# questions are already built around claimed collaboration, so spending the
# duplicate on another interpersonal dimension is what pushed a real
# experienced-switcher set to 8 of 10 questions about working with other people.

_INTERPERSONAL = {"work_culture_preferences", "teamwork_style",
                  "leadership_tendencies", "communication_style"}


def _dims_requested(resume_dims):
    """The dimensions _assemble_questions asks the generator for, given a resume
    that already covers `resume_dims`."""
    captured = {}

    def fake_generator(dims, onboarding=None, harder=False, resume_data=None):
        captured["dims"] = list(dims)
        return [{"id": f"gen-{d}", "dimension": d, "type": "recall",
                 "text": "q", "options": None} for d in dims]

    persona = SimpleNamespace(
        resume_data={
            "skills": ["Python"],
            "technical_questions": [
                {"dimension": d, "text": "t"} for d in resume_dims[:2]],
            "hr_questions": [
                {"dimension": d, "text": "h",
                 "options": [{"label": "A", "text": "x", "signal": "y"},
                             {"label": "B", "text": "x", "signal": "y"}]}
                for d in resume_dims[2:]],
        },
        dimension_questions=None,
        summary=None,
    )
    _assemble_questions(persona, SimpleNamespace(onboarding={}), fake_generator)
    return captured["dims"]


def test_the_duplicated_dimension_is_one_answerable_alone():
    """The resume covers four; the fifth generated slot must double up on a solo
    dimension rather than pile onto collaboration again."""
    requested = _dims_requested(["teamwork_style", "leadership_tendencies",
                                 "problem_solving_behavior", "work_culture_preferences"])
    duplicated = [d for d in requested if d in {"teamwork_style", "leadership_tendencies",
                                                "problem_solving_behavior",
                                                "work_culture_preferences"}]
    assert duplicated, "expected exactly one dimension to be probed twice"
    for d in duplicated:
        assert d not in _INTERPERSONAL, f"duplicate spent on interpersonal {d}"


def test_communication_style_is_still_pinned_into_the_generated_batch():
    """It is the only free-text recall probe for communication; the resume can
    only ever cover it with fixed options."""
    requested = _dims_requested(["communication_style", "teamwork_style",
                                 "leadership_tendencies", "problem_solving_behavior"])
    assert "communication_style" in requested


def test_uncovered_dimensions_still_win_over_covered_ones():
    requested = _dims_requested(["teamwork_style", "leadership_tendencies",
                                 "problem_solving_behavior", "professional_values"])
    for uncovered in ("decision_making_approach", "career_goals"):
        assert uncovered in requested


# --- every dimension must get a question somewhere in the set ---
# The resume agent's 4 dimension tags come back from an LLM and nothing outside
# that prompt forces them to be distinct. At a fixed 5 generated questions, a
# resume that tagged 3 of its 4 to one dimension left dimensions with no question
# anywhere — and persona_agent scores all 8 regardless, so those got a confident
# level invented from no evidence.

def _coverage(resume_dims):
    """Every dimension probed by the assembled set, and its total length."""
    captured = {}

    def fake_generator(dims, onboarding=None, harder=False, resume_data=None):
        captured["dims"] = list(dims)
        return [{"id": f"gen-{d}", "dimension": d, "type": "recall",
                 "text": "q", "options": None} for d in dims]

    persona = SimpleNamespace(
        resume_data={
            "skills": ["Python"],
            "technical_questions": [{"dimension": d, "text": "t"} for d in resume_dims[:2]],
            "hr_questions": [
                {"dimension": d, "text": "h",
                 "options": [{"label": "A", "text": "x", "signal": "y"},
                             {"label": "B", "text": "x", "signal": "y"}]}
                for d in resume_dims[2:]],
        },
        dimension_questions=None,
        summary=None,
    )
    questions = _assemble_questions(persona, SimpleNamespace(onboarding={}), fake_generator)
    probed = {q["dimension"] for q in questions if q.get("dimension")}
    return probed, len(questions), captured["dims"]


def test_all_eight_dimensions_are_probed_when_the_resume_tags_four_distinct():
    probed, length, _ = _coverage(["problem_solving_behavior", "decision_making_approach",
                                   "teamwork_style", "leadership_tendencies"])
    assert probed == set(DIMENSION_ORDER)
    assert length == 10, "the ordinary set stays the 10-question experience"


def test_a_resume_that_tags_everything_to_one_dimension_still_covers_all_eight():
    """The pathological case the floor exists for."""
    probed, length, generated = _coverage(["teamwork_style"] * 4)
    assert probed == set(DIMENSION_ORDER)
    assert len(generated) == 7, "must expand past the floor to reach every dimension"
    assert length > 10, "a longer quiz is the cost of not inventing a score"


def test_partial_duplication_expands_only_as_far_as_it_must():
    probed, _, generated = _coverage(["teamwork_style", "teamwork_style",
                                      "leadership_tendencies", "career_goals"])
    assert probed == set(DIMENSION_ORDER)
    assert len(generated) == 5, "5 uncovered dimensions — the floor already covers it"


def test_the_floor_is_never_undercut():
    """Even a perfectly-spread resume gets the full generated batch — the extra
    question is the deliberate second probe, not padding."""
    _, _, generated = _coverage(["problem_solving_behavior", "decision_making_approach",
                                 "teamwork_style", "leadership_tendencies"])
    assert len(generated) == DIMENSION_COUNT


def test_no_resume_still_covers_every_dimension():
    persona = SimpleNamespace(resume_data=None, dimension_questions=None, summary=None)
    captured = {}

    def fake_generator(dims, onboarding=None, harder=False, resume_data=None):
        captured["dims"] = list(dims)
        return [{"id": f"gen-{d}", "dimension": d, "type": "recall",
                 "text": "q", "options": None} for d in dims]

    _assemble_questions(persona, SimpleNamespace(onboarding={}), fake_generator)
    assert set(captured["dims"]) == set(DIMENSION_ORDER)
