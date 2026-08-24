"""The two round types that were coming out wrong, and the rules that fix them.

TECHNICAL. `_persona_brief` returns `focus_areas` built from the persona's weak
DIMENSIONS — "communication", "teamwork", "handling pressure" (see _DIM_LABELS).
The technical prompt used to say "lean at least half the questions toward their
weak areas: {focus_areas}", which is an instruction to write HR questions in the
technical round. While the résumé was compulsory the project facts partly masked
it; once the résumé became optional it was the only steer left, and the technical
round started sounding exactly like the HR round.

APTITUDE. A real campus paper (TCS NQT and friends) is a sectional MIX sat against
a per-question timer — numerical, reasoning and data interpretation interleaved.
The old prompt was one sentence with no blueprint, so the model chose freely and
kept landing on the same handful of topics. The blueprint is now built in Python
so the mix and the rotation are guaranteed rather than requested.
"""
import collections

import ai_quiz_bp
from ai_quiz_bp import (
    _APTITUDE_TOPICS,
    _aptitude_blueprint,
    _mark_paper,
    _role_prompt,
    _split_answer_key,
)


# --- the aptitude blueprint -------------------------------------------------

def test_a_paper_has_exactly_the_requested_number_of_questions():
    for count in (4, 5, 6, 8, 10):
        assert len(_aptitude_blueprint(count)) == count


def test_no_topic_is_asked_twice_in_the_same_paper():
    """Sampling without replacement — two percentage sums in a six-question paper
    wastes a sixth of a round the candidate only gets two of per day."""
    for count in (4, 6, 8):
        topics = [t for _, t in _aptitude_blueprint(count)]
        assert len(set(topics)) == len(topics)


def test_both_core_sections_always_appear():
    """The whole complaint was that aptitude wasn't a mix. Quantitative and logical
    are the two sections every placement paper leads with; neither may vanish."""
    for count in (4, 6, 8):
        sections = {s for s, _ in _aptitude_blueprint(count)}
        assert "quantitative ability" in sections
        assert "logical reasoning" in sections


def test_short_papers_drop_data_interpretation_rather_than_the_core_sections():
    """A DI question carries a whole table or chart with it. In a four-question
    paper that is a quarter of the round spent reading one dataset."""
    sections = collections.Counter(s for s, _ in _aptitude_blueprint(4))
    assert sections["data interpretation"] == 0
    assert sections["quantitative ability"] == 2
    assert sections["logical reasoning"] == 2


def test_longer_papers_do_include_data_interpretation():
    sections = collections.Counter(s for s, _ in _aptitude_blueprint(8))
    assert sections["data interpretation"] >= 1


def test_two_papers_in_a_row_are_not_the_same_paper():
    """Users get two free sessions a day. If the second paper repeats the first,
    the second one taught them nothing."""
    overlaps = []
    for _ in range(20):
        a = {t for _, t in _aptitude_blueprint(6)}
        b = {t for _, t in _aptitude_blueprint(6)}
        overlaps.append(len(a & b))
    assert sum(overlaps) / len(overlaps) < 4          # nowhere near identical


def test_every_blueprint_topic_is_a_real_named_topic():
    known = {t for topics in _APTITUDE_TOPICS.values() for t in topics}
    for _, topic in _aptitude_blueprint(8):
        assert topic in known


# --- the prompts ------------------------------------------------------------

BRIEF = "Candidate name: Asha\nStill weak / needs pushing on: communication, teamwork"
BEHAVIOURAL = ["communication", "teamwork", "handling pressure"]


def test_the_technical_round_is_never_aimed_at_behavioural_weak_areas(monkeypatch):
    """The regression itself: behavioural dimensions must not be handed to the
    technical round as its subject matter."""
    monkeypatch.setattr(ai_quiz_bp, "_resume_topics", lambda: ([], []))
    prompt = _role_prompt(BRIEF, BEHAVIOURAL, "Backend developer", "technical", "medium", 6)
    assert "lean at least half the questions toward their weak areas" not in prompt.lower()
    assert "teamwork" not in prompt.split("PRIVATE PROFILE")[0].lower()


def test_the_technical_round_bans_hr_questions_outright(monkeypatch):
    """Saying "be technical" is not enough — the ban has to name the shapes the
    model actually reached for."""
    monkeypatch.setattr(ai_quiz_bp, "_resume_topics", lambda: ([], []))
    prompt = _role_prompt(BRIEF, BEHAVIOURAL, "Backend developer", "technical", "medium", 6)
    lowered = prompt.lower()
    assert "technically checkable answer" in lowered
    for banned in ("motivation", "strengths and weaknesses", "teamwork", "tell me about a time"):
        assert banned in lowered          # named in the do-NOT-ask rule


