"""Generates the persona-dimension questions dynamically, per user, instead of
drawing from a static hardcoded bank (question_bank.json used to hold 3 hand-
written variants per dimension; a returning user had roughly a 1-in-3 chance of
seeing the exact same question again on a refresh).

Output is shaped identically to the old fixed-bank entries and to the resume-
grounded questions, so the rest of the pipeline (scoring, assembly, the frontend)
can't tell the difference.
"""
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator, ValidationError

from groq_client import groq_json, GroqError, FAST_MODEL
from llm_schemas import (
    PERSONA_DIMENSIONS,
    Option,
    describe_validation_error,
    normalize_dimension,
    normalize_options,
    require_non_blank,
)


class QuestionGenerationError(Exception):
    """Raised on a genuine LLM/network failure generating dimension questions."""


# Used when the model drops a dimension entirely. These are real interview
# questions, one per dimension — NOT a template built from the dimension name.
# The old fallback ("...how you handle leadership tendencies") was both vague and
# a direct leak of the trait being measured, which the whole assessment depends
# on staying hidden.
FALLBACK_QUESTIONS = {
    "work_culture_preferences":
        "Think about a place you studied or worked where you did your best work. "
        "What was it about how that place ran day to day that suited you?",
    "teamwork_style":
        "Tell me about a group project where the work was not shared evenly. "
        "What did you do about it?",
    "leadership_tendencies":
        "Tell me about a time you took ownership of something when nobody else "
        "stepped forward.",
    "decision_making_approach":
        "Describe a difficult decision you had to make when you did not have all "
        "the information you wanted.",
    "problem_solving_behavior":
        "Tell me about the hardest bug or technical problem you have solved "
        "recently. How did you get to the bottom of it?",
    "professional_values":
        "Tell me about a time you were pushed to do something the quick way when "
        "you felt there was a right way. What did you do?",
    "career_goals":
        "Where do you want to be in your work two years from now, and what are "
        "you doing right now to get there?",
    "communication_style":
        "Think of something technical you have built or studied. How would you "
        "explain it to someone with no technical background?",
}

_GENERIC_FALLBACK = (
    "Tell me about a recent situation in your work or studies that you think "
    "says a lot about how you operate."
)


def _clean_list(values, limit):
    """Drop null/blank entries BEFORE stringifying. Filtering on str(v) instead
    lets a None through as the literal text "None", which then reaches the model
    as if it were a skill."""
    out = []
    for v in values or []:
        if v is None or isinstance(v, bool):
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out[:limit]


def _format_resume_context(resume_data):
    """The concrete resume facts a question can be built around. Returns "" when
    there's no resume on file, so the prompt simply omits the section rather than
    telling the model about an empty resume.

    Mirrors the shape stored by resume_agent.parse_resume().
    """
    resume_data = resume_data or {}
    lines = []

    projects = [p for p in (resume_data.get("projects") or []) if isinstance(p, dict)]
    project_lines = []
    for p in projects[:4]:
        name = str(p.get("name") or "").strip()
        what = str(p.get("what_they_did") or "").strip()
        if not name and not what:
            continue
        project_lines.append(f"  - {name}: {what}" if name and what else f"  - {name or what}")
    if project_lines:
        lines.append("Projects:")
        lines.extend(project_lines)

    skills = _clean_list(resume_data.get("skills"), 12)
    if skills:
        lines.append("Technical skills: " + ", ".join(skills))

    # Absent from resume_data stored before soft-skill extraction existed, so this
    # section simply doesn't render for those users rather than breaking.
    soft = _clean_list(resume_data.get("soft_skills"), 8)
    if soft:
        lines.append("Soft skills: " + ", ".join(soft))

    level = str(resume_data.get("experience_level") or "").strip()
    if level:
        lines.append(f"Experience level: {level}")

    gaps = _clean_list(resume_data.get("likely_gaps"), 5)
    if gaps:
        lines.append("Areas they look weaker in: " + ", ".join(gaps))

    if not lines:
        return ""
    return "THEIR RESUME:\n" + "\n".join(lines)


