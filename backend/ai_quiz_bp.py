"""Mock interview module — powered by the hidden persona.

Two modes:
  - "role"  : a real interview for the user's target role, drawn from their resume
              projects/skills and aimed at the areas the persona marked weakest.
              No topic needed — Aira already knows them.
  - "topic" : the user names a subject; questions and evaluation are still shaped by
              their persona.

The persona is PRIVATE. We never accept it from the client and never send its raw
content back — it is read server-side from the database and only used to steer the
prompts. The user just experiences questions that happen to fit them.
"""
from flask import Blueprint, request, jsonify
from datetime import datetime
import urllib.parse

from models import db, QuizResult, get_pending, set_pending, clear_pending
from auth import current_user, login_required
from rate_limiter import limiter
from groq_client import groq_json, GroqError

quiz_bp = Blueprint("quiz_bp", __name__)

_DIM_LABELS = {
    "work_culture_preferences": "work culture fit",
    "teamwork_style": "teamwork",
    "leadership_tendencies": "leadership",
    "decision_making_approach": "decision making",
    "problem_solving_behavior": "problem solving",
    "professional_values": "professional values",
    "career_goals": "career direction",
    "communication_style": "communication",
}


def _call_groq(prompt, max_tokens=700, temperature=0.4, label="unknown"):
    """One Groq chat call returning parsed JSON, or None on failure.

    The quiz/communication flows always have a hand-written fallback, so failures
    degrade quietly instead of erroring the request.
    """
    try:
        return groq_json(prompt, max_tokens=max_tokens, temperature=temperature, label=label)
    except GroqError as e:
        print("Groq call failed:", e)
        return None


# ---------------------------------------------------------------------------
# Hidden persona → prompt context (read server-side, never from the client)
# ---------------------------------------------------------------------------

def _persona_brief(for_eval=False):
    """Build a private steering context from the logged-in user's persona, resume
    and onboarding. Returns (brief_text, focus_areas, target_role).

    When `for_eval` is True we omit the persona's weak DIMENSIONS — otherwise the
    evaluator parrots generic traits ("problem solving", "communication") back as the
    interview's weak areas instead of judging the actual subject matter answered.
    """
    user = current_user()
    persona = user.persona
    onboarding = (user.onboarding or {}) if user else {}

    target_role = onboarding.get("target_role") or "their target role"
    lines = [f"Candidate name: {user.name}"]
    if onboarding.get("study"):
        lines.append(f"Studying: {onboarding['study']}")
    if onboarding.get("goal"):
        lines.append(f"Why they're here: {onboarding['goal']}")
    lines.append(f"Target role: {target_role}")

    focus_areas = []
    if persona is not None:
        if persona.summary:
            lines.append(f"Coach's read of them: {persona.summary}")

        dims = persona.dimensions or {}
        strong = [ _DIM_LABELS.get(k, k) for k, v in dims.items() if (v or {}).get("level") == "Strong" ]
        weak   = [ _DIM_LABELS.get(k, k) for k, v in dims.items() if (v or {}).get("level") == "Developing" ]
        focus_areas = weak
        if not for_eval:
            if strong:
                lines.append("Already strong at: " + ", ".join(strong))
            if weak:
                lines.append("Still weak / needs pushing on: " + ", ".join(weak))

        rd = persona.resume_data or {}
        if rd.get("skills"):
            lines.append("Skills on their resume: " + ", ".join(rd["skills"][:10]))
        projects = rd.get("projects") or []
        for p in projects[:3]:
            lines.append(f"Their project: {p.get('name','')} — {p.get('what_they_did','')}")
        if rd.get("likely_gaps") and not for_eval:
            lines.append("Likely knowledge gaps: " + ", ".join(rd["likely_gaps"][:5]))

    return "\n".join(lines), focus_areas, target_role


def _fallback_questions(label):
    return [{"id": i + 1, "question": f"Question {i + 1}: explain a core concept of {label}."} for i in range(5)]


