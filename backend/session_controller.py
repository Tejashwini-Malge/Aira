from flask import Blueprint, request, jsonify
import json
import os
import random

from models import db, Persona
from auth import current_user, login_required
from rate_limiter import limiter
from llm_schemas import PERSONA_DIMENSIONS
from persona_eligibility import (
    SESSIONS_REQUIRED_TO_REFRESH,
    total_sessions as _total_sessions,
    refresh_eligible as _refresh_eligible,
)
from resume_agent import extract_text, parse_resume, build_resume_questions, ResumeError
from question_agent import generate_dimension_questions, QuestionGenerationError
from persona_agent import generate_core_persona, format_session_evidence, PersonaGenerationError
from persona_bp import _build_report_payload

session_bp = Blueprint("session_bp", __name__)


def _reject_if_locked(total_sessions, persona):
    """A Core Persona is locked once built; only an eligible refresh may touch
    it again. Enforced here — not just at generate-persona's final call — so a
    non-eligible user can't overwrite raw_responses or burn a Groq call on
    get-questions/save-answers by reaching them directly. The frontend's entry
    guard on questions.html is a UX nicety, not the actual gate; this is."""
    if persona is not None and persona.summary and not _refresh_eligible(total_sessions, persona):
        return jsonify({
            "error": "Your Core Persona is locked until Aira has enough new session evidence to refresh it.",
        }), 403
    return None


_BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "question_bank.json")
with open(_BANK_PATH, encoding="utf-8") as _f:
    _QUESTION_BANK = json.load(_f)

# Fixed order so dimension levels always appear in this sequence.
DIMENSION_ORDER = list(PERSONA_DIMENSIONS)


def _reflections():
    """The 2 generic, dimension-less closing questions — these stay hardcoded on
    purpose: they're deliberately open ("what's a good day look like for you")
    and don't target a specific trait, so personalizing them buys nothing."""
    return [q for q in _QUESTION_BANK if q["type"] == "reflection"]


# How many dimension questions go into the blended quiz. The resume agent
# contributes 4 more (2 technical + 2 HR) and we close with 1 open-ended reflection,
# for a 10-question quiz the user experiences as one seamless set.
DIMENSION_COUNT = 5


def _assemble_questions(persona, user=None, question_generator=generate_dimension_questions):
    """The hidden blend: 5 dynamically generated dimension questions + 4 resume-
    grounded + 1 open-ended reflection.

    The dimension questions are LLM-generated per user (not drawn from a static
    bank — see question_agent.py) and cover whichever dimensions the resume
    questions DON'T, so all 8 dimensions get signal between the two sources.
    They're cached on the persona so reloading mid-quiz doesn't reshuffle the set
    or cost another LLM call. generate_persona() clears this cache the moment a
    persona is (re)built, so the NEXT attempt (a future eligible refresh) starts
    fresh exactly once, then stays cached for the rest of that attempt.
    """
    reflections = _reflections()

    resume_questions = []
    if persona is not None and persona.resume_data:
        resume_questions = build_resume_questions(persona.resume_data)

    covered = {q["dimension"] for q in resume_questions}
    # Prefer dimensions the resume didn't already probe, but ALWAYS keep
    # communication_style in the generated batch: it's the one dimension question_agent
    # asks as a free-text "tell me about a time..." recall, and the resume can only ever
    # cover it with a fixed-option question. Without this pin it silently drops out of the
    # top-N whenever a resume HR question happens to be tagged to it, leaving the quiz with
    # no open recall probe for communication. Priority: comm_style, then uncovered, then covered.
    ordered_dims = sorted(
        DIMENSION_ORDER,
        key=lambda d: 0 if d == "communication_style" else (1 if d not in covered else 2),
    )
    # Without resume data there's no coverage to fill around — cover every dimension.
    target_dims = ordered_dims[:DIMENSION_COUNT] if resume_questions else DIMENSION_ORDER

    need_fresh = persona is None or not persona.dimension_questions
    if need_fresh:
        onboarding = user.onboarding if user else None
        # A persona that already has a summary is being re-assessed after real
        # practice sessions, not built for the first time — raise the bar.
        is_refresh = persona is not None and bool(persona.summary)
        dim_questions = question_generator(target_dims, onboarding, harder=is_refresh)
        if persona is not None:
            # Caching is the caller's DB transaction to commit — this function
            # stays testable without a Flask app/DB context.
            persona.dimension_questions = dim_questions
    else:
        dim_questions = persona.dimension_questions

    if not resume_questions:
        selected = list(dim_questions)
        selected.extend(reflections[:1] or reflections)
        return selected

    # Blend so the resume questions aren't visibly clustered, then close with a
    # single open-ended reflection.
    blended = dim_questions + resume_questions
    random.shuffle(blended)
    if reflections:
        blended.append(random.choice(reflections))
    return blended


