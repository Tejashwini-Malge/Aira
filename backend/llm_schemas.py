"""Shared Pydantic building blocks for validating raw Groq JSON responses.

Three pieces of logic used to be duplicated verbatim across resume_agent.py
and question_agent.py: the 8-dimension list, the option-normalization rule
(a missing "label" defaults to A/B/C/D by the option's position in the
ORIGINAL, pre-filter list — not its position after dropping blank options),
and the blank-question-text rejection rule. All three live here now so the
"must match across files" risk is gone.
"""
import re

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


# Near-misses the models actually return instead of the exact dimension key
# ("leadership", "problem solving", "Decision-Making"). These are resolved to the
# real dimension rather than defaulted, so a question keeps the meaning it was
# written with.
_DIMENSION_ALIASES = {
    "work_culture": "work_culture_preferences",
    "culture": "work_culture_preferences",
    "culture_fit": "work_culture_preferences",
    "work_environment": "work_culture_preferences",
    "teamwork": "teamwork_style",
    "collaboration": "teamwork_style",
    "team_style": "teamwork_style",
    "leadership": "leadership_tendencies",
    "ownership": "leadership_tendencies",
    "initiative": "leadership_tendencies",
    "decision_making": "decision_making_approach",
    "decisiveness": "decision_making_approach",
    "problem_solving": "problem_solving_behavior",
    "analytical_thinking": "problem_solving_behavior",
    "critical_thinking": "problem_solving_behavior",
    "values": "professional_values",
    "ethics": "professional_values",
    "integrity": "professional_values",
    "career": "career_goals",
    "goals": "career_goals",
    "ambition": "career_goals",
    "communication": "communication_style",
}


def normalize_dimension(value):
    """Resolve a model-supplied dimension label to one of PERSONA_DIMENSIONS,
    or None if it genuinely can't be resolved.

    Returning None rather than defaulting to a fixed dimension is the point: a
    silent remap credits one dimension with evidence that was never about it,
    and that shows up later as a confident level/note the answers never
    supported. Callers decide whether to drop the question or reject the batch.
    """
    if not isinstance(value, str):
        return None
    key = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if not key:
        return None
    if key in PERSONA_DIMENSIONS:
        return key
    if key in _DIMENSION_ALIASES:
        return _DIMENSION_ALIASES[key]
    # Last resort: an UNAMBIGUOUS partial match ("leadership_style" ->
    # leadership_tendencies). Ambiguous matches stay unresolved on purpose.
    matches = [d for d in PERSONA_DIMENSIONS if key in d or d in key]
    return matches[0] if len(matches) == 1 else None


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


def describe_validation_error(exc):
    """Render a pydantic ValidationError as field locations + error types only.

    str(ValidationError) embeds the offending INPUT VALUE. For these models that
    value is model-generated text derived from the candidate's resume, so
    logging the exception directly puts resume-derived content into the server
    logs. Logs get the shape of the failure; never the content.
    """
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('type', 'unknown')}")
    return "; ".join(parts) or "unknown validation error"


def require_non_blank(v, message="question text is required"):
    """Strip and reject a blank/missing question text field. Meant to be
    called from a Pydantic @field_validator in the calling module."""
    v = (v or "").strip()
    if not v:
        raise ValueError(message)
    return v
