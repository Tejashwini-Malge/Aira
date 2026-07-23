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

from flask import Flask

import account_bp
from account_bp import _make_token, _verify_token, MIN_PASSWORD_LEN


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
