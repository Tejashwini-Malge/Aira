"""Communication practice module — Teach → Try → Adapt → Refine.

Six tracks students fail at (communication / self-introduction / talking about their
own projects / vocal delivery / leadership stories / technical communication). Each
teaches a short framework — DESIGNED PER USER from their résumé by softskill_agent,
not drawn from a fixed bank — then practices it one beat at a time with ADAPTIVE
difficulty: after each answer Aira judges it and the next question gets harder
(strong) or easier (weak), on a 1-5 ladder. The hidden persona steers everything and
is never sent by the client nor returned raw — same discipline as the mock interview.

The framework a student practices against is generated once at /comm/start and then
persisted in their server-side session, so /comm/next and /comm/evaluate grade the
EXACT framework they were taught — the client never sends it.

Reuses the quiz module's Groq helper, private persona context, and resource builder.
"""
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

from models import db, SpeakingSession, get_pending, set_pending, clear_pending
from auth import current_user, login_required
from rate_limiter import limiter
from ai_quiz_bp import _call_groq, _persona_brief, _build_resources
from softskill_agent import (
    is_track, track_label, all_tracks, generate_lesson, generate_opening,
    fallback_framework, SoftSkillGenerationError,
)

comm_bp = Blueprint("comm_bp", __name__)

# Difficulty ladder
MIN_LEVEL, MAX_LEVEL, START_LEVEL = 1, 5, 2

# Free-plan gate: NOT which tracks you can open — all 6 are always open, so
# picking one never feels like a bet you might regret. The constraint is how
# many real practice REPS you get per day, like Duolingo hearts, decided fresh
# each day and never held against you for picking "wrong" tracks yesterday.
MAX_FREE_SESSIONS_PER_DAY = 2


def _sessions_today(user):
    """Completed practice sessions (real reps, not abandoned attempts) so far today."""
    today = datetime.now(timezone.utc).date()
    return sum(1 for s in user.speaking_sessions if s.created_at and s.created_at.date() == today)


def _resume_data():
    """Structured résumé facts (experience_level, skills, projects) the agent uses
    to adjust the framework — the persona brief carries the same facts as prose, but
    the agent wants experience_level explicitly."""
    persona = current_user().persona
    return (persona.resume_data or {}) if persona else {}


def _projects():
    """The user's real resume projects (for the project track)."""
    persona = current_user().persona
    rd = (persona.resume_data or {}) if persona else {}
    return rd.get("projects") or []


def _project_ctx(track, project_id):
    """Returns (label, context_line) for the chosen project, or defaults."""
    if track != "project":
        return track_label(track), ""
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


# The model judges the ANSWER; the arithmetic on top of that judgement is ours.
# Asking it for the new level too meant paying tokens for a sum we re-derived and
# re-clamped in Python anyway — and left it free to return a level that didn't
# match the quality it had just reported.
_LEVEL_STEP = {"strong": 1, "ok": 0, "weak": -1}


def _next_level(cur_level, quality):
    # quality comes straight out of model JSON, so it is not guaranteed to be a
    # string — anything unrecognised leaves the difficulty where it is.
    key = quality.strip().lower() if isinstance(quality, str) else ""
    return _clamp(cur_level + _LEVEL_STEP.get(key, 0))


# A spoken answer to one interview question is a few hundred words. Anything past
# this is not a person talking — it is a broken dictation loop, and letting it reach
# the model silently costs the student their whole session (the prompt blows the
# context window, _call_groq returns nothing, and they get the 5/5/5/5 fallback
# scores as if they had actually been graded). Clamp on ingest instead.
MAX_ANSWER_CHARS = 3000
_OVERLAP_WINDOW = 30   # words; bounds the cost of the adjacent-repeat scan
_SIGNATURE_WORDS = 8       # words that identify "the answer started over here"
_RESTART_SEARCH_WORDS = 200  # how far in to look for a restart point
_MAX_RESTART_DEPTH = 4       # restarts-within-restarts before we stop digging
_MAX_RAW_CHARS = 200_000     # refuse to even scan beyond this; it is not speech
_DEDUPE_FIRST_WORDS = 1500   # above this, collapse before de-duplicating (see below)