# "No two students get the same paper" made literal — and the code isn't a
# random decoration, it DECODES to real facts about this exact attempt:
# issue date, plus round type & difficulty (role) or topic initials (topic).
# aira.js's Aira.decodePaperRef() turns it back into that sentence in the UI.
_TYPE_CODE = {"technical": "T", "aptitude": "A", "project": "P"}
_DIFF_CODE = {"easy": "E", "medium": "M", "hard": "H"}


def _generate_paper_ref(mode, itype=None, difficulty=None, topic=None):
    datecode = datetime.utcnow().strftime("%m%d")
    if mode == "role":
        t = _TYPE_CODE.get((itype or "").lower(), "T")
        d = _DIFF_CODE.get((difficulty or "").lower(), "M")
        return f"AIRA-{datecode}-{t}{d}"
    letters = "".join(ch for ch in (topic or "").upper() if ch.isalpha())[:2] or "XX"
    return f"REQ-{datecode}-{letters}"


# ---------------------------------------------------------------------------
# Learning resources — we let the model suggest WHAT to look for, but we build the
# links ourselves as SEARCH urls so they always resolve (no hallucinated dead links).
# ---------------------------------------------------------------------------

_RESOURCE_KINDS = {
    "video": ("▶", "https://www.youtube.com/results?search_query="),
    "blog":  ("✎", "https://www.google.com/search?q="),
    "paper": ("❖", "https://scholar.google.com/scholar?q="),
}


def _build_resources(raw):
    """Normalise the model's resource suggestions and attach safe search URLs.

    Expected raw item shape: {"area", "items":[{"title","type","query"}]}.
    """
    out = []
    for group in (raw or [])[:4]:
        area = (group.get("area") or "").strip()
        items = []
        for it in (group.get("items") or [])[:3]:
            kind = (it.get("type") or "blog").lower()
            if kind in ("article", "blogpost"):
                kind = "blog"
            if kind in ("research", "researchpaper", "research paper"):
                kind = "paper"
            if kind not in _RESOURCE_KINDS:
                kind = "blog"
            query = (it.get("query") or it.get("title") or area).strip()
            if not query:
                continue
            icon, base = _RESOURCE_KINDS[kind]
            items.append({
                "title": (it.get("title") or query).strip(),
                "type": kind,
                "icon": icon,
                "url": base + urllib.parse.quote(query),
            })
        if area and items:
            out.append({"area": area, "items": items})
    return out


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

# Real interviews vary in length — map a chosen duration to a sensible question count.
_DURATION_COUNT = {10: 4, 20: 6, 30: 8}

# Interview tracks for the role mode.
_INTERVIEW_TYPES = {
    "aptitude": "aptitude round — quantitative ability, logical reasoning and verbal/analytical questions",
    "technical": "technical round — core fundamentals and applied concepts for the role",
    "project": "project round — deep questions about the candidate's own resume projects",
}

_DIFFICULTY_GUIDE = {
    "easy": "Easy: foundational, entry-level questions a fresher should handle.",
    "medium": "Medium: applied questions needing real understanding and some depth.",
    "hard": "Hard: challenging, senior-level questions probing edge cases and trade-offs.",
}


def _interview_count(duration):
    try:
        return _DURATION_COUNT.get(int(duration), 5)
    except (TypeError, ValueError):
        return 5


def _role_prompt(brief, focus_areas, target_role, itype, difficulty, count):
    track = _INTERVIEW_TYPES.get(itype, _INTERVIEW_TYPES["technical"])
    diff = _DIFFICULTY_GUIDE.get(difficulty, _DIFFICULTY_GUIDE["medium"])

    if itype == "aptitude":
        focus_rule = ("- These are general aptitude questions; they need not use the resume, "
                      "but keep them at a level appropriate for the role.")
    elif itype == "project":
        focus_rule = ("- Base EVERY question on the candidate's OWN resume projects/skills — make "
                      "them defend real decisions, trade-offs and challenges they faced.")
    else:  # technical
        focus_rule = (f"- Lean at least half the questions toward their weak areas: "
                      f"{', '.join(focus_areas) or 'core fundamentals'}. Ground 1-2 in their resume.")

    return f"""You are a senior interviewer running a real "{target_role}" mock interview —
specifically the {track}. You know the candidate from their private profile below. Do NOT
mention the profile; just ask questions that fit them.

PRIVATE PROFILE (for your eyes only):
{brief}

DIFFICULTY — {diff}

Generate exactly {count} interview questions for this round:
{focus_rule}
- All questions must match the difficulty above.
- They will be asked ONE AT A TIME like a real interview, so make each one stand alone.
- Make them feel like a genuine interview, not a textbook quiz.

Return ONLY JSON: {{"questions":[{{"id":1,"question":"..."}}, ... {count} total]}}"""


