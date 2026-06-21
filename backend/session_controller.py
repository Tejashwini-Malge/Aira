from flask import Blueprint, request, jsonify
import json
import os
import re
import random
import requests
from pathlib import Path
from dotenv import load_dotenv

from models import db, Persona
from auth import current_user, login_required
from resume_agent import extract_text, parse_resume, build_resume_questions, ResumeError

# Load .env next to this file regardless of the launch working directory.
load_dotenv(Path(__file__).resolve().parent / ".env")

session_bp = Blueprint("session_bp", __name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = "llama-3.3-70b-versatile"

_BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "question_bank.json")
with open(_BANK_PATH) as _f:
    _QUESTION_BANK = json.load(_f)

# Fixed order so dimension levels always appear in this sequence.
DIMENSION_ORDER = [
    "work_culture_preferences",
    "teamwork_style",
    "leadership_tendencies",
    "decision_making_approach",
    "problem_solving_behavior",
    "professional_values",
    "career_goals",
    "communication_style",
]


def _fixed_by_dimension():
    """{dimension: [variants]} and the list of reflection questions."""
    by_dim = {}
    reflections = []
    for q in _QUESTION_BANK:
        if q["type"] == "reflection":
            reflections.append(q)
        else:
            by_dim.setdefault(q["dimension"], []).append(q)
    return by_dim, reflections


# How many fixed dimension scenarios go into the blended quiz. The resume agent
# contributes 4 more (2 technical + 2 HR) and we close with 1 open-ended reflection,
# for a 10-question quiz the user experiences as one seamless set.
FIXED_COUNT = 5


def _assemble_questions(persona):
    """The hidden blend: 5 fixed + 4 resume-grounded + 1 open-ended reflection.

    The 5 fixed scenarios are chosen to cover dimensions the resume questions DON'T,
    so all 8 dimensions get signal between the two sources. Falls back to the plain
    fixed-bank sample if no resume analysis exists yet.
    """
    by_dim, reflections = _fixed_by_dimension()

    resume_questions = []
    if persona is not None and persona.resume_data:
        resume_questions = build_resume_questions(persona.resume_data)

    # Without resume data we can't build the intended blend — fall back to one
    # scenario per dimension plus the reflections (legacy behaviour).
    if not resume_questions:
        selected = [random.choice(by_dim[d]) for d in DIMENSION_ORDER if by_dim.get(d)]
        selected.extend(reflections[:1] or reflections)
        return selected

    covered = {q["dimension"] for q in resume_questions}
    # Prefer fixed questions for dimensions the resume didn't already probe.
    ordered_dims = [d for d in DIMENSION_ORDER if d not in covered] + \
                   [d for d in DIMENSION_ORDER if d in covered]
    fixed = []
    for dim in ordered_dims:
        if len(fixed) >= FIXED_COUNT:
            break
        if by_dim.get(dim):
            fixed.append(random.choice(by_dim[dim]))

    # Blend fixed + resume so the resume questions aren't visibly clustered, then
    # close with a single open-ended reflection.
    blended = fixed + resume_questions
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


class PersonaGenerationError(Exception):
    """Raised on a genuine LLM/network failure so the caller can return a retryable
    error instead of caching a generic fallback persona."""


def _format_context(onboarding, resume_data):
    """Concrete facts the assessment must be grounded in: who they are, what they
    want, and what their resume actually shows."""
    lines = ["WHAT THEY TOLD US ABOUT THEMSELVES:"]
    onboarding = onboarding or {}
    if onboarding:
        for k, v in onboarding.items():
            if v:
                lines.append(f"  - {k.replace('_', ' ')}: {v}")
    else:
        lines.append("  - (none provided)")

    resume_data = resume_data or {}
    lines.append("\nWHAT THEIR RESUME SHOWS:")
    if resume_data:
        skills = ", ".join(resume_data.get("skills", [])[:10]) or "none listed"
        lines.append(f"  - experience level: {resume_data.get('experience_level', 'unknown')}")
        lines.append(f"  - skills: {skills}")
        for p in (resume_data.get("projects") or [])[:4]:
            lines.append(f"  - project: {p.get('name', '')} — {p.get('what_they_did', '')}")
        gaps = ", ".join(resume_data.get("likely_gaps", [])[:5])
        if gaps:
            lines.append(f"  - possible gaps: {gaps}")
    else:
        lines.append("  - (no resume on file)")
    return "\n".join(lines)


