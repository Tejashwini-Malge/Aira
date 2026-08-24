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
from resume_agent import (
    extract_text,
    parse_resume,
    build_resume_questions,
    sanitize_resume_text,
    SANITIZER_VERSION,
    ResumeError,
)
from onboarding_schema import clean_onboarding
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


# The FLOOR for how many dimension questions go into the blended quiz — not a fixed
# count. The resume agent contributes 4 more (2 technical + 2 HR) and we close with
# 1 open-ended reflection, so the usual set is the 10 questions the user experiences
# as one seamless whole.
#
# It's a floor because the resume agent's 4 dimension tags come back from an LLM and
# nothing outside that prompt can force them to be distinct. When it tags several
# questions to the same dimension its real coverage shrinks, and at a fixed 5 the
# generated batch could no longer reach every remaining dimension — leaving a
# dimension with no question anywhere in the set, which persona_agent then scores
# anyway because its schema requires all 8. A confident level invented from no
# evidence is worse than a slightly longer quiz.
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

    resume_data = persona.resume_data if persona is not None else None

    resume_questions = []
    if resume_data:
        resume_questions = build_resume_questions(resume_data)

    covered = {q["dimension"] for q in resume_questions}
    # 5 generated + 4 resume questions across 8 dimensions means exactly one
    # dimension gets probed twice whenever the resume covers four. Which one
    # matters: the resume's behavioural questions are already built around the
    # candidate's claimed collaboration, so doubling up on another interpersonal
    # dimension is what pushed a real set to 8 of 10 questions about working with
    # other people. Spend the duplicate on a dimension answerable alone.
    interpersonal = {"work_culture_preferences", "teamwork_style",
                     "leadership_tendencies", "communication_style"}
    # Prefer dimensions the resume didn't already probe, but ALWAYS keep
    # communication_style in the generated batch: it's the one dimension question_agent
    # asks as a free-text "tell me about a time..." recall, and the resume can only ever
    # cover it with a fixed-option question. Without this pin it silently drops out of the
    # top-N whenever a resume HR question happens to be tagged to it, leaving the quiz with
    # no open recall probe for communication. Priority: comm_style, then uncovered, then covered.
    def _priority(d):
        if d == "communication_style":
            return 0
        if d not in covered:
            return 1
        # Already probed by a resume question — only reached when a duplicate is
        # unavoidable. Solo-answerable dimensions come first.
        return 3 if d in interpersonal else 2

    ordered_dims = sorted(DIMENSION_ORDER, key=_priority)
    if not resume_questions:
        # No resume to fill around — cover every dimension.
        target_dims = DIMENSION_ORDER
    else:
        # Take the floor, or enough to reach every dimension the resume left
        # untouched, whichever is larger. Deliberately NOT done by relabelling a
        # duplicated resume question: a dimension tag is coupled to the question's
        # content, so remapping it credits a dimension with evidence that was never
        # about it (the same reason llm_schemas.normalize_dimension returns None
        # instead of guessing). Generating a real question for the missing
        # dimension is the repair; retagging is only the appearance of one.
        uncovered = [d for d in DIMENSION_ORDER if d not in covered]
        needed = max(DIMENSION_COUNT, len(uncovered))
        if needed > DIMENSION_COUNT:
            print(f"[assemble] resume questions covered only {len(covered)} "
                  f"dimension(s) — generating {needed} instead of {DIMENSION_COUNT} "
                  f"so every dimension gets a question")
        target_dims = ordered_dims[:needed]

    need_fresh = persona is None or not persona.dimension_questions
    if need_fresh:
        onboarding = user.onboarding if user else None
        # A persona that already has a summary is being re-assessed after real
        # practice sessions, not built for the first time — raise the bar.
        is_refresh = persona is not None and bool(persona.summary)
        # resume_data is what makes these questions about THIS candidate. Without
        # it the generator only ever saw goal/field/year and had no way to
        # mention their projects or skills — the "questions don't relate to my
        # resume" bug. The resume-grounded questions are only 4 of the 10.
        dim_questions = question_generator(
            target_dims, onboarding, harder=is_refresh, resume_data=resume_data
        )
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
    persona = user.persona
    return jsonify({
        "complete": bool(user.onboarding_complete),
        "onboarding": user.onboarding or {},
        # Whether a resume is on file — not its contents. The profile page uses
        # this to decide between "Add your resume" and "Replace it". Now that the
        # resume is optional at onboarding, "onboarded" no longer implies "has one".
        "resume": bool(persona and persona.resume_data),
    }), 200


