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
import random
import urllib.parse

from models import db, QuizResult, get_pending, set_pending, clear_pending
from auth import current_user, login_required
from rate_limiter import limiter
from groq_client import groq_json, GroqError, FAST_MODEL

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


def _call_groq(prompt, max_tokens=700, temperature=0.4, label="unknown", model=None):
    """One Groq chat call returning parsed JSON, or None on failure.

    The quiz/communication flows always have a hand-written fallback, so failures
    degrade quietly instead of erroring the request.

    model defaults to None (the quality-first ladder) because most callers here —
    evaluation and the communication turns — are judgement work. Pass FAST_MODEL
    only for generation that a smaller model does just as well; see generate_quiz.
    """
    try:
        return groq_json(prompt, max_tokens=max_tokens, temperature=temperature,
                         label=label, model=model)
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
    "aptitude": "aptitude round — a timed mixed paper of quantitative ability, logical reasoning and data interpretation, in the style of a campus placement test",
    "technical": "technical round — core fundamentals and applied concepts for the role",
    "project": "project round — deep questions about the candidate's own resume projects",
}

# ---------------------------------------------------------------------------
# Aptitude
# ---------------------------------------------------------------------------
#
# A real campus aptitude paper (TCS NQT, Infosys, Accenture, Cognizant, AMCAT and
# friends) is a MIXED, sectional test — not a themed quiz. TCS NQT's foundation
# round is Numerical Ability 20Q/25min, Reasoning Ability 20Q/25min and Verbal
# Ability 25Q/26min, sat with a per-question timer and no negative marking. The
# two things that make it feel real are therefore:
#
#   1. the MIX — a candidate never gets six percentage sums in a row, and
#   2. the ROTATION — sitting a second paper must not re-run the first one.
#
# The old prompt was a single sentence ("quantitative ability, logical reasoning
# and verbal/analytical questions") with no blueprint behind it, so the model
# picked whatever came to mind and generally converged on percentages and blood
# relations every single time. Choosing the topics HERE rather than leaving it to
# the model is what guarantees both properties.
_APTITUDE_TOPICS = {
    "quantitative ability": [
        "percentages", "profit and loss", "ratio and proportion", "averages",
        "time and work", "time, speed and distance", "simple and compound interest",
        "number system, LCM and HCF", "problems on ages", "mixtures and alligations",
        "permutations and combinations", "probability", "pipes and cisterns",
        "boats and streams", "problems on trains", "partnerships", "mensuration",
    ],
    "logical reasoning": [
        "number series", "letter series", "coding-decoding", "blood relations",
        "linear seating arrangement", "circular seating arrangement", "syllogism",
        "data sufficiency", "direction sense", "statement and conclusion",
        "analogies", "odd one out", "constraint puzzles", "clocks and calendars",
        "cube folding and visual reasoning",
    ],
    "data interpretation": [
        "table interpretation", "bar chart interpretation", "line graph interpretation",
        "pie chart interpretation", "caselet data",
    ],
}


def _aptitude_blueprint(count):
    """Pick a concrete (section, topic) plan for `count` questions.

    Weighted to the two sections the user actually sits for — quantitative and
    logical, roughly 40/40 — with the remainder on data interpretation. Topics are
    sampled without replacement so one paper never repeats a topic, and randomly so
    the next paper is a different one.
    """
    count = max(1, int(count or 5))
    # Data interpretation carries a table or chart with it, which is a lot of question
    # for a short paper — so it only appears once there are five or more, and stays at
    # roughly a fifth. The remainder splits evenly between the two core sections, with
    # the odd question going to quantitative.
    di = 0 if count < 5 else max(1, round(count * 0.2))
    rest = count - di
    logical = rest // 2
    quant = rest - logical

    plan = []
    for section, n in (("quantitative ability", quant),
                       ("logical reasoning", logical),
                       ("data interpretation", di)):
        if n <= 0:
            continue
        pool = _APTITUDE_TOPICS[section]
        plan.extend((section, t) for t in random.sample(pool, min(n, len(pool))))
    random.shuffle(plan)            # interleave sections, like a real paper
    return plan


_VALID_LABELS = ("A", "B", "C", "D")


