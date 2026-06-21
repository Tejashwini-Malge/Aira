"""Communication practice module — Teach → Try → Adapt → Refine.

Three tracks students fail at (communication / self-introduction / talking about their
own projects). Each teaches a short framework, then practices it one beat at a time with
ADAPTIVE difficulty: after each answer Aira judges it and the next question gets harder
(strong) or easier (weak), on a 1-5 ladder. The hidden persona steers everything and is
never sent by the client nor returned raw — same discipline as the mock interview.

Reuses the quiz module's Groq helper, private persona context, and resource builder.
"""
from flask import Blueprint, request, jsonify

from models import db, SpeakingSession
from auth import current_user, login_required
from ai_quiz_bp import _call_groq, _persona_brief, _build_resources

comm_bp = Blueprint("comm_bp", __name__)

# Difficulty ladder
MIN_LEVEL, MAX_LEVEL, START_LEVEL = 1, 5, 2

FRAMEWORKS = {
    "communication": {
        "label": "Communication practice",
        "name": "PREP",
        "why": "It keeps you clear and short: make your Point, give a Reason, an Example, then restate the Point.",
        "steps": [
            {"label": "Point", "hint": "say your main message in one line"},
            {"label": "Reason", "hint": "why is it true?"},
            {"label": "Example", "hint": "one concrete example"},
            {"label": "Point", "hint": "restate it cleanly"},
        ],
        "total": 5,
        "focus": "speaking clearly and to the point on any prompt",
    },
    "intro": {
        "label": "Self-introduction",
        "name": "Present · Past · Future",
        "why": "A simple order for 'tell me about yourself': who you are now, the background that got you here, and where you're headed.",
        "steps": [
            {"label": "Present", "hint": "who you are right now"},
            {"label": "Past", "hint": "the background that's relevant"},
            {"label": "Future", "hint": "what you're aiming for"},
        ],
        "total": 4,
        "focus": "introducing yourself the way an interviewer wants to hear it",
    },
    "project": {
        "label": "Talk about your project",
        "name": "What · Why · How · Impact · Your role",
        "why": "Break a project into answerable parts so you never freeze: what it does, why you built it that way, how, the impact, and your own part in it.",
        "steps": [
            {"label": "What", "hint": "what does it do, in one sentence"},
            {"label": "Why", "hint": "why this approach"},
            {"label": "How", "hint": "how you built it"},
            {"label": "Impact", "hint": "the result or what you learned"},
            {"label": "Your role", "hint": "your specific contribution"},
        ],
        "total": 5,
        "focus": "explaining your own project clearly and owning your contribution",
    },
}


def _projects():
    """The user's real resume projects (for the project track)."""
    persona = current_user().persona
    rd = (persona.resume_data or {}) if persona else {}
    return rd.get("projects") or []


def _project_ctx(track, project_id):
    """Returns (label, context_line) for the chosen project, or defaults."""
    if track != "project":
        return FRAMEWORKS[track]["label"], ""
    projects = _projects()
    try:
        p = projects[int(project_id)]
        name = p.get("name", "your project")
        return f"Project: {name}", f"The project they are talking about: {name} — {p.get('what_they_did','')}"
    except (TypeError, ValueError, IndexError):
        return "Your project", ""


def _clamp(n):
    try:
        return max(MIN_LEVEL, min(MAX_LEVEL, int(n)))
    except (TypeError, ValueError):
        return START_LEVEL


def _diff_word(level):
    return {1: "very easy", 2: "easy", 3: "moderate", 4: "hard", 5: "very hard"}.get(level, "moderate")


# ---------------------------------------------------------------------------
# Setup — teach the framework + a persona-personalised example
# ---------------------------------------------------------------------------

@comm_bp.route("/comm/setup", methods=["GET"])
@login_required
def comm_setup():
    track = (request.args.get("track") or "communication").lower()
    if track not in FRAMEWORKS:
        return jsonify({"error": "Unknown track."}), 400
    fw = FRAMEWORKS[track]
    brief, _, target_role = _persona_brief()

    prompt = f"""You are a friendly speaking coach. Write ONE short model example a student
could use, for the "{fw['label']}" exercise using the {fw['name']} framework
({', '.join(s['label'] for s in fw['steps'])}). Make it fit THIS person without mentioning
their private profile. Keep it 2-3 short sentences, simple plain English.

PRIVATE PROFILE (for your eyes only, do not quote):
{brief}

Return ONLY JSON: {{"example": "..."}}"""
    data = _call_groq(prompt, max_tokens=300, temperature=0.5)
    example = (data or {}).get("example") if isinstance(data, dict) else None

    resp = {
        "track": track,
        "framework": {"name": fw["name"], "why": fw["why"], "steps": fw["steps"]},
        "example": example or "Lead with what you do today, keep it specific, and end on where you're headed.",
    }
    if track == "project":
        resp["projects"] = [
            {"id": i, "name": p.get("name", f"Project {i+1}"), "what_they_did": p.get("what_they_did", "")}
            for i, p in enumerate(_projects())
        ]
    return jsonify(resp), 200


