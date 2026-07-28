"""Tests for the local resume filter that runs BEFORE anything reaches Groq.

The assessment only needs projects, technical skills and soft skills. Identity,
education and employer history are stripped on the server so they never reach a
third-party model.
"""
import pytest

from llm_schemas import describe_validation_error
from resume_agent import (
    SANITIZER_VERSION,
    ResumeError,
    _build_prompt,
    _classify_heading,
    _normalize,
    _redact_pii,
    extract_text,
    sanitize_resume_text,
)

FULL_RESUME = """
PRIYA SHARMA
priya.sharma99@gmail.com | +91 98765 43210 | Bengaluru, Karnataka
linkedin.com/in/priyasharma  github.com/priyasharma

CAREER OBJECTIVE
Seeking a challenging backend role where I can grow.

EDUCATION
B.E. Computer Science, Srinivas Institute of Technology, 2022-2026
CGPA: 8.7 / 10
St. Joseph's Pre-University College, 2022 — 94.2%

WORK EXPERIENCE
Backend Intern, Zenith Softwares Pvt Ltd, Jun 2025 - Aug 2025
  Maintained billing microservices for a client in Dubai.

PROJECTS
SmartAttend — Face-recognition attendance system
  Built the OpenCV pipeline and a FastAPI backend, deployed with Docker.
Medical Imaging System
  CT scan segmentation with PyTorch; designed the PostgreSQL schema.

TECHNICAL SKILLS
Python, FastAPI, PostgreSQL, Docker, OpenCV, PyTorch

SOFT SKILLS
Mentoring juniors, presenting to stakeholders, team coordination

CERTIFICATIONS
AWS Cloud Practitioner, 2025

HOBBIES
Cricket, sketching
"""

# Anything here reaching the LLM is a leak.
_PII = ["priya.sharma99", "98765", "linkedin.com", "github.com", "Srinivas Institute",
        "CGPA", "8.7", "St. Joseph", "94.2", "Zenith Softwares", "Dubai", "B.E."]

# Anything here missing means the filter ate the signal.
_SIGNAL = ["SmartAttend", "OpenCV", "FastAPI", "Medical Imaging", "PyTorch",
           "PostgreSQL", "Docker", "Mentoring", "presenting"]


def test_sanitizer_keeps_projects_and_skills():
    out = sanitize_resume_text(FULL_RESUME)
    for token in _SIGNAL:
        assert token in out, f"filter dropped signal: {token!r}"


def test_sanitizer_removes_identity_education_and_employers():
    out = sanitize_resume_text(FULL_RESUME)
    for token in _PII:
        assert token not in out, f"leaked to the LLM: {token!r}"


def test_sanitizer_drops_unrequested_sections():
    out = sanitize_resume_text(FULL_RESUME)
    for token in ("AWS Cloud Practitioner", "Cricket", "sketching", "challenging backend role"):
        assert token not in out


def test_redaction_handles_contact_details_anywhere():
    text = _redact_pii("Reach me at a.b+x@y.co.in or +91 98765 43210 see https://x.dev/a 123456789")
    for token in ("a.b+x@y.co.in", "98765", "https://x.dev/a", "123456789"):
        assert token not in text


def test_redaction_leaves_version_numbers_alone():
    """A phone/ID pattern must not eat 'Python 3.11' or 'ES2015'."""
    text = _redact_pii("Python 3.11, Node 18, ES2015, 99.9% uptime")
    for token in ("3.11", "18", "ES2015", "99.9"):
        assert token in text


def test_unstructured_resume_still_yields_text():
    """Tier 3: a resume with no headings at all must not come back empty — that
    would turn a privacy tightening into an onboarding outage."""
    prose = ("I built a face recognition attendance system using OpenCV and FastAPI, "
             "and a CT scan segmentation pipeline in PyTorch with a PostgreSQL schema.")
    out = sanitize_resume_text(prose)
    assert "OpenCV" in out and "PyTorch" in out


def test_denylisted_sections_dropped_even_without_an_allowlisted_one():
    """Tier 2: no projects/skills heading, but education must still not go."""
    text = ("EDUCATION\nB.E. at Srinivas Institute, CGPA 8.7\n\n"
            "NOTES\nI enjoy building things with OpenCV and FastAPI in my spare time.")
    out = sanitize_resume_text(text)
    assert "Srinivas Institute" not in out
    assert "OpenCV" in out


def test_sanitizer_is_idempotent():
    """save_onboarding stores the filtered text and parse_resume filters again
    internally. Re-filtering already-filtered text must be a no-op, or the
    stored copy would degrade every time it passes through."""
    once = sanitize_resume_text(FULL_RESUME)
    assert sanitize_resume_text(once) == once


def test_filtered_text_still_parses_as_the_same_sections():
    """The stored copy must remain usable input for a re-parse, not just a
    human-readable summary."""
    once = sanitize_resume_text(FULL_RESUME)
    for token in _SIGNAL:
        assert token in sanitize_resume_text(once), f"re-filtering lost {token!r}"


def test_sanitizer_is_resilient_to_empty_input():
    assert sanitize_resume_text("") == ""
    assert sanitize_resume_text(None) == ""


# --- heading vocabulary ---
#
# Regression test for the v1 bug found against real production data: the
# allowlist was matched as a PREFIX while the denylist used containment, so
# "<qualifier> <keyword>" headings — the common form — were missed, and
# "Personal/Academic Projects" were actively discarded on "personal"/"academic".
# That emptied tier 1 and pushed ~29% of resumes onto the tier-2/3 fallback,
# which sends far more text than a correct match would.

