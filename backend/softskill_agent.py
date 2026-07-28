"""Soft-skills framework agent — designs each speaking framework PER USER from
their résumé, instead of drawing from a static hardcoded bank.

The communication module used to hold six fixed frameworks (PREP, STAR, Present·
Past·Future, …) in a dict — the same steps and hints for a first-year student and
a senior engineer alike. This agent replaces that: given only a track's *intent*
(what conversation it trains) plus the person's résumé/persona, it generates the
framework name, why it works, the beats/hints, the focus and a model example,
all adjusted to their actual experience level, domain, projects and target role.

The track id + label are the only structural constants (navigation, URLs and the
unlock system key on them); every pedagogical detail is generated. Shaped exactly
like the old fixed-bank entries so the rest of the pipeline (setup, start, the
adaptive engine, evaluation, the frontend) can't tell the difference.

Mirrors the discipline of persona_agent / question_agent: one Groq call, Pydantic
validation, a typed error, and — because a framework outage must NOT hard-block
practice the way a missing persona does — a minimal skeleton so the page degrades
gracefully when the model is genuinely unreachable.
"""
from typing import Optional

from pydantic import BaseModel, Field, field_validator, ValidationError

from groq_client import groq_json, GroqError
from llm_schemas import describe_validation_error

# Steps allowed per framework — enough to be a real structure, few enough to keep
# a spoken answer memorable. Practice length is derived from this, not trusted raw.
MIN_STEPS, MAX_STEPS = 3, 6
MIN_TOTAL, MAX_TOTAL = 3, 6


class SoftSkillGenerationError(Exception):
    """Raised on a genuine LLM/network failure so the caller can fall back to the
    skeleton rather than 500-ing."""


# ---------------------------------------------------------------------------
# Track blueprints — the ONLY hardcoded piece: a track's identity (label) and a
# one-line INTENT describing the conversation it trains. Deliberately says nothing
# about the framework itself — the agent designs that, tuned to the person.
# ---------------------------------------------------------------------------
TRACK_BLUEPRINTS = {
    "communication": {
        "label": "Communication practice",
        "intent": "speaking clearly and persuasively on any prompt — making one point and backing it up so a listener follows easily",
    },
    "intro": {
        "label": "Self-introduction",
        "intent": "answering 'tell me about yourself' the way an interviewer actually wants to hear it — relevant, structured, not a life story",
    },
    "project": {
        "label": "Talk about your project",
        "intent": "explaining one of their own résumé projects with real depth and ownership, without freezing or listing features",
    },
    "voice": {
        "label": "Voice module",
        "intent": "vocal delivery itself — pace, deliberate pauses instead of filler words, pitch variation, and emphasis on the words that matter",
    },
    "leadership": {
        "label": "Leadership stories",
        "intent": "telling a short story that proves initiative and ownership from an ordinary moment, so it lands as leadership not bragging",
    },
    "technical": {
        "label": "Technical communication",
        "intent": "explaining a complex technical idea simply enough for a non-technical interviewer to follow and see why it matters",
    },
}


def is_track(track):
    return track in TRACK_BLUEPRINTS


def track_label(track):
    return (TRACK_BLUEPRINTS.get(track) or {}).get("label", "Practice session")


def all_tracks():
    return list(TRACK_BLUEPRINTS.keys())


