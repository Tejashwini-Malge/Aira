"""Make the backend modules importable when pytest runs from the repo root,
and supply the HTTP-level fixtures for endpoint tests.

WHY NOT IMPORT app.py. app.py builds its Flask app at module scope, calls
load_dotenv() on backend/.env, and reads DATABASE_URL from it — which in this
repo points at the live Render Postgres. Importing it in a test would connect a
test run to production data. The `app` fixture below registers the blueprint
under test onto a bare Flask app with an in-memory SQLite database instead, so a
test can never reach a real database no matter what is in .env.
"""
import sys
from pathlib import Path

import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import db, User            # noqa: E402  (needs the path above)
from rate_limiter import limiter       # noqa: E402


@pytest.fixture
def app():
    """A minimal app carrying only what the endpoint tests exercise.

    Rate limiting is disabled: /onboarding/save allows 5 per hour keyed by user
    id, and several tests deliberately POST more than that to the same account.
    The limits themselves are configuration, verified by reading the decorator,
    not behaviour these tests are trying to pin.
    """
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

    # Imported here, not at module scope: session_controller pulls in the resume
    # and LLM agents, and importing those before sys.path is set up fails.
    from session_controller import session_bp

    flask_app.register_blueprint(session_bp)

    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user(app):
    """A committed User with no onboarding yet — the state a real account is in
    the moment it first reaches the onboarding form."""
    row = User(name="Test Student", email="student@example.com")
    row.set_password("irrelevant-for-these-tests")
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def auth_client(client, user):
    """A client whose signed session cookie carries the user id — the same thing
    login sets, without going through the password path."""
    with client.session_transaction() as flask_session:
        flask_session["user_id"] = user.id
    return client