_KEEP_VOCAB = [
    "PROJECTS", "Projects", "PERSONAL PROJECTS", "ACADEMIC PROJECTS",
    "MINI PROJECTS", "MAJOR PROJECT", "KEY PROJECTS", "PROJECTS UNDERTAKEN",
    "SKILLS", "TECHNICAL SKILLS", "SOFT SKILLS", "COMPUTER SKILLS",
    "KEY SKILLS", "IT SKILLS", "SOFTWARE SKILLS", "PROFESSIONAL SKILLS",
    "TECHNICAL EXPERTISE", "TECHNICAL PROFICIENCY", "CORE COMPETENCIES",
    "AREAS OF EXPERTISE", "TECHNOLOGIES", "TECH STACK", "PORTFOLIO",
]

_DROP_VOCAB = [
    "EDUCATION", "ACADEMIC QUALIFICATION", "WORK EXPERIENCE", "EMPLOYMENT",
    "INTERNSHIP", "CONTACT", "PERSONAL DETAILS", "CAREER OBJECTIVE",
    "CERTIFICATIONS", "ACHIEVEMENTS", "HOBBIES", "LANGUAGES", "REFERENCES",
    "DECLARATION", "EXTRACURRICULAR ACTIVITIES",
]


@pytest.mark.parametrize("heading", _KEEP_VOCAB)
def test_real_world_keep_headings_are_kept(heading):
    assert _classify_heading(heading) == "keep"


@pytest.mark.parametrize("heading", _DROP_VOCAB)
def test_real_world_drop_headings_are_dropped(heading):
    assert _classify_heading(heading) == "drop"


def test_qualified_projects_section_survives_end_to_end():
    """The exact shape that was being discarded in production."""
    text = ("PERSONAL PROJECTS\nSmartAttend — built the OpenCV pipeline with FastAPI.\n\n"
            "EDUCATION\nB.E. at Srinivas Institute, CGPA 8.7\n")
    out = sanitize_resume_text(text)
    assert "SmartAttend" in out and "OpenCV" in out
    assert "Srinivas Institute" not in out and "CGPA" not in out


# --- versioning ---

def test_sanitizer_version_is_a_positive_int():
    """Stored per row so a future filter improvement can be re-applied to only
    the rows that predate it."""
    assert isinstance(SANITIZER_VERSION, int) and SANITIZER_VERSION >= 1


# --- log hygiene: metrics only, never content ---

class _FakeUpload:
    """Minimal Werkzeug FileStorage stand-in — a scanned PDF named after its owner."""

    filename = "Priya_Sharma_Resume_2026.pdf"

    def read(self):
        return b"%PDF-1.4 tiny scanned image with no text layer"


def test_extraction_failure_log_has_no_filename(capsys, monkeypatch):
    """Resumes are overwhelmingly named after their owner, so logging the
    filename verbatim put candidate names in the Render logs."""
    monkeypatch.setattr("resume_agent._extract_pdf", lambda raw: "")
    with pytest.raises(ResumeError):
        extract_text(_FakeUpload())
    log = capsys.readouterr().out
    assert "Priya" not in log and "Sharma" not in log
    assert "priya_sharma_resume_2026" not in log.lower()
    # ...but the diagnostics that matter are still there.
    assert "filetype=pdf" in log and "final_extracted_chars=0" in log


def test_validation_failure_log_has_no_resume_content(capsys):
    """str(ValidationError) embeds the offending input value — here, text the
    model derived from the candidate's resume."""
    bad = {
        "skills": ["Python"],
        "projects": [],
        "technical_questions": [
            {"dimension": "totally_made_up", "text": "Explain SmartAttend's OpenCV pipeline"},
            {"dimension": "career_goals", "text": "Why PostgreSQL?"},
        ],
        "hr_questions": [],
    }
    with pytest.raises(ResumeError):
        _normalize(bad)
    log = capsys.readouterr().out
    assert "SmartAttend" not in log and "OpenCV" not in log and "totally_made_up" not in log
    # The shape of the failure survives, so a spike is still diagnosable.
    assert "technical_questions" in log


def test_describe_validation_error_reports_location_and_type_only():
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        n: int

    try:
        M.model_validate({"n": "SmartAttend OpenCV pipeline"})
    except ValidationError as e:
        described = describe_validation_error(e)
    assert "n:" in described
    assert "SmartAttend" not in described


def test_sanitizer_fallback_log_reports_metrics_not_text(capsys):
    """The tier-2/3 fallback notice must not echo the text it fell back to."""
    sanitize_resume_text("NOTES\nI built things with OpenCV and FastAPI in my spare time, at length.")
    log = capsys.readouterr().out
    assert log, "the fallback should announce itself"
    assert "OpenCV" not in log and "FastAPI" not in log
    assert "chars" in log


def test_prompt_sends_only_the_filtered_text():
    """End-to-end: the prompt actually built for Groq carries no PII."""
    prompt = _build_prompt(FULL_RESUME, {"goal": "backend developer"})
    for token in _PII:
        assert token not in prompt, f"leaked into the Groq prompt: {token!r}"
    assert "SmartAttend" in prompt and "Mentoring" in prompt
    assert "soft_skills" in prompt