def _answer_quality_hint(r):
    """A neutral observation about how much substance an answer carries, so the model
    can't mistake an empty/lazy answer for a strong one."""
    qtype = r.get("type", "")
    if qtype in ("situational", "tradeoff"):
        return None  # choice questions carry their own signal
    answer = (r.get("answer") or "").strip()
    if not answer:
        return "NOTE: left blank — this is evidence of avoidance or low engagement, not a strong trait."
    words = len(answer.split())
    if words < 8:
        return "NOTE: very short/vague answer — weak evidence, lean toward Developing unless it's clearly substantive."
    if words < 20:
        return "NOTE: brief answer — judge how specific it actually is."
    return None


def _build_llm_prompt(responses, onboarding=None, resume_data=None):
    lines = [
        "You are an experienced career coach. You have just finished a real assessment of "
        "this person and you are writing your honest verdict. You are warm but you do NOT "
        "flatter — your job is to tell them the truth so they can grow.\n",
        "HOW TO SCORE — read each answer and JUDGE ITS QUALITY, do not just note the topic:\n"
        "  Strong     = the answer is specific, thoughtful and shows real depth or self-awareness.\n"
        "  Moderate   = some substance but generic, mixed, or plays it safe.\n"
        "  Developing = vague, very short, avoidant, blank, or (for technical questions) shows\n"
        "               they don't really understand their own project/skill.\n"
        "BE STRICT AND HONEST. A weak, empty, or evasive answer MUST score Developing — never "
        "reward effortless or generic answers. Most people are a genuine MIX across the eight "
        "areas; if everything comes out Strong you are not judging hard enough. The score must "
        "clearly change depending on how well they actually answered.\n",
        "For the technical questions: compare their answer to what their resume claims. If they "
        "cannot explain their own listed project or skill clearly and correctly, that is a red "
        "flag — score the related area lower and say so plainly.\n",
        "WRITE IN SIMPLE, PLAIN ENGLISH, speaking directly to them as 'you'. Short everyday "
        "words, as if the reader is not fluent in English. Never use jargon or personality "
        "labels like 'Analytical Thinker' or 'Strategic'. Sound like a coach who actually paid "
        "attention to what they said.\n",
        _format_context(onboarding, resume_data) + "\n",
        "THEIR ASSESSMENT ANSWERS:\n",
    ]

    for r in responses:
        dim = r.get("dimension") or "none"
        qtype = r.get("type", "")
        qtext = r.get("text", "")

        if qtype in ("situational", "tradeoff"):
            lines.append(f"[area: {dim}] (multiple-choice)")
            lines.append(f"  Q: {qtext}")
            lines.append(f"  They chose: {r.get('selected_text', '(no choice)')}")
            lines.append(f"  What that choice reveals: {r.get('signal', '')}\n")
        elif qtype == "recall":
            answer = (r.get("answer") or "").strip() or "(left blank)"
            lines.append(f"[area: {dim}] (free answer — judge depth and correctness)")
            lines.append(f"  Q: {qtext}")
            lines.append(f"  Their answer: {answer}")
            hint = _answer_quality_hint(r)
            if hint:
                lines.append(f"  {hint}")
            lines.append("")
        elif qtype == "reflection":
            answer = (r.get("answer") or "").strip() or "(left blank)"
            lines.append("[reflection — informs your overall verdict only, not a single area score]")
            lines.append(f"  Q: {qtext}")
            lines.append(f"  Their answer: {answer}\n")

    lines.append(
        "Now write your coach's verdict. Return ONLY this JSON object — no markdown, no extra text.\n"
        "Rules:\n"
        "  - \"summary\": 3-4 short sentences spoken to them ('you ...'). Honest coach read — name "
        "what they genuinely did well AND what was weak, citing real things from their answers, "
        "projects and goal. Simple words.\n"
        "  - each \"note\": ONE short sentence pointing to what THIS person's actual answer showed "
        "for that area — specific to them, not a generic description. This is the proof of your "
        "score, so a Developing note must say what was missing.\n"
        "{\n"
        '  "summary": "...",\n'
        '  "dimensions": {\n'
        '    "work_culture_preferences": {"level": "Strong|Moderate|Developing", "note": "..."},\n'
        '    "teamwork_style": {"level": "Strong|Moderate|Developing", "note": "..."},\n'
        '    "leadership_tendencies": {"level": "Strong|Moderate|Developing", "note": "..."},\n'
        '    "decision_making_approach": {"level": "Strong|Moderate|Developing", "note": "..."},\n'
        '    "problem_solving_behavior": {"level": "Strong|Moderate|Developing", "note": "..."},\n'
        '    "professional_values": {"level": "Strong|Moderate|Developing", "note": "..."},\n'
        '    "career_goals": {"level": "Strong|Moderate|Developing", "note": "..."},\n'
        '    "communication_style": {"level": "Strong|Moderate|Developing", "note": "..."}\n'
        "  }\n"
        "}"
    )
    return "\n".join(lines)