# ---------------------------------------------------------------------------
# Start — the first question (difficulty 2)
# ---------------------------------------------------------------------------

@comm_bp.route("/comm/start", methods=["POST"])
@login_required
def comm_start():
    data = request.get_json() or {}
    track = (data.get("track") or "communication").lower()
    if track not in FRAMEWORKS:
        return jsonify({"error": "Unknown track."}), 400
    fw = FRAMEWORKS[track]
    label, proj_ctx = _project_ctx(track, data.get("project_id"))
    brief, _, _ = _persona_brief()

    prompt = f"""You are an interactive speaking coach running a "{fw['label']}" exercise with
a student, using the {fw['name']} framework. Ask the FIRST question only. It should match
difficulty level {START_LEVEL} of 5 ({_diff_word(START_LEVEL)}). Goal of the exercise:
{fw['focus']}. Phrase it naturally; do not mention any private profile.

PRIVATE PROFILE (for your eyes only, do not quote):
{brief}
{proj_ctx}

Return ONLY JSON: {{"prompt": "the question", "hint": "one short tip"}}"""
    gen = _call_groq(prompt, max_tokens=300, temperature=0.5)
    beat = _beat_from(gen, 1, START_LEVEL, fw)
    return jsonify({"track": track, "label": label, "beat": beat, "total": fw["total"]}), 200


def _beat_from(gen, beat_id, level, fw):
    if isinstance(gen, dict) and gen.get("prompt"):
        return {"id": beat_id, "prompt": gen["prompt"].strip(),
                "hint": (gen.get("hint") or "").strip(), "difficulty": level}
    step = fw["steps"][min(beat_id - 1, len(fw["steps"]) - 1)]
    return {"id": beat_id, "prompt": f"{step['label']}: {step['hint']}.",
            "hint": step["hint"], "difficulty": level}


# ---------------------------------------------------------------------------
# Next — the adaptive engine: judge the last answer, adjust difficulty, ask next
# ---------------------------------------------------------------------------

@comm_bp.route("/comm/next", methods=["POST"])
@login_required
def comm_next():
    data = request.get_json() or {}
    track = (data.get("track") or "communication").lower()
    if track not in FRAMEWORKS:
        return jsonify({"error": "Unknown track."}), 400
    fw = FRAMEWORKS[track]
    history = data.get("history") or []
    cur_level = _clamp(data.get("level") or START_LEVEL)
    label, proj_ctx = _project_ctx(track, data.get("project_id"))

    answered = len(history)
    if answered >= fw["total"]:
        return jsonify({"done": True}), 200

    brief, _, _ = _persona_brief()
    last = history[-1] if history else {}
    transcript = "\n".join(
        f"Q{i+1} (level {h.get('difficulty','?')}): {h.get('prompt','')}\n  Their answer: {(h.get('answer') or '').strip() or '(blank)'}"
        for i, h in enumerate(history)
    )
    next_step = fw["steps"][min(answered, len(fw["steps"]) - 1)]

    prompt = f"""You are an interactive speaking coach running a "{fw['label']}" exercise
(framework: {fw['name']}). Goal: {fw['focus']}.

PRIVATE PROFILE (for your eyes only, do not quote):
{brief}
{proj_ctx}

CONVERSATION SO FAR:
{transcript}

The current difficulty level is {cur_level} (of 5). Do THREE things:
1. Judge their MOST RECENT answer: "strong" (specific, clear, confident), "ok" (some
   substance but generic/safe), or "weak" (vague, very short, or blank).
2. Set the next difficulty: strong -> {cur_level}+1, ok -> {cur_level}, weak -> {cur_level}-1
   (clamp between 1 and 5). Harder = more depth, edge cases, or follow-up pressure.
3. Ask the NEXT question at that new level. This is question {answered+1} of {fw['total']};
   it should move the framework forward (next focus: {next_step['label']} — {next_step['hint']}).
   Phrase it naturally, plain English. If their last answer was weak, a one-line supportive
   reaction; if strong, acknowledge it briefly.

Return ONLY JSON:
{{"quality":"strong|ok|weak","level":<new level 1-5>,"reaction":"one short line to them",
  "prompt":"the next question","hint":"one short tip"}}"""

    gen = _call_groq(prompt, max_tokens=400, temperature=0.4)
    gen = gen if isinstance(gen, dict) else {}
    new_level = _clamp(gen.get("level") or cur_level)
    beat = _beat_from(gen, answered + 1, new_level, fw)
    return jsonify({
        "done": False,
        "reaction": (gen.get("reaction") or "").strip(),
        "level": new_level,
        "beat": beat,
    }), 200


# ---------------------------------------------------------------------------
# Evaluate — one honest verdict for the whole session, grounded in client metrics
# ---------------------------------------------------------------------------

