"""Generates the Core Persona assessment: one LLM call that judges the QUALITY of
each answer (not just its topic) across the 8 dimensions, grounded in the user's
onboarding details, resume, and — on a refresh — real practice-session evidence
accumulated since the last profile, not just a repeat of the same questionnaire.
"""
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator, ValidationError

from groq_client import groq_json, GroqError
from llm_schemas import PERSONA_DIMENSIONS, describe_validation_error
from onboarding_schema import prompt_context


class PersonaGenerationError(Exception):
    """Raised on a genuine LLM/network failure so the caller can return a retryable
    error instead of caching a generic fallback persona."""


class DimensionScore(BaseModel):
    level: Literal["Strong", "Moderate", "Developing"]
    note: str = ""

    @field_validator("note", mode="before")
    @classmethod
    def _default_note(cls, v):
        return (v or "").strip()


class PersonaResult(BaseModel):
    summary: str
    dimensions: dict[str, DimensionScore]

    @field_validator("summary")
    @classmethod
    def _summary_not_blank(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("summary is required")
        return v

    @model_validator(mode="after")
    def _all_dimensions_present(self):
        missing = [d for d in PERSONA_DIMENSIONS if d not in self.dimensions]
        if missing:
            raise ValueError(f"missing dimension(s): {', '.join(missing)}")
        return self


def _validate(data):
    """Pure, network-free validation of the raw persona dict — all 8 dimensions
    must be present with a valid level, not just a truthy summary/dimensions
    check. Extracted so it's directly unit-testable, mirroring resume_agent._normalize."""
    try:
        result = PersonaResult.model_validate(data)
    except ValidationError as e:
        # Metrics only (see llm_schemas.describe_validation_error): the exception
        # renders the offending input value, which here is the model's persona
        # summary and per-dimension notes — a written assessment of a named
        # person, derived from their answers and resume.
        print(f"Persona validation failed ({describe_validation_error(e)})")
        raise PersonaGenerationError("Aira couldn't build your persona right now. Please try again.")
    return result.model_dump()


def _format_context(onboarding, resume_data):
    """Concrete facts the assessment must be grounded in: who they are, what they
    want, and what their resume actually shows."""
    lines = ["WHAT THEY TOLD US ABOUT THEMSELVES:"]
    onboarding = prompt_context(onboarding)
    if onboarding:
        for k, v in onboarding.items():
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


def format_session_evidence(report):
    """Real performance signal accumulated since the Core Persona was last built —
    what a refresh should weigh alongside (or against) the original answers, not
    just a repeat of the same questionnaire. None if there's no session history yet.

    `report` is the dict shape returned by persona_bp._build_report_payload
    (quizzes, speaking, speaking_averages, recurring_weak_areas).
    """
    quizzes = report.get("quizzes") or []
    speaking = report.get("speaking") or []
    if not quizzes and not speaking:
        return None

    lines = [f"\nEVIDENCE FROM {len(quizzes) + len(speaking)} COMPLETED PRACTICE SESSION(S) SINCE THEIR CURRENT PROFILE:"]

    avg = report.get("speaking_averages")
    if avg:
        parts = [f"{k}: {v}/10" for k, v in avg.items() if v is not None]
        if parts:
            lines.append(f"  - average speaking scores — {', '.join(parts)}")

    weak = report.get("recurring_weak_areas") or []
    if weak:
        named = ", ".join(f"{w['area']} (seen {w['count']}x)" for w in weak[:6])
        lines.append(f"  - recurring weak areas across sessions: {named}")

    quiz_topics = ", ".join(q.get("topic", "") for q in quizzes[:5] if q.get("topic"))
    if quiz_topics:
        lines.append(f"  - quiz topics attempted: {quiz_topics}")

    lines.append(
        "This is real evidence of how they perform under practice, not just what they said "
        "about themselves. If it confirms the original answers, say so plainly. If it reveals "
        "something new or contradicts the original answers, update the relevant area(s) and the "
        "note for that area should say what changed and why."
    )
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


# How the verdict is DELIVERED on a first build. The scoring rubric above is
# untouched — levels stay honest, or the report card means nothing. What changes
# is the summary's shape: a first-timer meets this screen roughly ten minutes
# after signing up, and the first thing it said to one real user was that their
# answers "lacked detail". They abandoned the next quiz and left. A returning
# user has bought in and can take the blunt read; someone eleven minutes in has
# not agreed to be assessed like that yet.
_FIRST_BUILD_SUMMARY_RULE = (
    "  - This is the FIRST time this person has ever seen a profile from you, minutes "
    "after signing up. Score the areas exactly as strictly as the rubric says — but "
    "write the summary as a STARTING POINT, not a verdict. Open with what they "
    "genuinely did well, citing something real they said. Then name the ONE area that "
    "would help them most to work on, phrased as the next thing to practise rather "
    "than a shortcoming. Do not list every weakness. Close by telling them what "
    "practising will show. Still honest — never invent a strength they did not show — "
    "but they should finish reading it wanting to continue.\n"
)

_REFRESH_SUMMARY_RULE = (
    "  - This person has practised with you before and is seeing an UPDATED profile. "
    "They have earned the blunt read: say plainly what has improved and what has not "
    "moved, citing the evidence.\n"
)


def _build_llm_prompt(responses, onboarding=None, resume_data=None, session_evidence=None,
                      first_build=True):
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
    ]
    if session_evidence:
        lines.append(session_evidence + "\n")
    lines.append("THEIR ASSESSMENT ANSWERS:\n")

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
        + (_FIRST_BUILD_SUMMARY_RULE if first_build else _REFRESH_SUMMARY_RULE) +
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


def generate_core_persona(responses, onboarding=None, resume_data=None, session_evidence=None,
                          first_build=True):
    """Single LLM call → persona dict with source_id tracking merged in.

    The assessment is grounded in the user's onboarding details and resume so it
    speaks about the actual person, not a generic label. On a refresh, session_evidence
    (real quiz/speaking performance since the last profile) is weighed alongside the
    questionnaire answers rather than treating a retake as the only signal.

    first_build changes only how the summary is DELIVERED, never how the areas are
    scored — see _FIRST_BUILD_SUMMARY_RULE. Passed explicitly rather than inferred
    from `session_evidence is None`, which is a different question: a refreshing
    user with no completed sessions yet would have no evidence but has still
    earned the blunt read.
    """
    source_map = {r["dimension"]: r["id"] for r in responses if r.get("dimension")}

    prompt = _build_llm_prompt(responses, onboarding, resume_data, session_evidence,
                               first_build=first_build)

    try:
        # Low temperature so the same answers score consistently and the rubric is
        # followed rather than improvised. json_mode forces valid JSON output.
        data = groq_json(prompt, max_tokens=1100, temperature=0.2, label="generate_core_persona")
    except GroqError as e:
        # A genuine LLM/network failure. Surface it as retryable rather than
        # silently persisting a generic persona that would then be cached forever.
        # Distinct wording from the validation path above: both used to print
        # "Persona generation error", so a log line couldn't tell a network
        # failure apart from a malformed reply. GroqError carries API/transport
        # detail, not persona content, so it's safe to log whole.
        print("Persona generation failed (Groq call):", e)
        raise PersonaGenerationError("Aira couldn't build your persona right now. Please try again.")

    out = _validate(data)

    # Attach the question variant ID that informed each dimension, for future traceability.
    for dim, source_id in source_map.items():
        if dim in out["dimensions"]:
            out["dimensions"][dim]["source_id"] = source_id

    return out