# ---------------------------------------------------------------------------
# Validation — the generated framework, checked in isolation.
# ---------------------------------------------------------------------------
class Step(BaseModel):
    label: str
    hint: str = ""

    @field_validator("label")
    @classmethod
    def _label_required(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("step label is required")
        return v

    @field_validator("hint", mode="before")
    @classmethod
    def _clean_hint(cls, v):
        return (v or "").strip()


class Framework(BaseModel):
    name: str
    why: str
    steps: list[Step]
    focus: str = ""
    total: Optional[int] = None

    @field_validator("name", "why")
    @classmethod
    def _required(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("field is required")
        return v

    @field_validator("focus", mode="before")
    @classmethod
    def _clean_focus(cls, v):
        return (v or "").strip()

    @field_validator("steps")
    @classmethod
    def _enough_steps(cls, v):
        if len(v) < MIN_STEPS:
            raise ValueError(f"a framework needs at least {MIN_STEPS} steps")
        return v[:MAX_STEPS]

    @field_validator("total", mode="before")
    @classmethod
    def _lenient_total(cls, v):
        # The model sometimes writes "5 questions" or "four" — don't discard a good
        # framework over it; drop to None and let _finalize derive the real number.
        try:
            return int(v)
        except (TypeError, ValueError):
            return None


def _finalize(fw_model, intent):
    """Turn a validated Framework into the plain dict the rest of the module uses,
    deriving `total` (never trust the model's number blindly) and a `focus`."""
    fw = fw_model.model_dump()
    steps = fw["steps"]
    total = fw.get("total")
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = len(steps) + 1
    fw["total"] = max(MIN_TOTAL, min(MAX_TOTAL, max(total, len(steps))))
    fw["focus"] = fw.get("focus") or intent
    return fw


# ---------------------------------------------------------------------------
# Prompt building — a shared framework-spec fragment both entry points reuse, so
# the "design a résumé-fit framework" instructions live in exactly one place.
# ---------------------------------------------------------------------------
def _experience_line(resume_data):
    rd = resume_data or {}
    level = (rd.get("experience_level") or "").strip()
    if not level:
        return ""
    return (
        f"\nTHEIR EXPERIENCE LEVEL: {level}. Pitch the framework to this — a fresher's "
        "structure should lean on learning, curiosity and their specific contribution; "
        "an experienced person's should lean on trade-offs, scale, impact and judgement."
    )


def _framework_spec(track, brief, resume_data):
    intent = TRACK_BLUEPRINTS[track]["intent"]
    return f"""You are an expert interview and communication coach. Design a SHORT spoken-answer
framework that THIS specific person can use for: {intent}.

Adjust it to who they actually are — their field/domain, their target role, their
projects and their experience level. Two different people should get two different
frameworks. Do NOT default to a textbook acronym unless it genuinely fits them.
{_experience_line(resume_data)}

PRIVATE PROFILE (for your eyes only — shape the framework to it, never quote or reveal it):
{brief}

Framework rules:
  - "name": a short, memorable name for the structure — an acronym or a "A · B · C"
    style label of the beats. Keep it under ~5 words.
  - "why": ONE plain-English sentence on why this structure works for a person like them.
  - "steps": {MIN_STEPS}-{MAX_STEPS} beats, in the order they'd speak them. Each has a
    "label" (1-3 words) and a "hint" (one short, concrete nudge). Where natural, aim the
    hint at their actual domain — but keep it general enough that it never leaks private facts.
  - "focus": one sentence naming the single skill this exercise builds.
  - "total": how many practice questions to ask (between {MIN_TOTAL} and {MAX_TOTAL})."""


_FRAMEWORK_JSON = (
    '"framework": {"name": "...", "why": "...", '
    '"steps": [{"label": "...", "hint": "..."}], "focus": "...", "total": <int>}'
)


def generate_lesson(track, brief, resume_data=None):
    """For the framework/teach screen: one Groq call → the résumé-tailored framework
    PLUS a model example grounded in their real background. Raises
    SoftSkillGenerationError on a genuine failure."""
    if not is_track(track):
        raise SoftSkillGenerationError(f"Unknown track: {track}")

    prompt = _framework_spec(track, brief, resume_data) + f"""

Also write "example": ONE short model answer (2-3 sentences, simple plain English) that
follows the framework you just designed and fits this person — without mentioning or
quoting their private profile.

Return ONLY this JSON, no markdown:
{{{_FRAMEWORK_JSON}, "example": "..."}}"""

    data = _call(prompt, max_tokens=650, temperature=0.5, label="generate_lesson")
    fw = _validate(data.get("framework"), track)
    example = _clean_text(data.get("example"))
    return {"framework": fw, "example": example or _default_example(fw)}


def generate_opening(track, brief, resume_data=None, project_ctx=""):
    """For the start of a live session: one Groq call → the résumé-tailored framework
    PLUS the first practice question (difficulty is set by the caller). The framework
    is returned so the caller can persist it for the whole session."""
    if not is_track(track):
        raise SoftSkillGenerationError(f"Unknown track: {track}")

    extra = f"\n{project_ctx}" if project_ctx else ""
    prompt = _framework_spec(track, brief, resume_data) + f"""{extra}

Then ask the FIRST practice question only — an easy opener (difficulty 2 of 5) that starts
the person working through the framework you designed. Phrase it naturally; never quote the
private profile.

Return ONLY this JSON, no markdown:
{{{_FRAMEWORK_JSON}, "first_question": "the opening question", "hint": "one short tip"}}"""

    data = _call(prompt, max_tokens=700, temperature=0.5, label="generate_opening")
    fw = _validate(data.get("framework"), track)
    return {
        "framework": fw,
        "first_question": _clean_text(data.get("first_question")),
        "hint": _clean_text(data.get("hint")),
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _call(prompt, max_tokens, temperature, label="unknown"):
    try:
        data = groq_json(prompt, max_tokens=max_tokens, temperature=temperature, label=label)
    except GroqError as e:
        print("Soft-skill framework generation error:", e)
        raise SoftSkillGenerationError("Aira couldn't design your framework right now.")
    if not isinstance(data, dict):
        raise SoftSkillGenerationError("Malformed framework response.")
    return data


def _validate(raw, track):
    try:
        model = Framework.model_validate(raw or {})
    except ValidationError as e:
        # Metrics only (see llm_schemas.describe_validation_error): the framework
        # is generated from the candidate's experience level and project context,
        # so the rejected value is resume-derived content.
        print(f"Soft-skill framework validation failed ({describe_validation_error(e)})")
        raise SoftSkillGenerationError("Aira couldn't design your framework right now.")
    return _finalize(model, TRACK_BLUEPRINTS[track]["intent"])


def _clean_text(v):
    return (v or "").strip() if isinstance(v, str) else ""


def _default_example(fw):
    first = fw["steps"][0]["label"].lower() if fw.get("steps") else "your main point"
    return f"Open with {first}, keep it specific with one real example, and close on why it matters."


# ---------------------------------------------------------------------------
# Skeleton — resilience only. Used when the model is genuinely unreachable so the
# feature degrades instead of 500-ing; it is NOT the product path. Built from the
# track's intent, so it still reads as that track rather than a generic stub.
# ---------------------------------------------------------------------------
def fallback_framework(track):
    label = track_label(track)
    intent = (TRACK_BLUEPRINTS.get(track) or {}).get("intent", "speaking clearly under pressure")
    return {
        "name": label,
        "why": f"A simple order for {intent} — open, support, close.",
        "steps": [
            {"label": "Open", "hint": "start with your single main point"},
            {"label": "Support", "hint": "back it up with one real, concrete example"},
            {"label": "Close", "hint": "end on why it matters"},
        ],
        "focus": intent,
        "total": 4,
    }