@comm_bp.route("/comm/evaluate", methods=["POST"])
@login_required
def comm_evaluate():
    data = request.get_json() or {}
    track = (data.get("track") or "communication").lower()
    fw = FRAMEWORKS.get(track, FRAMEWORKS["communication"])
    label = (data.get("label") or fw["label"]).strip()
    qa = data.get("qa") or []
    metrics = data.get("metrics") or {}
    brief, _, _ = _persona_brief(for_eval=True)

    transcript = "\n\n".join(
        f"Q{i+1}: {item.get('prompt','')}\nTheir answer: {(item.get('answer') or '').strip() or '(left blank)'}"
        for i, item in enumerate(qa)
    )
    metrics_line = (f"Measured delivery signals — speaking pace: {metrics.get('wpm','?')} words/min, "
                    f"filler words used: {metrics.get('fillers','?')}, total words: {metrics.get('words','?')}.")

    prompt = f"""You are an honest, supportive speaking coach. Evaluate how this student did
in the "{label}" exercise (framework {fw['name']}; goal: {fw['focus']}). Use their private
profile only to set tone — never reveal or quote it.

PRIVATE PROFILE (for your eyes only):
{brief}

TRANSCRIPT (the questions and what they actually said):
{transcript}

{metrics_line}

Evaluate THIS session, letting feedback flow from what they actually said. You judge
WORDING, STRUCTURE, CLARITY and OWNERSHIP from the text; base the fluency/confidence
numbers on the measured signals above (you cannot hear tone, so do not invent it).
"weak_areas" and resources must be the SPECIFIC delivery habits they showed (e.g. "rambling
without a clear point", "too many filler words", "not owning your contribution"), not
generic personality traits. Plain, simple English.

Return ONLY JSON:
{{
  "feedback": "2-3 honest sentences about how this session went, citing what they said",
  "scores": {{"fluency": 0-10, "clarity": 0-10, "confidence": 0-10, "structure": 0-10}},
  "beat_scores": [{{"beat": "the question topic", "verdict": "Strong|Okay|Weak", "note": "why"}}],
  "weakest_beat": {{"id": <question number>, "label": "short name", "why": "what was missing"}},
  "redo_prompt": "one question to retry their weakest beat",
  "weak_areas": ["specific delivery habit to fix", "another"],
  "suggestions": ["actionable tip tied to what they said", "another", "another"],
  "resources": [
    {{"area": "the specific habit", "items": [
      {{"title": "what to look for", "type": "video", "query": "search terms"}},
      {{"title": "...", "type": "blog", "query": "..."}}
    ]}}
  ]
}}"""

    result = _call_groq(prompt, max_tokens=1100)
    if not isinstance(result, dict) or not result.get("feedback"):
        result = {
            "feedback": "We couldn't fully evaluate this session. Try again.",
            "scores": {"fluency": 5, "clarity": 5, "confidence": 5, "structure": 5},
            "beat_scores": [],
            "weakest_beat": {"id": 1, "label": label, "why": "needs more detail"},
            "redo_prompt": "Try your first answer again, more specifically.",
            "weak_areas": ["being more specific"],
            "suggestions": ["Make one clear point", "Give a concrete example"],
            "resources": [{"area": "clear speaking", "items": [
                {"title": "Speak clearly and concisely", "type": "video", "query": "how to speak clearly and concisely"},
            ]}],
        }

    result["resources"] = _build_resources(result.get("resources"))
    scores = result.get("scores") or {}

    def _s(k):
        try:
            return int(scores.get(k))
        except (TypeError, ValueError):
            return None

    # Persist server-side (the client can't forge scores) — mirrors /quiz/evaluate.
    record = SpeakingSession(
        user_id=current_user().id,
        mode="practice",
        track=track,
        fluency=_s("fluency"),
        clarity=_s("clarity"),
        confidence=_s("confidence"),
        structure=_s("structure"),
        summary=result.get("feedback"),
        weak_areas=result.get("weak_areas") or [],
        suggestions=result.get("suggestions") or [],
        transcript=qa,
    )
    db.session.add(record)
    db.session.commit()

    return jsonify({"feedback": result}), 200


# ---------------------------------------------------------------------------
# Redo — the attempt-1 vs attempt-2 delta for the weakest beat
# ---------------------------------------------------------------------------

@comm_bp.route("/comm/redo", methods=["POST"])
@login_required
def comm_redo():
    data = request.get_json() or {}
    beat = (data.get("beat") or "your answer").strip()
    a1 = (data.get("attempt1") or "").strip()
    a2 = (data.get("attempt2") or "").strip()
    if not a2:
        return jsonify({"error": "No second attempt provided."}), 400

    prompt = f"""A student retried this prompt: "{beat}".
First attempt: "{a1 or '(blank)'}"
Second attempt: "{a2}"
In one short, encouraging sentence say what improved (or what still needs work). Plain English.
Return ONLY JSON: {{"improved": "one sentence", "verdict": "Better|About the same|Needs more"}}"""
    gen = _call_groq(prompt, max_tokens=200, temperature=0.4)
    gen = gen if isinstance(gen, dict) else {}
    return jsonify({
        "improved": (gen.get("improved") or "Good effort — keep practising that beat.").strip(),
        "verdict": gen.get("verdict") or "About the same",
    }), 200
