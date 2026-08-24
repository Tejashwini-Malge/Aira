"""Pure-logic tests for account_bp's password-reset token design — no network,
no real DB (the user lookup is stubbed). A bare, minimal Flask app supplies just
the app-context + SECRET_KEY that itsdangerous needs; nothing else touches Flask.

These lock the exact security property the design depends on: a reset token
becomes worthless the instant the password it was issued against changes —
verified directly via the live-curl walkthrough during development, and here
as a fast, repeatable regression test.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

import account_bp
from account_bp import _make_token, _verify_token, MIN_PASSWORD_LEN
from models import db, User
from rate_limiter import limiter


def _app_context():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret-key"
    return app.app_context()


def _user(uid=1, password_hash="pbkdf2:sha256:abc123def456ghi789jkl"):
    return SimpleNamespace(id=uid, password_hash=password_hash)


def test_valid_token_round_trips_to_the_same_user():
    user = _user()
    with _app_context():
        token = _make_token(user)
        with patch.object(account_bp.db.session, "get", return_value=user):
            result = _verify_token(token)
    assert result is user


def test_token_fails_after_the_password_changes():
    # The exact property the whole design rests on: a token signed against the
    # OLD password_hash must stop working once the hash actually changes,
    # without any DB-side revocation list.
    user = _user(password_hash="pbkdf2:sha256:original-hash-fragment-1")
    with _app_context():
        token = _make_token(user)
        user.password_hash = "pbkdf2:sha256:changed-hash-fragment-2"
        with patch.object(account_bp.db.session, "get", return_value=user):
            result = _verify_token(token)
    assert result is None


def test_token_fails_for_unknown_user():
    user = _user()
    with _app_context():
        token = _make_token(user)
        with patch.object(account_bp.db.session, "get", return_value=None):
            result = _verify_token(token)
    assert result is None


def test_garbage_token_is_rejected():
    with _app_context():
        assert _verify_token("not-a-real-token") is None


def test_empty_token_is_rejected():
    with _app_context():
        assert _verify_token("") is None


def test_token_from_a_different_secret_key_is_rejected():
    # Simulates a token forged/signed with the wrong key entirely.
    user = _user()
    app1 = Flask(__name__); app1.config["SECRET_KEY"] = "key-one"
    app2 = Flask(__name__); app2.config["SECRET_KEY"] = "key-two"
    with app1.app_context():
        token = _make_token(user)
    with app2.app_context():
        with patch.object(account_bp.db.session, "get", return_value=user):
            result = _verify_token(token)
    assert result is None


def test_expired_token_is_rejected():
    user = _user()
    with _app_context():
        token = _make_token(user)
        # Force max_age to 0 by monkeypatching the constant used inside _verify_token.
        with patch.object(account_bp, "RESET_MAX_AGE", -1):
            with patch.object(account_bp.db.session, "get", return_value=user):
                result = _verify_token(token)
    assert result is None


def test_min_password_len_is_enforced_constant():
    # Guards against silently weakening the password floor.
    assert MIN_PASSWORD_LEN >= 8


# --- /account/forgot-password: the response must never carry a reset token ---
#
# The endpoint once returned {"token", "reset_path"} for any registered address,
# which made "know an email" enough to seize the account. These tests are the
# regression net: they assert on the whole serialized body rather than on named
# keys, so re-introducing the link under ANY key name fails here.

@pytest.fixture
def forgot_client():
    """account_bp mounted on its own app with an in-memory database. Separate
    from conftest's `app` fixture, which registers session_bp only."""
    flask_app = Flask(__name__)
    flask_app.config.update(
        SECRET_KEY="test-secret-key",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
        RATELIMIT_ENABLED=False,
    )
    db.init_app(flask_app)
    limiter.init_app(flask_app)
    flask_app.register_blueprint(account_bp.account_bp)

    with flask_app.app_context():
        db.create_all()
        row = User(name="Registered Student", email="registered@example.com")
        row.set_password("a-real-password")
        db.session.add(row)
        db.session.commit()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def test_forgot_password_never_returns_a_token_for_a_real_account(forgot_client):
    res = forgot_client.post("/account/forgot-password",
                             json={"email": "registered@example.com"})
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "token" not in body
    assert "reset-password.html" not in body


def test_forgot_password_reply_is_identical_for_unknown_addresses(forgot_client):
    """No enumeration tell: same status, same bytes, registered or not."""
    known = forgot_client.post("/account/forgot-password",
                               json={"email": "registered@example.com"})
    unknown = forgot_client.post("/account/forgot-password",
                                 json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.get_json() == unknown.get_json()


def test_a_token_issued_out_of_band_still_resets(forgot_client):
    """Closing the self-service hole must not break the manual path documented
    in the module docstring — a validly signed token still works."""
    with forgot_client.application.app_context():
        user = User.query.filter_by(email="registered@example.com").first()
        token = _make_token(user)

    res = forgot_client.post("/account/reset-password",
                             json={"token": token, "password": "brand-new-password"})
    assert res.status_code == 200

    with forgot_client.application.app_context():
        user = User.query.filter_by(email="registered@example.com").first()
        assert user.check_password("brand-new-password")