def _split_answer_key(questions):
    """Separate a generated MCQ set into what the client may see and what only the
    server keeps.

    The answer key must NEVER go to the browser. It would sit in the network tab and
    in the page source, and an aptitude paper whose answers can be read off it is not
    a test of anything. /quiz/evaluate reads the key back out of the pending record,
    the same way the question set itself is already trusted only from the server.

    A question the model returned without a usable key is left as-is and simply not
    machine-marked — a paper that half-generated should still be answerable.
    """
    public, key = [], {}
    for q in questions:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id"))
        answer = str(q.get("answer") or "").strip().upper()[:1]
        options = q.get("options")
        if answer in _VALID_LABELS and isinstance(options, dict) and answer in options:
            key[qid] = {"answer": answer, "solution": (q.get("solution") or "").strip()}
        public.append({k: v for k, v in q.items() if k not in ("answer", "solution")})
    return public, key


def _mark_paper(questions, answers, key):
    """Machine-mark an MCQ paper. Returns (correct, total, transcript_lines).

    Correctness here is arithmetic, not opinion. Asking the LLM to decide whether an
    answer was right is what let confident working on a wrong number score well; once
    the key exists, the marking is a fact and the model's only job is the coaching.
    """
    correct, total, lines = 0, 0, []
    for i, q in enumerate(questions):
        qid = str(q.get("id"))
        entry = key.get(qid)
        given = (answers.get(f"Q{qid}") or "").strip().upper()[:1]
        line = f"Q{i+1}: {q.get('question','')}"
        if not entry:
            line += f"\nTheir answer: {given or '(left blank)'}"
            lines.append(line)
            continue
        total += 1
        right = entry["answer"]
        options = q.get("options") or {}
        if not given:
            line += f"\nTheir answer: (left blank) — WRONG (correct: {right}. {options.get(right, '')})"
        elif given == right:
            correct += 1
            line += f"\nTheir answer: {given}. {options.get(given, '')} — CORRECT"
        else:
            line += (f"\nTheir answer: {given}. {options.get(given, '')} — WRONG "
                     f"(correct: {right}. {options.get(right, '')})")
        if entry.get("solution"):
            line += f"\nWorking: {entry['solution']}"
        lines.append(line)
    return correct, total, lines