@session_bp.route("/onboarding/save", methods=["POST"])
@login_required
@limiter.limit("5 per hour")
def save_onboarding():
    """Collect career details, and a resume if the user has one to hand.

    The resume is OPTIONAL. When present it's parsed into structured signal AND
    the 4 resume-grounded persona questions, stored on the Persona for the quiz
    to blend in later. When absent, everything downstream already has a real
    resume-less path — _assemble_questions covers all 8 dimensions with generated
    questions instead of 5, persona_agent writes "(no resume on file)" into its
    context, and candidate_profile routes off the declared band rather than
    inferring one. Requiring the file was the heaviest possible first ask (a
    student on a phone does not have a PDF), and it was the step users stopped at.

    A resume added later, from the profile page, goes to /me/resume below.
    """
    user = current_user()

    # Resume analysis is a real Groq call and onboarding is meant to happen once —
    # without this, a logged-in user could resubmit the form in a loop and burn a
    # full resume-parsing call every time.
    if user.onboarding_complete:
        return jsonify({"success": False, "message": "Onboarding is already complete."}), 400

    # Career-development detail fields (everything except the file). Allowlisted
    # and capped by onboarding_schema rather than stored as submitted: this blob
    # is flattened into two LLM prompts downstream, so an undeclared form field
    # would be an unbounded attacker-controlled string with a direct path into a
    # model prompt. Still a flexible JSON blob on the model — the field set can
    # evolve by editing the schema, with no migration.
    details = clean_onboarding(request.form)

    persona = _get_or_create_persona(user)

    resume_file = request.files.get("resume")
    if resume_file is None or not resume_file.filename:
        user.onboarding = details
        user.onboarding_complete = True
        db.session.commit()
        return jsonify({"success": True, "resume": False}), 200

    try:
        # --- privacy boundary -------------------------------------------------
        # raw_resume holds the candidate's name, contact details, college and
        # employers. It exists for exactly two statements and is dropped before
        # anything else runs, so no later edit in this function can reach it and
        # accidentally send or store it. Past this point `sanitized_resume` is
        # the only resume text in scope, and it is what BOTH the Groq call and
        # the stored column receive.
        #
        # (`del` is a scoping/readability guarantee, not a memory-wipe: CPython
        # frees the string but does not zero the bytes, and the upload buffer
        # still exists on the request. It makes the boundary explicit and makes
        # a later misuse a NameError rather than a silent leak.)
        raw_resume = extract_text(resume_file)
        sanitized_resume = sanitize_resume_text(raw_resume)
        del raw_resume
        # ----------------------------------------------------------------------
        # parse_resume filters again internally as a safety net for other
        # callers; sanitize_resume_text is idempotent, so this is a no-op.
        resume_data = parse_resume(sanitized_resume, details)
    except ResumeError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    persona.resume_text = sanitized_resume
    persona.resume_sanitizer_version = SANITIZER_VERSION
    persona.resume_data = resume_data

    user.onboarding = details
    user.onboarding_complete = True
    db.session.commit()

    return jsonify({"success": True, "resume": True}), 200


