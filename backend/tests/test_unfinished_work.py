"""Surfacing work the user began and never finished.

PendingAssessment has always held this: /quiz/evaluate and /comm/evaluate read the
row back so they grade against the questions the server issued. Nothing ever showed
it to the USER, so someone who closed the tab mid-paper never heard about it again.

It is the cheapest return available in the product — the person already chose to
start, and the work is half done — which is why it renders above everything else on
the classroom page rather than below the fold.
"""
from datetime import datetime, timedelta

import pytest
from flask import Flask

from models import db, PendingAssessment, User
from persona_bp import _PENDING_MAX_AGE_DAYS, _unfinished


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.config.update(
        SECRET_KEY="test-secret-key",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        TESTING=True,
    )
    db.init_app(flask_app)
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def student(app):
    row = User(name="Test Student", email="student@example.com")
    row.set_password("irrelevant")
    db.session.add(row)
    db.session.commit()
    return row


def _pending(user, kind, payload, age_days=0):
    row = PendingAssessment(user_id=user.id, kind=kind, payload=payload)
    db.session.add(row)
    db.session.commit()
    row.updated_at = datetime.utcnow() - timedelta(days=age_days)
    db.session.commit()
    return row


def test_nothing_pending_means_nothing_to_show(student):
    assert _unfinished(student) == []


def test_an_abandoned_quiz_comes_back_with_somewhere_to_go(student):
    _pending(student, "quiz", {"topic": "Backend developer", "questions": [{"id": 1}]})
    out = _unfinished(student)
    assert len(out) == 1
    assert out[0]["label"] == "Backend developer"
    assert out[0]["page"] == "ai_quiz.html"
    assert out[0]["daysAgo"] == 0


def test_an_abandoned_speaking_session_points_at_the_tracks(student):
    _pending(student, "comm", {"track": "intro"})
    out = _unfinished(student)
    assert out[0]["page"] == "soft_skills.html"
    assert out[0]["label"] == "Speaking practice"


def test_a_payload_with_no_topic_still_produces_a_usable_card(student):
    """A malformed or partial payload must not blank out the card's title."""
    _pending(student, "quiz", {})
    assert _unfinished(student)[0]["label"] == "Mock interview"


def test_a_payload_that_is_not_a_dict_is_survivable(student):
    _pending(student, "quiz", ["unexpected"])
    assert _unfinished(student)[0]["label"] == "Mock interview"


def test_ancient_abandoned_work_is_left_alone(student):
    """Past the cutoff this is archaeology, not an invitation — "pick up where you
    left off" about something from two months ago reads as the product having lost
    track rather than having remembered."""
    _pending(student, "quiz", {"topic": "Old paper"}, age_days=_PENDING_MAX_AGE_DAYS + 1)
    assert _unfinished(student) == []


def test_work_just_inside_the_cutoff_is_still_offered(student):
    _pending(student, "quiz", {"topic": "Recent enough"}, age_days=_PENDING_MAX_AGE_DAYS - 1)
    assert len(_unfinished(student)) == 1


def test_the_most_recent_thing_is_offered_first(student):
    """The card shows one item, so ordering decides which — and coming back to the
    thing you were most recently doing needs the least re-orientation."""
    _pending(student, "quiz", {"topic": "Older"}, age_days=5)
    _pending(student, "comm", {"track": "intro"}, age_days=1)
    out = _unfinished(student)
    assert [item["kind"] for item in out] == ["comm", "quiz"]


def test_another_students_unfinished_work_is_never_shown(app, student):
    other = User(name="Someone Else", email="other@example.com")
    other.set_password("irrelevant")
    db.session.add(other)
    db.session.commit()
    _pending(other, "quiz", {"topic": "Not yours"})
    assert _unfinished(student) == []


def test_an_unknown_kind_is_ignored_rather_than_rendered_blank(student):
    _pending(student, "something_new", {"topic": "Future feature"})
    assert _unfinished(student) == []