def _collapse_restarts(words, depth=0):
    """Keep the fullest revision when an answer restarts and re-says itself.

    A dictation loop re-delivers the same sentence over and over, one word longer each
    time, so the answer becomes a chain of growing prefixes. Every one of those
    restarts opens with the same run of words, so indexing the signature-length
    word-grams by position finds the restart point in a single pass: split there and
    keep the longest section, since each section is a prefix of the same sentence and
    the longest is the complete thing they said. A student can trail off and restart
    again inside that section, so the survivor is re-examined for a restart of its own.
    """
    if depth > _MAX_RESTART_DEPTH or len(words) < _SIGNATURE_WORDS * 2:
        return words

    lowered = [w.lower() for w in words]
    positions = {}
    for i in range(len(words) - _SIGNATURE_WORDS + 1):
        positions.setdefault(tuple(lowered[i:i + _SIGNATURE_WORDS]), []).append(i)

    # Earliest point the answer starts over. Anything before it was said once.
    for p in range(min(len(words) - _SIGNATURE_WORDS, _RESTART_SEARCH_WORDS)):
        starts = positions.get(tuple(lowered[p:p + _SIGNATURE_WORDS]), ())
        starts = [i for i in starts if i >= p]
        if len(starts) < 2:
            continue
        bounds = starts + [len(words)]
        longest = max((words[a:b] for a, b in zip(bounds, bounds[1:])), key=len)
        return words[:p] + _collapse_restarts(longest, depth + 1)
    return words


def _dedupe_adjacent(words):
    """Drop any run of words that immediately repeats the run just kept.

    Unwinds short stutters and the tail of a dictation loop ("the interest for" +
    "for admission" -> "the interest for admission"). Ordinary speech only ever loses
    a genuine repetition, which is what we want in a transcript anyway.
    """
    kept, lowered_kept, i = [], [], 0
    while i < len(words):
        limit = min(len(kept), len(words) - i, _OVERLAP_WINDOW)
        tail = [w.lower() for w in words[i:i + limit]]
        overlap = 0
        for k in range(limit, 0, -1):
            if lowered_kept[len(kept) - k:] == tail[:k]:
                overlap = k
                break
        if overlap:
            i += overlap          # already have these words; skip the repeat
        else:
            kept.append(words[i])
            lowered_kept.append(words[i].lower())
            i += 1
    return kept


def _sanitize_answer(text):
    """Recover a dictated answer from a broken speech loop, then hard-cap its length.

    De-duplicating before collapsing recovers slightly more of what was said, because
    the restart points line up better once the stutters between them are gone. But
    that pass is quadratic in the overlap window, so on a badly runaway answer it is
    far too slow for a request path — there we collapse first to get the word count
    down, and accept losing a trailing clause of already-mangled speech.
    """
    text = (text or "").strip()
    if not text:
        return ""

    words = text[:_MAX_RAW_CHARS].split()
    if len(words) > _DEDUPE_FIRST_WORDS:
        words = _collapse_restarts(words)
    words = _collapse_restarts(_dedupe_adjacent(words))

    cleaned = " ".join(words)
    if len(cleaned) > MAX_ANSWER_CHARS:
        # Still oversized after collapsing — cut at a word boundary so the prompt
        # stays well inside the context window no matter what the client sent.
        cleaned = cleaned[:MAX_ANSWER_CHARS].rsplit(" ", 1)[0]
    return cleaned


# ---------------------------------------------------------------------------
# Setup — teach the framework + a persona-personalised example
# ---------------------------------------------------------------------------

@comm_bp.route("/comm/setup", methods=["GET"])
@login_required
@limiter.limit("30 per hour")
def comm_setup():
    # Browsing a framework is always free — every track is always open. Only
    # actually starting a practice attempt (/comm/start) spends a day's rep.
    track = (request.args.get("track") or "communication").lower()
    if not is_track(track):
        return jsonify({"error": "Unknown track."}), 400
    user = current_user()

    # The agent designs the framework AND a model example from their résumé in one call.
    brief, _, _ = _persona_brief()
    try:
        lesson = generate_lesson(track, brief, _resume_data())
        fw, example = lesson["framework"], lesson["example"]
    except SoftSkillGenerationError:
        # Groq unreachable — degrade to the skeleton so the teach screen still renders.
        fw = fallback_framework(track)
        example = "Open with your single main point, keep it specific with one real example, and close on why it matters."

    resp = {
        "track": track,
        "framework": {"name": fw["name"], "why": fw["why"], "steps": fw["steps"]},
        "example": example,
        "sessionsUsedToday": _sessions_today(user),
        "maxFreeSessionsPerDay": MAX_FREE_SESSIONS_PER_DAY,
    }
    if track == "project":
        resp["projects"] = [
            {"id": i, "name": p.get("name", f"Project {i+1}"), "what_they_did": p.get("what_they_did", "")}
            for i, p in enumerate(_projects())
        ]
    return jsonify(resp), 200