@session_bp.route("/me/resume", methods=["POST"])
@login_required
@limiter.limit("5 per hour")
def upload_resume():
    """Add or replace the resume after onboarding — the way back in for anyone
    who skipped it (and the way to update a stale one for anyone who didn't).

    Costs a Groq call, so it carries save's rate limit rather than update's.

    What makes this more than a file upload: the resume is the largest single
    piece of evidence the persona can be built from, and a user who skipped it
    has a persona built without it. So this also
      * clears the cached dimension_questions, so the next /session/get-questions
        regenerates the set — now with the 4 resume-grounded questions blended in
        and 5 generated ones instead of 8, and
      * sets resume_refresh_pending, which makes the locked persona eligible for
        one immediate refresh instead of waiting out the 3-session threshold.
    A persona that has never been built needs neither: the first build will pick
    the resume up on its own.
    """
    user = current_user()
    if not user.onboarding_complete:
        return jsonify({
            "success": False,
            "message": "Finish onboarding first — the resume goes in there.",
        }), 400

    resume_file = request.files.get("resume")
    if resume_file is None or not resume_file.filename:
        return jsonify({"success": False, "message": "No resume file was uploaded."}), 400

    try:
        # Same privacy boundary as save_onboarding: the raw text exists for two
        # statements and is dropped before anything else can reach it.
        raw_resume = extract_text(resume_file)
        sanitized_resume = sanitize_resume_text(raw_resume)
        del raw_resume
        resume_data = parse_resume(sanitized_resume, user.onboarding)
    except ResumeError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    persona = _get_or_create_persona(user)
    persona.resume_text = sanitized_resume
    persona.resume_sanitizer_version = SANITIZER_VERSION
    persona.resume_data = resume_data
    persona.dimension_questions = None

    # Only meaningful if a persona actually exists to be refreshed.
    persona_exists = bool(persona.summary)
    if persona_exists:
        persona.resume_refresh_pending = True

    db.session.commit()

    return jsonify({
        "success": True,
        # The frontend uses this to decide what to offer next: retake the
        # assessment (a persona exists and is now stale) or just carry on.
        "persona_refresh_available": persona_exists,
    }), 200


@session_bp.route("/onboarding/update", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def update_onboarding():
    """Edit the career details captured at onboarding, after the fact.

    Separate from /onboarding/save on purpose. save is the once-only path that
    ingests a resume and burns a Groq call; this one touches nothing but the
    details blob, so it costs nothing and can be used as often as a real person
    would plausibly need it. A career stage changes; the answer given in month
    one should not be permanent.

    Every field goes through the same clean_onboarding allowlist as save. This is
    the SECOND write path into `users.onboarding`, and that blob is flattened
    into two LLM prompts — a route that skipped the schema would reopen exactly
    the injection hole save was fixed for.

    A partial submission is a partial update: only the fields actually present in
    the request are touched. That way the profile page can PATCH a single
    dropdown without having to re-post the resume-era answers, and a field the
    form doesn't render yet can't be wiped by omission.
    """
    user = current_user()

    # Only meaningful once the first pass exists — before that, /onboarding/save
    # is the route, and it also needs the resume this one deliberately ignores.
    if not user.onboarding_complete:
        return jsonify({
            "success": False,
            "message": "Finish onboarding first.",
        }), 400

    updates = clean_onboarding(request.form)
    if not updates:
        return jsonify({
            "success": False,
            "message": "Nothing to update.",
        }), 400

    previous = user.onboarding or {}
    # Replace the whole column rather than mutating in place: JSON columns don't
    # track in-place mutation, so `user.onboarding[k] = v` would be silently
    # dropped at commit unless explicitly flagged (see models.set_pending).
    user.onboarding = {**previous, **updates}

    # An in-progress question set was generated for the OLD answers and is cached
    # on the persona, so without this a user who corrects their stage keeps being
    # asked the questions their wrong stage produced — which makes the correction
    # look like it did nothing. Clearing the cache lets the next get-questions
    # regenerate at the right band.
    #
    # Only for a persona that has not been built yet. Once summary is set the
    # answers are already scored against those exact questions, and refresh
    # eligibility owns when a new set gets generated — clearing here would burn
    # a Groq call the user did not ask for and desynchronise the stored
    # raw_responses from the questions they were written against.
    reroutes = {"experience", "goal"}
    changed = {k for k in updates if previous.get(k) != updates[k]}
    persona = user.persona
    if changed & reroutes and persona is not None and not persona.summary:
        persona.dimension_questions = None

    db.session.commit()

    return jsonify({"success": True, "onboarding": user.onboarding}), 200


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
        result = generate_core_persona(persona.raw_responses, user.onboarding, persona.resume_data,
                                       session_evidence, first_build=not persona.summary)
    except PersonaGenerationError as e:
        # Don't set persona.summary — leaving it unset means the next call retries
        # cleanly instead of returning a permanently-cached fallback.
        return jsonify({"error": str(e)}), 503

    persona.summary = result["summary"]
    persona.dimensions = result["dimensions"]
    # Reset the eligibility baseline — the NEXT refresh needs its own fresh evidence.
    persona.session_count_at_generation = total_sessions
    # The late-resume refresh has now been spent. Clearing it here (rather than at
    # upload) means an upload that is never followed through stays available, and
    # the persona built with the resume can't immediately be rebuilt again for free.
    persona.resume_refresh_pending = False
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
