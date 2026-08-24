"""last_seen_at — the only signal that anyone ever came back.

A return visit leaves no trace anywhere else unless the user finishes a quiz or a
speaking session, and almost nobody does. Without this column, a student who logged
in on three separate days and practised nothing is indistinguishable from one who
never returned after signing up.

The column only accrues data going forward, which is the whole reason it was added
before there was anything to measure: added in October it can say nothing about
August.
"""
from datetime import datetime, timedelta

import pytest
from flask import Flask, jsonify

from auth import login_required, touch_last_seen
from models import db, User


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.config.update(
        SECRET_KEY="test-secret-key",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        TESTING=True,
    )
    db.init_app(flask_app)

    @flask_app.route("/protected")
    @login_required
    def protected():
        return jsonify({"ok": True})

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


def test_a_new_account_has_never_been_seen(student):
    """NULL, not created_at. Back-filling would invent a return visit: a signup in
    March that never came back would read as 'active in March'."""
    assert student.last_seen_at is None


def test_hitting_a_protected_endpoint_records_the_visit(app, student):
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_id"] = student.id

    assert client.get("/protected").status_code == 200
    assert db.session.get(User, student.id).last_seen_at is not None


def test_an_unauthenticated_request_records_nothing(app, student):
    """The 401 path must not touch the column — otherwise a bot hammering a
    protected URL would look like the user showing up."""
    app.test_client().get("/protected")
    assert db.session.get(User, student.id).last_seen_at is None


def test_a_second_visit_the_same_day_does_not_write_again(app, student):
    """Day granularity: the question is "did they come back on ANOTHER day", and
    per-request writes would put a database write on every authenticated request in
    the app to sharpen a signal nobody needs sharper."""
    touch_last_seen(student.id)
    first = db.session.get(User, student.id).last_seen_at
    assert first is not None

    touch_last_seen(student.id)
    assert db.session.get(User, student.id).last_seen_at == first


def test_a_visit_on_a_later_day_does_write(app, student):
    student.last_seen_at = datetime.utcnow() - timedelta(days=3)
    db.session.commit()
    stale = student.last_seen_at

    touch_last_seen(student.id)
    assert db.session.get(User, student.id).last_seen_at > stale


def test_a_deleted_user_id_in_the_session_is_survivable(app, student):
    """A signed cookie can outlive the account it names."""
    ghost = student.id
    db.session.delete(student)
    db.session.commit()
    touch_last_seen(ghost)          # must not raise


def test_a_write_failure_never_breaks_the_request(app, student, monkeypatch):
    """This is a statistic. A statistic must never turn a working request into a
    500 for the user standing in front of it."""
    def boom():
        raise RuntimeError("database went away")

    monkeypatch.setattr(db.session, "commit", boom)
    touch_last_seen(student.id)     # swallowed

    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_id"] = student.id
    assert client.get("/protected").status_code == 200
