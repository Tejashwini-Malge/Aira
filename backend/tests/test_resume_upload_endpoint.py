"""HTTP-level tests for POST /me/resume — adding a resume after onboarding.

WHY THIS ROUTE EXISTS. The resume became optional at onboarding because it was
the heaviest first ask and the step users stopped at. That is only defensible if
there is a way back in: without one, a user who skips is permanently resume-less,
never sees the 4 resume-grounded questions, and can never use the "talk about
your project" track.

The interesting behaviour isn't the upload, it's the two side effects — the
cached question set is dropped so the next assessment regenerates WITH resume
questions, and an already-built persona becomes refreshable immediately instead
of waiting out SESSIONS_REQUIRED_TO_REFRESH. A persona built with no resume at
all is exactly the case the session threshold should not apply to.

The resume parser is stubbed, so no Groq call and no real file parsing happens.
"""
import io

import pytest

from models import db, Persona, User
from persona_eligibility import SESSIONS_REQUIRED_TO_REFRESH, refresh_eligible


@pytest.fixture
def stub_resume(monkeypatch):
    import session_controller

    monkeypatch.setattr(session_controller, "extract_text",
                        lambda f: "Projects: Dev Host promo. Skills: Python.")
    monkeypatch.setattr(session_controller, "parse_resume",
                        lambda text, details: {
                            "skills": ["Python"],
                            "soft_skills": [],
                            "projects": [{"name": "Dev Host promo",
                                          "what_they_did": "built the site"}],
                            "experience_level": "student",
                            "likely_gaps": [],
                            "technical_questions": [],
                            "hr_questions": [],
                        })


@pytest.fixture
def onboarded(auth_client, user):
    """A user who finished onboarding WITHOUT a resume — the case this route is
    for."""
    auth_client.post("/onboarding/save",
                     data={"study": "BTech", "experience": "Still studying"},
                     content_type="multipart/form-data")
    return user


def _upload(client, filename="resume.pdf"):
    return client.post("/me/resume",
                       data={"resume": (io.BytesIO(b"%PDF-1.4 fake"), filename)},
                       content_type="multipart/form-data")


# --- the basics ---

def test_upload_attaches_resume_data_to_the_persona(auth_client, onboarded, stub_resume):
    response = _upload(auth_client)

    assert response.status_code == 200
    persona = Persona.query.filter_by(user_id=onboarded.id).one()
    assert persona.resume_data["skills"] == ["Python"]
    assert persona.resume_text


def test_only_filtered_resume_text_is_stored(auth_client, onboarded, stub_resume):
    """Same privacy boundary as onboarding: the raw upload never lands in a
    column, and the row records which sanitizer produced what it holds."""
    from resume_agent import SANITIZER_VERSION

    _upload(auth_client)
    persona = Persona.query.filter_by(user_id=onboarded.id).one()
    assert persona.resume_sanitizer_version == SANITIZER_VERSION


def test_anonymous_requests_are_rejected(client, stub_resume):
    assert _upload(client).status_code == 401


def test_upload_before_onboarding_is_refused(auth_client, user, stub_resume):
    assert _upload(auth_client).status_code == 400


def test_a_request_with_no_file_is_refused(auth_client, onboarded, stub_resume):
    response = auth_client.post("/me/resume", data={},
                                content_type="multipart/form-data")
    assert response.status_code == 400


# --- the side effects that make this more than a file upload ---

def test_the_cached_question_set_is_dropped(auth_client, onboarded, stub_resume):
    """Stale cache = the next assessment would still be the resume-less set,
    silently wasting the upload."""
    persona = Persona.query.filter_by(user_id=onboarded.id).one()
    persona.dimension_questions = [{"id": "old", "question": "stale"}]
    db.session.commit()

    _upload(auth_client)

    assert Persona.query.filter_by(user_id=onboarded.id).one().dimension_questions is None


def test_an_existing_persona_becomes_refreshable_immediately(auth_client, onboarded, stub_resume):
    persona = Persona.query.filter_by(user_id=onboarded.id).one()
    persona.summary = "Built before the resume existed."
    persona.session_count_at_generation = 0
    db.session.commit()

    # Zero sessions since it was built: normally locked.
    assert refresh_eligible(0, persona) is False

    response = _upload(auth_client)
    assert response.get_json()["persona_refresh_available"] is True

    persona = Persona.query.filter_by(user_id=onboarded.id).one()
    assert persona.resume_refresh_pending is True
    assert refresh_eligible(0, persona) is True


def test_no_refresh_is_offered_when_no_persona_exists_yet(auth_client, onboarded, stub_resume):
    """Nothing to refresh — the first build will pick the resume up by itself,
    and claiming otherwise would send the user to retake an assessment they have
    not taken."""
    response = _upload(auth_client)

    assert response.get_json()["persona_refresh_available"] is False
    assert Persona.query.filter_by(user_id=onboarded.id).one().resume_refresh_pending is False


def test_the_threshold_still_applies_to_everyone_else():
    """The bypass must be scoped to a late resume, not a general weakening of the
    lock — otherwise one flag turns the refresh rule off for the whole product."""
    from types import SimpleNamespace

    normal = SimpleNamespace(session_count_at_generation=0, resume_refresh_pending=False)
    assert refresh_eligible(SESSIONS_REQUIRED_TO_REFRESH - 1, normal) is False
    assert refresh_eligible(SESSIONS_REQUIRED_TO_REFRESH, normal) is True