@quiz_bp.route("/quiz/generate", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def generate_quiz():
    data = request.get_json() or {}
    mode = (data.get("mode") or "topic").lower()
    topic = (data.get("topic") or "").strip()
    brief, focus_areas, target_role = _persona_brief()

    if mode == "role":
        itype = (data.get("type") or "technical").lower()
        difficulty = (data.get("difficulty") or "medium").lower()
        duration = data.get("duration", 20)
        count = _interview_count(duration)
        label = target_role
        prompt = _role_prompt(brief, focus_areas, target_role, itype, difficulty, count)
        meta = {"type": itype, "difficulty": difficulty, "duration": duration}
    else:
        if not topic:
            return jsonify({"error": "Please enter a topic."}), 400
        count = 5
        label = topic
        meta = {}
        prompt = f"""You are an expert interviewer. Generate 5 thoughtful, conceptual interview
questions on "{topic}" tailored to this candidate, whose private profile is below. Do NOT
mention the profile.

PRIVATE PROFILE (for your eyes only):
{brief}

Rules:
- Test genuine understanding, not recall.
- Lean toward the candidate's weak areas where the topic allows: {', '.join(focus_areas) or 'general'}.
- Mix difficulty: 2 foundational, 2 applied, 1 challenging.

Return ONLY JSON: {{"questions":[{{"id":1,"question":"..."}}, ... 5 total]}}"""

    result = _call_groq(prompt, max_tokens=900, label="generate_quiz")
    questions = (result or {}).get("questions") if isinstance(result, dict) else None
    if not questions:
        print("Quiz fallback for", label)
        questions = _fallback_questions(label)[:count]

    if mode == "role":
        paper_ref = _generate_paper_ref(mode, itype=itype, difficulty=difficulty)
    else:
        paper_ref = _generate_paper_ref(mode, topic=label)

    # Hold the generated set server-side: /quiz/evaluate grades THESE questions, so
    # a client can't post back a forged transcript and have it scored into history.
    set_pending(current_user().id, "quiz", {
        "questions": questions, "mode": mode, "topic": label, "paper_ref": paper_ref, **meta
    })

    return jsonify({"questions": questions, "mode": mode, "topic": label, "paper_ref": paper_ref, **meta}), 200


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

@quiz_bp.route("/quiz/evaluate", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def evaluate_quiz():
    data = request.get_json() or {}
    answers = data.get("answers")  # {"Q<id>": "..."} keyed by question id
    if not isinstance(answers, dict):
        return jsonify({"error": "Expected 'answers' as an object keyed by question id."}), 400

    # Grade only the question set WE generated and stored for this user — the client
    # sends answers alone. No stored set means there is nothing legitimate to score.
    user_id = current_user().id
    stored = get_pending(user_id, "quiz")
    if not stored or not stored.get("questions"):
        return jsonify({"error": "No active interview to evaluate. Generate questions first."}), 400

    questions = stored["questions"]
    paper_ref = stored.get("paper_ref")
    topic = (stored.get("topic") or "this interview").strip()
    itype = (stored.get("type") or "").strip()
    difficulty = (stored.get("difficulty") or "").strip()
    round_label = topic + (f" · {itype} round ({difficulty})" if itype else "")
    n_questions = len(questions)
    brief, focus_areas, target_role = _persona_brief(for_eval=True)

    # Build a readable transcript so the model judges the ACTUAL questions asked, not
    # answers in a vacuum. This is what makes the feedback specific to THIS session.
    lines = []
    for i, q in enumerate(questions):
        answer = (answers.get(f"Q{q.get('id')}") or "").strip() or "(left blank)"
        lines.append(f"Q{i+1}: {q.get('question','')}\nTheir answer: {answer}")
    transcript = "\n\n".join(lines)

    prompt = f"""You are an honest, supportive interview coach. Evaluate how this candidate did in
the interview transcript below. You know them from their private profile — use it only to set
your tone and pick examples, but NEVER reveal or quote the profile.

PRIVATE PROFILE (for your eyes only):
{brief}

INTERVIEW ROUND: {round_label}

TRANSCRIPT (the exact questions asked and what they answered):
{transcript}

Evaluate THIS session specifically:
- Reference the actual questions and answers above. Let the feedback flow from what they said.
- "weak_areas" MUST be the concrete subject topics/concepts FROM THESE QUESTIONS that they
  answered poorly or vaguely — e.g. "database indexing", "REST API pagination", "process
  scheduling". They must NOT be generic soft skills or personality traits like "problem solving"
  or "communication" unless the question was literally about that.
- Resources must target those exact concepts, so a weak answer on indexing yields indexing
  resources — not generic "how to solve problems" videos.
- Judge real understanding, not length. Reward strong answers, call out weak/blank ones plainly.
- Do NOT invent URLs — give a precise SEARCH QUERY for each resource. Write in simple English.

Return ONLY JSON:
{{
  "feedback": "2-3 honest sentences about how THIS interview went, citing specific answers",
  "score": "X/{n_questions} answered well",
  "weak_areas": ["specific topic from the questions they were weak on", "another"],
  "study_plan": "specific 2-3 sentence plan on the actual topics they missed in this session",
  "suggestions": ["actionable tip tied to a specific topic", "another", "another"],
  "resources": [
    {{"area": "the specific topic", "items": [
      {{"title": "what to look for", "type": "video", "query": "exact search terms for that topic"}},
      {{"title": "...", "type": "blog", "query": "..."}},
      {{"title": "...", "type": "paper", "query": "..."}}
    ]}}
  ]
}}"""

    result = _call_groq(prompt, max_tokens=1100, label="evaluate_quiz")
    if not isinstance(result, dict) or not result.get("feedback"):
        print("Evaluation fallback for", topic)
        # Keep the fallback tied to the session's topic, not generic persona traits.
        weak = [topic]
        result = {
            "feedback": "We couldn't fully evaluate this round. Try submitting again.",
            "score": "",
            "weak_areas": weak,
            "study_plan": f"Revise the core ideas of {topic} and answer 2 practice questions.",
            "suggestions": ["Explain it aloud", "Write one worked example"],
            "resources": [{"area": w, "items": [
                {"title": f"{w} explained", "type": "video", "query": f"{w} tutorial"},
                {"title": f"{w} guide", "type": "blog", "query": f"{w} explained"},
            ]} for w in weak],
        }

    # Turn the model's resource suggestions into safe, always-resolving search links.
    result["resources"] = _build_resources(result.get("resources"))

    # Persist this attempt for the logged-in user.
    record = QuizResult(
        user_id=user_id,
        topic=topic,
        score=result.get("score"),
        feedback=result.get("feedback"),
        study_plan=result.get("study_plan"),
        weak_areas=result.get("weak_areas") or [],
        suggestions=result.get("suggestions") or [],
        paper_ref=paper_ref,
    )
    db.session.add(record)
    db.session.commit()

    # This set is spent — a fresh /quiz/generate is needed for the next attempt.
    clear_pending(user_id, "quiz")

    result["paper_ref"] = paper_ref
    return jsonify({"feedback": result}), 200


@quiz_bp.route("/me/quizzes", methods=["GET"])
@login_required
def my_quizzes():
    rows = current_user().quiz_results
    return jsonify({"quizzes": [q.to_dict() for q in rows]}), 200
