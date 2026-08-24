"""The practice streak.

Two rules carry the whole feature, and both exist because the obvious version is
wrong in a way the user can see but cannot explain:

  1. Days are counted in IST, not UTC. created_at is utcnow() and IST is UTC+5:30,
     so a session at 1am local time is stamped 19:30 the PREVIOUS day. Counting in
     UTC would break a streak for practising late at night, which is when students
     practise.

  2. A streak survives until a whole day has passed with nothing in it. Counting only
     from today would drop every streak to zero each morning and rebuild it each
     evening, which teaches people to distrust the number.

It counts PRACTICE, not visits — a streak built from logins rewards opening the app
and closing it again.
"""
from datetime import datetime, timedelta

import pytest
from flask import Flask

from models import db, QuizResult, SpeakingSession, User
from persona_bp import _STREAK_TZ_OFFSET, _streak


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


def _ist_now():
    return datetime.utcnow() + _STREAK_TZ_OFFSET


def _quiz_on_local_day(user, days_ago, hour=12):
    """A quiz sat at `hour` local time, `days_ago` local days back — stored in UTC
    exactly as the app stores it."""
    local = (_ist_now() - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    row = QuizResult(user_id=user.id, topic="t", score="5/5", feedback="{}")
    db.session.add(row)
    db.session.commit()
    row.created_at = local - _STREAK_TZ_OFFSET      # back to UTC, as written
    db.session.commit()
    return row


def test_a_student_who_never_practised_has_no_streak(student):
    assert _streak(student) == {"current": 0, "totalDays": 0, "practisedToday": False}


def test_practising_today_starts_a_streak_of_one(student):
    _quiz_on_local_day(student, 0)
    s = _streak(student)
    assert s["current"] == 1
    assert s["practisedToday"] is True


def test_three_consecutive_days_count_as_three(student):
    for d in (0, 1, 2):
        _quiz_on_local_day(student, d)
    assert _streak(student)["current"] == 3


def test_yesterday_alone_keeps_the_streak_alive_today(student):
    """The grace rule. At 9am, someone who practised yesterday has not lost anything
    yet — they still have all of today."""
    _quiz_on_local_day(student, 1)
    s = _streak(student)
    assert s["current"] == 1
    assert s["practisedToday"] is False


def test_a_missed_day_ends_the_streak(student):
    _quiz_on_local_day(student, 2)
    _quiz_on_local_day(student, 3)
    assert _streak(student)["current"] == 0


def test_a_gap_does_not_join_two_runs_together(student):
    for d in (0, 1, 3, 4, 5):
        _quiz_on_local_day(student, d)
    assert _streak(student)["current"] == 2          # not 5


def test_twice_in_one_day_is_still_one_day(student):
    _quiz_on_local_day(student, 0, hour=9)
    _quiz_on_local_day(student, 0, hour=21)
    s = _streak(student)
    assert s["current"] == 1
    assert s["totalDays"] == 1


def test_a_speaking_session_counts_as_practice_too(student):
    """Both halves of the product are practice; a streak that only counted quizzes
    would punish the users doing the thing we most want them to do."""
    row = SpeakingSession(user_id=student.id, track="intro")
    db.session.add(row)
    db.session.commit()
    row.created_at = datetime.utcnow()
    db.session.commit()
    assert _streak(student)["current"] == 1


def test_late_night_practice_belongs_to_the_local_day_not_the_utc_one(student):
    """The bug this whole offset exists for. 00:30 IST is 19:00 UTC the day before:
    counted in UTC, a student practising just after midnight would be credited to
    yesterday and could break a streak they had actually kept."""
    just_after_midnight = _ist_now().replace(hour=0, minute=30, second=0, microsecond=0)
    row = QuizResult(user_id=student.id, topic="t", score="5/5", feedback="{}")
    db.session.add(row)
    db.session.commit()
    row.created_at = just_after_midnight - _STREAK_TZ_OFFSET
    db.session.commit()

    assert row.created_at.date() != just_after_midnight.date()   # differs in UTC
    assert _streak(student)["practisedToday"] is True            # right day locally


def test_total_days_counts_every_day_ever_not_just_the_run(student):
    for d in (0, 1, 6, 20):
        _quiz_on_local_day(student, d)
    s = _streak(student)
    assert s["current"] == 2
    assert s["totalDays"] == 4
