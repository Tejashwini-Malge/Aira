"""Pure-logic tests for the report-snapshot activity signature — no app, no DB.

A snapshot must only be archived when the user actually does something new (takes a
quiz, completes a speaking round, rebuilds their persona) — never on a plain reload.
The signature is what decides that, so these lock exactly which changes count.
"""
from persona_bp import _activity_signature


def _payload(quizzes=0, speaking=0, summary="A steady read of you."):
    return {
        "quizzes": [{"id": i} for i in range(quizzes)],
        "speaking": [{"id": i} for i in range(speaking)],
        "persona": {"summary": summary} if summary else None,
    }


def test_identical_activity_has_identical_signature():
    # Same quizzes/rounds/persona → same signature → no new snapshot on reload.
    assert _activity_signature(_payload(1, 2)) == _activity_signature(_payload(1, 2))


def test_new_quiz_changes_signature():
    assert _activity_signature(_payload(1, 2)) != _activity_signature(_payload(2, 2))


def test_new_speaking_round_changes_signature():
    assert _activity_signature(_payload(1, 2)) != _activity_signature(_payload(1, 3))


def test_rebuilt_persona_changes_signature():
    before = _payload(1, 2, summary="Old read.")
    after = _payload(1, 2, summary="New, refreshed read.")
    assert _activity_signature(before) != _activity_signature(after)


def test_missing_persona_is_stable():
    # No persona both times (e.g. onboarding not done) → still identical, no churn.
    assert _activity_signature(_payload(0, 0, summary=None)) == \
        _activity_signature(_payload(0, 0, summary=None))


def test_empty_and_missing_lists_are_equivalent():
    # A payload with no keys must not crash and must match an explicitly-empty one.
    assert _activity_signature({}) == _activity_signature(_payload(0, 0, summary=None))