def test_technical_without_a_resume_anchors_on_the_role_instead(monkeypatch):
    monkeypatch.setattr(ai_quiz_bp, "_resume_topics", lambda: ([], []))
    prompt = _role_prompt(BRIEF, [], "Backend developer", "technical", "medium", 6)
    assert "no résumé on file" in prompt
    assert "Backend developer" in prompt


def test_technical_with_a_resume_prefers_the_declared_gaps(monkeypatch):
    monkeypatch.setattr(ai_quiz_bp, "_resume_topics", lambda: (["Django"], ["SQL indexing"]))
    prompt = _role_prompt(BRIEF, [], "Backend developer", "technical", "medium", 6)
    assert "SQL indexing" in prompt
    assert "no résumé on file" not in prompt


def test_the_aptitude_paper_carries_no_persona_brief():
    """Aptitude is identical for everyone in the hall, so the profile steers nothing
    — and on a per-model daily token cap that does not roll over, sending it would
    be spend that buys nothing."""
    prompt = _role_prompt(BRIEF, BEHAVIOURAL, "Backend developer", "aptitude", "medium", 6)
    assert BRIEF not in prompt
    assert "PRIVATE PROFILE" not in prompt
    assert "Backend developer" not in prompt


# --- the answer key ---------------------------------------------------------

def _mcq(qid, answer="B"):
    return {
        "id": qid,
        "question": "A train 120 m long crosses a pole in 6 s. Find its speed.",
        "options": {"A": "60 km/h", "B": "72 km/h", "C": "20 km/h", "D": "36 km/h"},
        "answer": answer,
        "solution": "120/6 = 20 m/s = 72 km/h.",
    }


def test_the_answer_key_never_reaches_the_client():
    """The single thing that would make the whole round pointless: an answer sitting
    in the network tab and in view-source."""
    public, key = _split_answer_key([_mcq(1), _mcq(2, "D")])
    blob = repr(public)
    assert "answer" not in blob
    assert "solution" not in blob
    assert key == {
        "1": {"answer": "B", "solution": "120/6 = 20 m/s = 72 km/h."},
        "2": {"answer": "D", "solution": "120/6 = 20 m/s = 72 km/h."},
    }


def test_the_options_themselves_do_still_reach_the_client():
    public, _ = _split_answer_key([_mcq(1)])
    assert public[0]["options"]["C"] == "20 km/h"
    assert public[0]["question"]


def test_a_question_with_a_broken_key_is_kept_but_not_marked():
    """A half-generated paper should still be answerable — dropping the question
    would leave the candidate short of the count they were promised."""
    broken = _mcq(1)
    broken["answer"] = "Z"                       # not one of the four labels
    public, key = _split_answer_key([broken, _mcq(2)])
    assert len(public) == 2
    assert "1" not in key and "2" in key


def test_marking_is_arithmetic_not_opinion():
    questions, key = _split_answer_key([_mcq(1, "B"), _mcq(2, "A"), _mcq(3, "C")])
    correct, total, lines = _mark_paper(questions, {"Q1": "B", "Q2": "D", "Q3": "c"}, key)
    assert (correct, total) == (2, 3)            # Q3 lower-case still counts
    assert "CORRECT" in lines[0]
    assert "WRONG (correct: A" in lines[1]


def test_a_blank_answer_is_marked_wrong_and_shown_the_right_one():
    questions, key = _split_answer_key([_mcq(1, "B")])
    correct, total, lines = _mark_paper(questions, {}, key)
    assert (correct, total) == (0, 1)
    assert "(left blank)" in lines[0]
    assert "correct: B" in lines[0]


def test_the_working_is_carried_into_the_transcript():
    """The coach can only name the step that went wrong if it can see the working."""
    questions, key = _split_answer_key([_mcq(1, "B")])
    _, _, lines = _mark_paper(questions, {"Q1": "A"}, key)
    assert "Working: 120/6 = 20 m/s = 72 km/h." in lines[0]


def test_the_aptitude_prompt_asks_for_four_options_and_a_key():
    prompt = _role_prompt(BRIEF, [], "Backend developer", "aptitude", "medium", 6)
    assert "exactly FOUR options" in prompt
    assert '"answer"' in prompt
    assert "MULTIPLE CHOICE" in prompt


def test_the_aptitude_prompt_spells_out_the_blueprint():
    prompt = _role_prompt(BRIEF, [], "Backend developer", "aptitude", "medium", 6)
    assert "quantitative ability" in prompt
    assert "logical reasoning" in prompt
    # The rule text wraps across lines in the prompt, so match on the phrase only.
    assert "definite correct answer" in prompt
    assert "self-contained" in prompt