def _get_or_create_persona(user):
    if user.persona is None:
        persona = Persona(user_id=user.id)
        db.session.add(persona)
        return persona
    return user.persona


# A free-text answer needs real substance, not a single word, to be worth interpreting.
MIN_TEXT_CHARS = 15


def _response_answered(r):
    """True only if this response carries a real answer Aira can read."""
    if r.get("type") in ("situational", "tradeoff"):
        return bool(r.get("selected_label"))
    return len((r.get("answer") or "").strip()) >= MIN_TEXT_CHARS


def _unanswered_ids(responses):
    return [r.get("id") for r in responses if not _response_answered(r)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@session_bp.route("/onboarding/status", methods=["GET"])
@login_required
def onboarding_status():
    user = current_user()
    return jsonify({
        "complete": bool(user.onboarding_complete),
        "onboarding": user.onboarding or {},
    }), 200


@session_bp.route("/onboarding/save", methods=["POST"])
@login_required
@limiter.limit("5 per hour")
def save_onboarding():
    """Collect career details + a compulsory resume, then run the resume agent.

    The resume is parsed into structured signal AND the 4 resume-grounded persona
    questions, stored on the Persona for the quiz to blend in later.
    """
    user = current_user()

    # Resume analysis is a real Groq call and onboarding is meant to happen once —
    # without this, a logged-in user could resubmit the form in a loop and burn a
    # full resume-parsing call every time.
    if user.onboarding_complete:
        return jsonify({"success": False, "message": "Onboarding is already complete."}), 400

    # Career-development detail fields (everything except the file). Stored as a
    # flexible blob so the field set can evolve without a migration.
    details = {k: v.strip() for k, v in request.form.items() if v and v.strip()}

    resume_file = request.files.get("resume")
    if resume_file is None or not resume_file.filename:
        return jsonify({"success": False, "message": "A resume is required to continue."}), 400

    try:
        resume_text = extract_text(resume_file)
        resume_data = parse_resume(resume_text, details)
    except ResumeError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    persona = _get_or_create_persona(user)
    persona.resume_text = resume_text
    persona.resume_data = resume_data

    user.onboarding = details
    user.onboarding_complete = True
    db.session.commit()

    return jsonify({"success": True}), 200


@session_bp.route("/session/get-questions", methods=["GET"])
@login_required
@limiter.limit("15 per hour")
def get_questions():
    user = current_user()
    locked = _reject_if_locked(_total_sessions(user), user.persona)
    if locked:
        return locked
    try:
        questions = _assemble_questions(user.persona, user)
    except QuestionGenerationError as e:
        return jsonify({"error": str(e)}), 503
    db.session.commit()
    return jsonify({"questions": questions}), 200


@session_bp.route("/session/save-answers", methods=["POST"])
@login_required
def save_answers():
    user = current_user()
    locked = _reject_if_locked(_total_sessions(user), user.persona)
    if locked:
        return locked

    data = request.get_json() or {}
    responses = data.get("responses")
    if not responses or not isinstance(responses, list):
        return jsonify({"success": False, "message": "Expected a 'responses' array"}), 400

    # Refuse to store a half-finished onboarding — a persona is only honest if it's
    # built from every answer, not judged from one and padded with defaults.
    unanswered = _unanswered_ids(responses)
    if unanswered:
        return jsonify({
            "success": False,
            "message": f"{len(unanswered)} question(s) still need a real answer before Aira can build your profile.",
            "unanswered": unanswered,
        }), 400

    persona = _get_or_create_persona(user)
    persona.raw_responses = responses
    db.session.commit()
    return jsonify({"success": True}), 200


@session_bp.route("/session/generate-persona", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def generate_persona():
    user = current_user()
    persona = _get_or_create_persona(user)
    total_sessions = _total_sessions(user)

    # `force` re-runs the assessment from the saved answers even if one already exists.
    # Useful while iterating on the logic, and the basis for re-assessing after sessions.
    force = bool((request.get_json(silent=True) or {}).get("force")) or \
        request.args.get("force") in ("1", "true")

    # Otherwise generate exactly once — return the existing persona on later calls.
    # (summary is the sentinel now that we no longer produce an abstract title.)
    if persona.summary and not force:
        return jsonify({"persona": persona.to_dict()}), 200

    # A Core Persona already exists and the caller wants to replace it — only allowed
    # once enough new practice sessions have happened since it was built. Defense in
    # depth: the frontend hides the refresh action until eligible, but this is the
    # rule that actually holds regardless of what the client sends.
    if persona.summary and force and not _refresh_eligible(total_sessions, persona):
        return jsonify({
            "error": "Aira doesn't have enough new session evidence yet to refresh your profile.",
            "canRefresh": False,
        }), 403

    if not persona.raw_responses:
        return jsonify({"error": "No onboarding responses found. Complete the questions first."}), 400

    # Defense in depth: never judge a person from incomplete answers, even if a
    # request reaches here directly. The frontend enforces this too.
    unanswered = _unanswered_ids(persona.raw_responses)
    if unanswered:
        return jsonify({
            "error": f"Your profile needs all questions answered first — {len(unanswered)} still missing.",
            "unanswered": unanswered,
        }), 400

    # A refresh (not a first build) weighs real practice-session performance alongside
    # the questionnaire answers, not just a repeat of the same quiz.
    session_evidence = format_session_evidence(_build_report_payload(user)) if persona.summary else None

    try:
        result = generate_core_persona(persona.raw_responses, user.onboarding, persona.resume_data, session_evidence)
    except PersonaGenerationError as e:
        # Don't set persona.summary — leaving it unset means the next call retries
        # cleanly instead of returning a permanently-cached fallback.
        return jsonify({"error": str(e)}), 503

    persona.summary = result["summary"]
    persona.dimensions = result["dimensions"]
    # Reset the eligibility baseline — the NEXT refresh needs its own fresh evidence.
    persona.session_count_at_generation = total_sessions
    # Clear the dimension-question cache: it belonged to THIS attempt (already
    # spent). The next attempt (a future eligible refresh) generates its own
    # fresh set exactly once via _assemble_questions' need_fresh check.
    persona.dimension_questions = None
    db.session.commit()

    # The persona is private — never return its content to the client. Confirm only
    # that it was built. It lives server-side and drives the other modules.
    return jsonify({"persona": {"ready": True}}), 200


@session_bp.route("/me/persona", methods=["GET"])
@login_required
def get_my_persona():
    """Existence + refresh-eligibility check only (used for gating). Never exposes
    the assessment content, and never exposes the raw session-count rule — only
    whether Aira currently has enough new evidence to offer a refresh."""
    user = current_user()
    persona = user.persona
    ready = bool(persona and persona.summary)
    if not ready:
        return jsonify({"persona": None}), 200

    total_sessions = _total_sessions(user)
    return jsonify({
        "persona": {
            "ready": True,
            "totalSessions": total_sessions,
            "canRefresh": _refresh_eligible(total_sessions, persona),
        }
    }), 200