def _build_prompt(dimensions, onboarding, harder=False, resume_data=None):
    onboarding = onboarding or {}
    context = ", ".join(f"{k}={v}" for k, v in onboarding.items() if v) or "none provided"
    dims_list = "\n".join(f"  - {d}" for d in dimensions)

    # Without this the model only ever saw goal/field/year, so every question it
    # wrote was necessarily generic — the reported "questions don't relate to my
    # resume" bug. The resume block is omitted entirely when there's no resume.
    resume_block = _format_resume_context(resume_data)
    resume_section = f"\n{resume_block}\n" if resume_block else ""
    resume_rule = (
        """  - GROUND THE SCENARIOS IN THEIR OWN WORK. Wherever it fits naturally, build the
    question around a real project, technology or responsibility from the resume
    above — name it explicitly ("While building SmartAttend, ...", "Your OpenCV
    pipeline starts ..."). A question that could have been asked of any candidate
    is a wasted question.
  - Do NOT force it. If a dimension has nothing to do with their technical work,
    a realistic everyday work/study scenario is fine — but still keep it concrete.
  - Grounding a question in their resume must NOT turn it into a technical quiz:
    you are still measuring how they work and think, not what they know.\n"""
        if resume_block else ""
    )

    difficulty_rule = (
        """  - REFRESH mode (the candidate already has a Core Persona and real practice
    history) — two extra rules, same JSON shape as always:
    * Do not use these overused setups: a teammate falling behind, an approaching
      deadline, a coworker disagreeing in a meeting, receiving critical feedback.
      Pick a different, more specific scenario instead.
    * Make the 4 options genuinely close calls — each should trade one real value
      against another, with no single option obviously more "mature" than the rest.
    Every situational question still needs exactly 4 options with a label, text and
    signal each — that requirement does not change in refresh mode.\n"""
        if harder else ""
    )

    return f"""You are writing a personality/work-style assessment for a career coaching app.
The candidate will answer these questions and must never see which trait each one targets.

CANDIDATE CONTEXT: {context}
{resume_section}
Write ONE question for EACH of these {len(dimensions)} dimensions, in this order:
{dims_list}

Rules per question:
  - Ground it in a realistic, everyday work/study scenario — concrete, not abstract.
{resume_rule}  - For the "communication_style" dimension: write a free-text "recall" question
    ("Tell me about a time you...") asking them to recount a real situation.
  - For every OTHER dimension: write a multiple-choice "situational" question with
    exactly 4 options. Each option must read like something a real person would
    actually choose, and each needs a one-line "signal" describing what choosing
    it reveals about the person.
  - Vary the scenarios — do not reuse the same setup across questions.
  - The candidate must never be able to tell these were auto-generated or which
    trait is being measured.
{difficulty_rule}
Return ONLY this JSON, no markdown, no commentary:
{{
  "questions": [
    {{
      "dimension": "<dimension from the list above>",
      "type": "situational or recall",
      "text": "...",
      "options": [
        {{"label": "A", "text": "...", "signal": "..."}},
        {{"label": "B", "text": "...", "signal": "..."}},
        {{"label": "C", "text": "...", "signal": "..."}},
        {{"label": "D", "text": "...", "signal": "..."}}
      ]
    }}
  ]
}}
For a "recall" question, omit "options" (or set it to null)."""


def generate_dimension_questions(dimensions, onboarding=None, harder=False, resume_data=None):
    """One Groq call -> one question per requested dimension, shaped like the old
    fixed-bank/resume-grounded questions. Raises QuestionGenerationError on a
    genuine LLM/network failure so the caller can surface a retryable error
    instead of silently returning an incomplete quiz.

    harder=True is for a persona REFRESH (the candidate already has a Core
    Persona and real practice-session history) — scenarios get higher-stakes
    and options closer in plausibility, instead of repeating the same
    first-timer difficulty every attempt.

    resume_data is the structured profile stored by resume_agent.parse_resume().
    It's optional (a caller without a resume still gets a valid, if generic,
    quiz) but passing it is what makes these questions actually about the
    candidate rather than about nobody in particular.
    """
    if not dimensions:
        return []

    prompt = _build_prompt(dimensions, onboarding, harder=harder, resume_data=resume_data)
    # NOTE: an earlier version also raised temperature to 0.8 on refresh, hoping
    # more sampling variance would help the model diverge from its default
    # completion. In practice it destabilized JSON schema compliance instead —
    # several dimensions failed validation and silently fell back to the generic
    # recall prompt (see _normalize below). Keeping temperature fixed and letting
    # the (now more concrete, example-banning) instructions alone do the work.
    try:
        data = groq_json(prompt, max_tokens=1400, temperature=0.6, json_mode=True,
                         label="generate_dimension_questions", model=FAST_MODEL)
    except GroqError as e:
        print("Dimension question generation error:", e)
        raise QuestionGenerationError("Aira couldn't prepare your questions right now. Please try again.")

    questions = (data or {}).get("questions") or []
    return _normalize(questions, dimensions)


class GeneratedQuestion(BaseModel):
    """One candidate question, validated in isolation. The coverage-guarantee
    loop in _normalize() decides what happens to a dimension with no surviving
    valid question — that's imperative repair logic, not a model concern."""
    dimension: str
    type: str = "situational"
    text: str
    options: Optional[list[Option]] = Field(default=None)

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, v):
        return "recall" if v == "recall" else "situational"

    @field_validator("text")
    @classmethod
    def _require_text(cls, v):
        return require_non_blank(v)

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_opts(cls, v):
        return normalize_options(v)

    @model_validator(mode="after")
    def _finalize(self):
        if self.type == "recall":
            self.options = None
        elif len(self.options or []) < 2:
            raise ValueError("situational question needs at least 2 usable options")
        return self


def _normalize(questions, requested_dims):
    """Validate/repair the model output and guarantee coverage — fall back to a
    plain recall prompt for any dimension the model dropped rather than silently
    shipping a quiz with a missing dimension."""
    by_dim = {}
    for raw in questions:
        if not isinstance(raw, dict):
            continue
        # Resolve near-misses ("leadership" -> leadership_tendencies) instead of
        # discarding them: a dropped question here becomes a fallback question
        # below, so loose tagging used to silently cost real generated content.
        dim = normalize_dimension(raw.get("dimension"))
        if dim is None or dim not in requested_dims or dim in by_dim:
            continue
        try:
            q = GeneratedQuestion.model_validate({**raw, "dimension": dim})
        except ValidationError as e:
            print(f"Dimension question for '{dim}' failed validation "
                  f"({describe_validation_error(e)}) — falling back.")
            continue
        by_dim[dim] = q.model_dump()

    result = []
    for i, dim in enumerate(requested_dims):
        if dim not in by_dim:
            print(f"No valid model question for '{dim}' — using the natural fallback question.")
        q = by_dim.get(dim) or {
            "dimension": dim,
            "type": "recall",
            "text": FALLBACK_QUESTIONS.get(dim, _GENERIC_FALLBACK),
            "options": None,
        }
        q["id"] = f"gen-{dim}-{i}"
        result.append(q)
    return result