def _run_generation(responses, onboarding=None, resume_data=None):
    """Single LLM call → persona dict with source_id tracking merged in.

    The assessment is grounded in the user's onboarding details and resume so it
    speaks about the actual person, not a generic label.
    """
    source_map = {r["dimension"]: r["id"] for r in responses if r.get("dimension")}

    prompt = _build_llm_prompt(responses, onboarding, resume_data)

    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": prompt}],
                # Low temperature so the same answers score consistently and the
                # rubric is followed rather than improvised.
                "temperature": 0.2,
                "max_tokens": 1100,
                # Force valid JSON so we never fail on stray prose or markdown fences.
                "response_format": {"type": "json_object"},
            },
            timeout=45,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            data = json.loads(match.group(0)) if match else {}
        if not data.get("summary") or not data.get("dimensions"):
            raise ValueError("Incomplete persona JSON from LLM")
    except Exception as e:
        # A genuine LLM/network failure. Completeness is enforced before we get here,
        # so this is never just sparse answers. Surface it as retryable rather than
        # silently persisting a generic persona that would then be cached forever.
        print("Persona generation error:", e)
        raise PersonaGenerationError("Aira couldn't build your persona right now. Please try again.")

    # Attach the question variant ID that informed each dimension, for future traceability.
    for dim, source_id in source_map.items():
        if dim in data.get("dimensions", {}):
            data["dimensions"][dim]["source_id"] = source_id

    return data


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
def save_onboarding():
    """Collect career details + a compulsory resume, then run the resume agent.

    The resume is parsed into structured signal AND the 4 resume-grounded persona
    questions, stored on the Persona for the quiz to blend in later.
    """
    user = current_user()

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
def get_questions():
    persona = current_user().persona
    return jsonify({"questions": _assemble_questions(persona)}), 200


@session_bp.route("/session/save-answers", methods=["POST"])
@login_required
def save_answers():
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

    persona = _get_or_create_persona(current_user())
    persona.raw_responses = responses
    db.session.commit()
    return jsonify({"success": True}), 200


@session_bp.route("/session/generate-persona", methods=["POST"])
@login_required
def generate_persona():
    user = current_user()
    persona = _get_or_create_persona(user)

    # `force` re-runs the assessment from the saved answers even if one already exists.
    # Useful while iterating on the logic, and the basis for re-assessing after sessions.
    force = bool((request.get_json(silent=True) or {}).get("force")) or \
        request.args.get("force") in ("1", "true")

    # Otherwise generate exactly once — return the existing persona on later calls.
    # (summary is the sentinel now that we no longer produce an abstract title.)
    if persona.summary and not force:
        return jsonify({"persona": persona.to_dict()}), 200

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

    try:
        result = _run_generation(persona.raw_responses, user.onboarding, persona.resume_data)
    except PersonaGenerationError as e:
        # Don't set persona.summary — leaving it unset means the next call retries
        # cleanly instead of returning a permanently-cached fallback.
        return jsonify({"error": str(e)}), 503

    persona.summary = result["summary"]
    persona.dimensions = result["dimensions"]
    db.session.commit()

    # The persona is private — never return its content to the client. Confirm only
    # that it was built. It lives server-side and drives the other modules.
    return jsonify({"persona": {"ready": True}}), 200


@session_bp.route("/me/persona", methods=["GET"])
@login_required
def get_my_persona():
    """Existence check only (used for gating). Does not expose the assessment."""
    persona = current_user().persona
    ready = bool(persona and persona.summary)
    return jsonify({"persona": {"ready": True} if ready else None}), 200
