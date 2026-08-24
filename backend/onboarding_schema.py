"""The accepted shape of the onboarding form — one place, server-side.

WHY THIS EXISTS. /onboarding/save used to store the form as-is:

    details = {k: v.strip() for k, v in request.form.items() if v and v.strip()}

Every key the client chose to send was persisted verbatim into `users.onboarding`,
and that blob is flattened into BOTH LLM prompts (question_agent._build_prompt's
CANDIDATE CONTEXT line and persona_agent._format_context). So an unrecognised
form field was an unbounded, attacker-controlled string with a direct path into a
model prompt — a prompt-injection channel and a storage-size hole in one. Nothing
downstream could defend against it, because by then the text is indistinguishable
from a real answer.

The rule here is allowlist-only: a field that isn't declared below never reaches
the database, and therefore never reaches a prompt. Adding a question to
onboarding.html means adding it here too — that coupling is deliberate and is
what keeps the boundary honest.

CHOICE fields are also the routing keys for candidate_profile, so they are
validated against their exact option set rather than merely length-capped: a
value outside the set is a client that has been tampered with, not a user with an
unusual answer, and is dropped rather than stored.
"""

# Free-text fields: kept, but capped. The caps are generous versus what the UI
# can realistically produce and exist to bound storage and prompt size, not to
# police the user's phrasing. `note` is the only field with real room, because
# it's the one users genuinely write prose into.
TEXT_FIELDS = {
    "study": 200,
    "target_role": 120,
    "target_industry": 120,
    "language": 120,
    "note": 1500,
}

# Closed vocabularies. These MUST stay in sync with the <option value="...">
# attributes in frontend/onboarding.html — test_onboarding_schema.py asserts it
# by parsing the HTML, so a drifted option fails the suite instead of silently
# dropping a user's answer on save.
CHOICE_FIELDS = {
    "goal": {
        "Placement / job preparation",
        "Building confidence",
        "Improving communication",
        "Interview or exam coming up",
        "Just exploring",
    },
    "timeline": {
        "Within a month",
        "In a few months",
        "Sometime this year",
    },
    # Self-declared stage. Replaces inferring seniority from the resume with an
    # LLM — see candidate_profile for why that inference was unreliable.
    # Two options, because Aira's audience is students and first-role seekers.
    # Options for experienced and career-changing candidates were removed on
    # 2026-08-07: nobody in the product was either.
    "experience": {
        "Still studying",
        "Graduated, looking for my first role",
    },
}

ALLOWED_FIELDS = set(TEXT_FIELDS) | set(CHOICE_FIELDS)

# What a field is allowed to contribute to an LLM PROMPT, as distinct from what
# it may be stored as. Storage caps bound the database; these bound the token
# bill, and they are tighter because the onboarding blob is rendered into three
# separate prompts (question generation, resume parsing, persona verdict) —
# every character here is paid for three times per user, on every regeneration.
#
# `note` is the field that matters: 1500 stored characters is roughly 375 tokens
# of free prose entering all three calls. The first couple of sentences carry the
# signal a prompt can actually use; the rest is the user thinking aloud. Trimming
# it here does not shorten what they wrote or what is shown back to them.
PROMPT_LIMITS = {"note": 400}


def prompt_context(onboarding):
    """The stored onboarding trimmed to what a prompt should carry.

    Returns the same keys with over-long values truncated — callers format them
    however their prompt reads (comma-joined, bulleted) rather than this module
    guessing at one shape for all three.
    """
    out = {}
    for key, value in (onboarding or {}).items():
        if value is None:
            continue
        # Strip BEFORE the emptiness check: a whitespace-only value is truthy, so
        # testing the raw value lets it through and it lands in the prompt as an
        # empty line — tokens spent teaching the model nothing.
        text = str(value).strip()
        if not text:
            continue
        limit = PROMPT_LIMITS.get(key)
        if limit and len(text) > limit:
            text = text[:limit].rstrip() + "…"
        out[key] = text
    return out


def clean_onboarding(form):
    """Return only the declared fields, trimmed, capped and vocabulary-checked.

    `form` is any mapping (werkzeug MultiDict or plain dict). Unknown keys are
    dropped rather than rejected — a client sending them is either stale or
    hostile, and neither case is worth a 400 that blocks a legitimate signup.
    Invalid CHOICE values are dropped the same way: downstream readers already
    treat every onboarding key as optional, so absence is a shape they handle,
    whereas a junk value is not.

    Dropping is quiet by design but must not be INVISIBLE. A discarded `experience`
    silently demotes that user to the inference fallback and changes every question
    they are asked, with nothing anywhere to say why. The observed "student|fresher"
    row sat in production unnoticed because nothing logged it. So each drop prints
    one line, matching the existing "[groq_usage] ..." convention.

    Log lines carry the field name and — for choice fields only — the rejected
    value, which is safe because it failed to match a closed vocabulary and is
    truncated regardless. Free-text values are NEVER logged: `note` and `study`
    hold user-written prose, and an over-length one is a capping event, not a
    security event.
    """
    cleaned = {}

    for key, limit in TEXT_FIELDS.items():
        value = (form.get(key) or "").strip()
        if value:
            if len(value) > limit:
                print(f"[onboarding] truncated field={key} "
                      f"from={len(value)} to={limit}")
            cleaned[key] = value[:limit]

    for key, allowed in CHOICE_FIELDS.items():
        value = (form.get(key) or "").strip()
        if value in allowed:
            cleaned[key] = value
        elif value:
            print(f"[onboarding] dropped field={key} value={value[:60]!r} "
                  f"reason=not-in-vocabulary")

    unknown = sorted(set(form.keys()) - ALLOWED_FIELDS - {"resume"})
    if unknown:
        print(f"[onboarding] dropped unknown fields={unknown}")

    return cleaned