def _resume_topics():
    """Concrete TECHNICAL material from the résumé: skills and self-declared gaps.

    Empty for most users now that the résumé is optional at onboarding — which is
    exactly the case the technical round has to survive.
    """
    persona = getattr(current_user(), "persona", None)
    rd = (persona.resume_data or {}) if persona else {}
    return (rd.get("skills") or [])[:8], (rd.get("likely_gaps") or [])[:5]

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
        plan = _aptitude_blueprint(count)
        lines = "\n".join(
            f"  {i+1}. {section} — {topic}" for i, (section, topic) in enumerate(plan)
        )
        focus_rule = f"""- Follow this exact paper blueprint, one question per line, in this order:
{lines}
- This is an aptitude paper, NOT an interview chat. Each question must be fully
  self-contained: state every number and condition needed to solve it, and have ONE
  definite correct answer.
- Size each one to be solvable in about 60-90 seconds, as in a real timed sectional test.
- Do not mention the candidate, their role, their résumé or their profile anywhere in
  these questions. Aptitude papers are identical for every candidate who sits them.
- Ask the candidate to give their answer AND their working."""
    elif itype == "project":
        focus_rule = ("- Base EVERY question on the candidate's OWN resume projects/skills — make "
                      "them defend real decisions, trade-offs and challenges they faced.")
    else:  # technical
        # NOT `focus_areas`. Those are the persona's weak behavioural DIMENSIONS —
        # "communication", "teamwork", "handling pressure" — so aiming the technical
        # round at them was an instruction to write HR questions. With a résumé the
        # project facts partly masked it; once the résumé became optional it was the
        # only steer left, and the technical round started sounding like an HR round.
        skills, gaps = _resume_topics()
        anchor = gaps or skills
        if anchor:
            focus_rule = (f"- Ground the questions in the technologies they actually claim: "
                          f"{', '.join(anchor)}. Push on depth, not trivia.")
        else:
            # No résumé: the role and the field of study are the only honest technical
            # anchors available. The persona holds nothing technical whatsoever.
            focus_rule = (f"- This candidate has no résumé on file, so build the questions from the "
                          f"standard core syllabus a \"{target_role}\" is expected to know, pitched at "
                          f"their field of study. Cover a spread of the fundamentals rather than one topic.")
        focus_rule += """
- This is a TECHNICAL round. Every question must have a technically checkable answer:
  a concept to explain, a mechanism to describe, a trade-off to justify, or a problem
  to work through.
- Do NOT ask about motivation, strengths and weaknesses, teamwork, conflict, career
  goals, or anything starting "tell me about a time". Those belong to the HR round. If
  a question could be answered honestly with a feeling or a story, rewrite it."""

    if itype == "aptitude":
        # The persona brief is deliberately NOT sent here. A real aptitude paper is
        # the same for everyone in the hall, so the profile would steer nothing — and
        # on a per-model daily token cap that does not roll over, sending it would be
        # spend that buys literally nothing.
        return f"""You are setting a campus placement aptitude paper. It is MULTIPLE CHOICE,
exactly as the real test is sat.

DIFFICULTY — {diff}

Set exactly {count} questions:
{focus_rule}
- They will be shown ONE AT A TIME with a timer, so each must stand completely alone.
- Give exactly FOUR options, labelled A, B, C and D. Exactly ONE is correct.
- The three wrong options must be the answers a candidate actually arrives at by
  making the usual mistakes on that topic — the percentage taken on the wrong base,
  the ratio inverted, the return journey forgotten. Never filler numbers, and never
  an option so absurd it can be discarded without doing the work. Eliminating bad
  options is half the skill the real paper is testing.
- "solution" is one or two lines of working showing why the correct option is right.

Return ONLY JSON: {{"questions":[{{"id":1,"question":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"B","solution":"..."}}, ... {count} total]}}"""

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

    # Cheap tier: writing questions is generation, not judgement, and the quality
    # tier is the smaller pool (300k/day vs 700k) carrying the most features. On
    # the worst case this asks for — a 30-minute role round, 8 hard questions —
    # gpt-oss-20b returned all 8, grounded in the candidate's own projects and
    # weighted to their weak areas, in FEWER tokens than the 120b. Evaluation
    # below deliberately stays on the quality tier: grading someone's answers is
    # the judgement call, writing the questions is not.
    result = _call_groq(prompt, max_tokens=900, label="generate_quiz", model=FAST_MODEL)
    questions = (result or {}).get("questions") if isinstance(result, dict) else None
    if not questions:
        print("Quiz fallback for", label)
        questions = _fallback_questions(label)[:count]

    # Aptitude is the one MCQ round, so it is the one round with an answer key to
    # keep back. `questions` is rebound to the stripped copy, which is what both the
    # pending record and the response below then carry.
    answer_key = {}
    if mode == "role" and itype == "aptitude":
        questions, answer_key = _split_answer_key(questions)

    if mode == "role":
        paper_ref = _generate_paper_ref(mode, itype=itype, difficulty=difficulty)
    else:
        paper_ref = _generate_paper_ref(mode, topic=label)

    # Hold the generated set server-side: /quiz/evaluate grades THESE questions, so
    # a client can't post back a forged transcript and have it scored into history.
    set_pending(current_user().id, "quiz", {
        "questions": questions, "mode": mode, "topic": label, "paper_ref": paper_ref,
        "answer_key": answer_key, **meta
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
    answer_key = stored.get("answer_key") or {}
    round_rule = ""
    if answer_key:
        # Aptitude: already marked in Python above. Correctness is arithmetic, so the
        # model is told the result rather than asked for it — that is what stops
        # fluent working on a wrong number from scoring well.
        correct, marked_total, lines = _mark_paper(questions, answers, answer_key)
        round_rule = f"""
- This was an APTITUDE paper and it has ALREADY been marked against the answer key:
  they scored {correct} out of {marked_total}. That marking is authoritative — do not
  re-grade it, do not argue with it, and never soften a WRONG answer because the
  reasoning around it sounded confident.
- Their "weak_areas" must be the aptitude TOPICS they got wrong ("time and work",
  "syllogism", "data interpretation"), never soft skills.
- Where they were wrong, point at the specific step that went wrong using the working
  supplied, so the next attempt is different rather than merely more careful."""
    else:
        lines = []
        for i, q in enumerate(questions):
            answer = (answers.get(f"Q{q.get('id')}") or "").strip() or "(left blank)"
            lines.append(f"Q{i+1}: {q.get('question','')}\nTheir answer: {answer}")
        if itype == "aptitude":
            # Generation returned no usable key — a fallback set, or JSON the model
            # malformed. Ask it to mark, which is weaker than arithmetic but beats
            # grading an aptitude paper on how confident the writing sounded.
            round_rule = """
- This was an APTITUDE paper: every question has ONE correct answer. Work each one out
  yourself and judge it right or wrong on the arithmetic before anything else."""
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
- Reference the actual questions and answers above. Let the feedback flow from what they said.{round_rule}
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
