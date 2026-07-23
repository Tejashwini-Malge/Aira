"""Shared Pydantic building blocks for validating raw Groq JSON responses.

Three pieces of logic used to be duplicated verbatim across resume_agent.py
and question_agent.py: the 8-dimension list, the option-normalization rule
(a missing "label" defaults to A/B/C/D by the option's position in the
ORIGINAL, pre-filter list — not its position after dropping blank options),
and the blank-question-text rejection rule. All three live here now so the
"must match across files" risk is gone.
"""
from pydantic import BaseModel

PERSONA_DIMENSIONS = (
    "work_culture_preferences",
    "teamwork_style",
    "leadership_tendencies",
    "decision_making_approach",
    "problem_solving_behavior",
    "professional_values",
    "career_goals",
    "communication_style",
)


class Option(BaseModel):
    """A pre-sanitized MCQ option. Callers must run raw option lists through
    normalize_options() first — this model does no defaulting itself."""
    label: str
    text: str
    signal: str = ""


def normalize_options(raw):
    """Drop options with blank/missing text; default a missing label to
    A/B/C/D by the option's position in the ORIGINAL (pre-filter) list."""
    kept = []
    for j, o in enumerate(raw or []):
        if not isinstance(o, dict):
            continue
        text = (o.get("text") or "").strip()
        if not text:
            continue
        kept.append({
            "label": o.get("label") or chr(65 + j),
            "text": text,
            "signal": (o.get("signal") or "").strip(),
        })
    return kept


def require_non_blank(v, message="question text is required"):
    """Strip and reject a blank/missing question text field. Meant to be
    called from a Pydantic @field_validator in the calling module."""
    v = (v or "").strip()
    if not v:
        raise ValueError(message)
    return v