# ---------------------------------------------------------------------------
# Quota — how many of today's free reps are left, before committing to a track
# ---------------------------------------------------------------------------

@comm_bp.route("/comm/quota", methods=["GET"])
@login_required
def comm_quota():
    used = _sessions_today(current_user())
    return jsonify({
        "sessionsUsedToday": used,
        "maxFreeSessionsPerDay": MAX_FREE_SESSIONS_PER_DAY,
        "remainingToday": max(0, MAX_FREE_SESSIONS_PER_DAY - used),
    }), 200


# ---------------------------------------------------------------------------
# Start — the first question (difficulty 2)
# ---------------------------------------------------------------------------

@comm_bp.route("/comm/start", methods=["POST"])
@login_required
@limiter.limit("15 per hour")
def comm_start():
    data = request.get_json() or {}
    track = (data.get("track") or "communication").lower()
    if not is_track(track):
        return jsonify({"error": "Unknown track."}), 400

    user = current_user()
    used_today = _sessions_today(user)
    if used_today >= MAX_FREE_SESSIONS_PER_DAY:
        return jsonify({
            "error": f"You've used today's {MAX_FREE_SESSIONS_PER_DAY} free practice sessions — come back tomorrow for more.",
            "sessionsUsedToday": used_today,
            "maxFreeSessionsPerDay": MAX_FREE_SESSIONS_PER_DAY,
        }), 403

    label, proj_ctx = _project_ctx(track, data.get("project_id"))
    brief, _, _ = _persona_brief()

    # The agent designs the résumé-fit framework AND the opening question in one call.
    # Its framework becomes the session's source of truth — persisted below so every
    # later beat and the final evaluation grade the same structure the student was given.
    try:
        opening = generate_opening(track, brief, _resume_data(), proj_ctx)
        fw = opening["framework"]
        beat = _beat_from(
            {"prompt": opening["first_question"], "hint": opening["hint"]},
            1, START_LEVEL, fw,
        )
    except SoftSkillGenerationError:
        fw = fallback_framework(track)
        beat = _beat_from({}, 1, START_LEVEL, fw)

    # The session's framework AND prompts live server-side from here: /comm/next and
    # /comm/evaluate extend and grade THIS stored state — the client only sends answers.
    set_pending(current_user().id, "comm", {
        "track": track,
        "label": label,
        "project_id": data.get("project_id"),
        "framework": fw,
        "level": START_LEVEL,
        "history": [dict(beat, answer=None)],
    })

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
@limiter.limit("60 per hour")
def comm_next():
    data = request.get_json() or {}
    raw_answer = (data.get("answer") or "").strip()
    answer = _sanitize_answer(raw_answer)
    if len(raw_answer) > len(answer) * 2 + 200:
        # Loud on purpose: the last time a dictation loop reached the model it cost a
        # student their whole session and left no trace anywhere but the transcript.
        print(f"Dictation loop cleaned: {len(raw_answer)} -> {len(answer)} chars")

    # Only the stored session can advance — the client sends just its latest answer,
    # and the prompts/difficulty come from what WE actually asked.
    user_id = current_user().id
    state = get_pending(user_id, "comm")
    if not state or not state.get("history"):
        return jsonify({"error": "No active session. Start one with /comm/start first."}), 400

    track = state["track"]
    fw = state.get("framework") or fallback_framework(track)
    history = state["history"]
    cur_level = _clamp(state.get("level") or START_LEVEL)
    label, proj_ctx = _project_ctx(track, state.get("project_id"))

    # Record their answer against the beat we actually asked.
    history[-1]["answer"] = answer

    answered = len(history)
    if answered >= fw["total"]:
        set_pending(user_id, "comm", state)  # keep the finished transcript for /comm/evaluate
        return jsonify({"done": True}), 200

    brief, _, _ = _persona_brief()
    transcript = "\n".join(
        f"Q{i+1} (level {h.get('difficulty','?')}): {h.get('prompt','')}\n  Their answer: {(h.get('answer') or '').strip() or '(blank)'}"
        for i, h in enumerate(history)
    )
    next_step = fw["steps"][min(answered, len(fw["steps"]) - 1)]

    prompt = f"""You are an interactive speaking coach running a "{label}" exercise
(framework: {fw['name']}). Goal: {fw['focus']}.

PRIVATE PROFILE (for your eyes only, do not quote):
{brief}
{proj_ctx}

CONVERSATION SO FAR:
{transcript}

The current difficulty level is {cur_level} (of 5). Do TWO things:
1. Judge their MOST RECENT answer: "strong" (specific, clear, confident), "ok" (some
   substance but generic/safe), or "weak" (vague, very short, or blank).
2. Ask the NEXT question, pitched one notch harder than the last if their answer was
   strong, the same if ok, and gentler if weak. Harder = more depth, edge cases, or
   follow-up pressure. This is question {answered+1} of {fw['total']};
   it should move the framework forward (next focus: {next_step['label']} — {next_step['hint']}).
   Phrase it naturally, plain English. If their last answer was weak, a one-line supportive
   reaction; if strong, acknowledge it briefly.

Return ONLY JSON:
{{"quality":"strong|ok|weak","reaction":"one short line to them",
  "prompt":"the next question","hint":"one short tip"}}"""

    gen = _call_groq(prompt, max_tokens=400, temperature=0.4, label="comm_next")
    gen = gen if isinstance(gen, dict) else {}
    new_level = _next_level(cur_level, gen.get("quality"))
    beat = _beat_from(gen, answered + 1, new_level, fw)

    # Persist the new beat so the next round (and the final evaluation) grades what
    # was really asked, at the difficulty we really set.
    history.append(dict(beat, answer=None))
    state["level"] = new_level
    set_pending(user_id, "comm", state)

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
@limiter.limit("15 per hour")
def comm_evaluate():
    data = request.get_json() or {}
    # Delivery signals (pace/fillers/words) are measured in the browser, so they stay
    # client-sent — but the transcript itself is only ever the one WE accumulated.
    metrics = data.get("metrics") or {}

    user_id = current_user().id
    state = get_pending(user_id, "comm")
    if not state or not state.get("history"):
        return jsonify({"error": "No active session to evaluate. Start one with /comm/start first."}), 400

    history = state["history"]
    # If the client skipped the final /comm/next round-trip, accept its last answer
    # here — but only slotted into the beat we actually asked.
    final_answer = _sanitize_answer(data.get("answer"))
    if final_answer and not (history[-1].get("answer") or "").strip():
        history[-1]["answer"] = final_answer

    track = state["track"]
    fw = state.get("framework") or fallback_framework(track)
    label = (state.get("label") or track_label(track)).strip()
    qa = [{"prompt": h.get("prompt"), "answer": h.get("answer"), "difficulty": h.get("difficulty")} for h in history]
    brief, _, _ = _persona_brief(for_eval=True)

    # Sanitize again on the way out, not just on ingest: a session that was already
    # in flight when this deployed still has raw answers sitting in pending state.
    transcript = "\n\n".join(
        f"Q{i+1}: {item.get('prompt','')}\nTheir answer: {_sanitize_answer(item.get('answer')) or '(left blank)'}"
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

    result = _call_groq(prompt, max_tokens=1100, label="comm_evaluate")
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
        user_id=user_id,
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

    # This session is spent — a fresh /comm/start is needed for the next one.
    clear_pending(user_id, "comm")

    return jsonify({"feedback": result}), 200


# ---------------------------------------------------------------------------
# Redo — the attempt-1 vs attempt-2 delta for the weakest beat
# ---------------------------------------------------------------------------

@comm_bp.route("/comm/redo", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
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
    gen = _call_groq(prompt, max_tokens=200, temperature=0.4, label="comm_redo")
    gen = gen if isinstance(gen, dict) else {}
    return jsonify({
        "improved": (gen.get("improved") or "Good effort — keep practising that beat.").strip(),
        "verdict": gen.get("verdict") or "About the same",
    }), 200
