"""Resume agent: turn an uploaded resume into structured signal + persona questions.

Two steps, mirroring the existing Groq-call pattern used elsewhere in the app:

1. extract_text()  — pull raw text from the uploaded PDF/DOCX (mechanical, no AI).
2. parse_resume()  — one Groq call that returns a structured profile AND four
   persona questions grounded in the resume:
     - 2 technical/project questions (free-text "recall") that probe whether the
       candidate actually understands the projects they list, and
     - 2 HR/behavioral multiple-choice questions ("situational") with options.

   Every generated question is tagged with the persona dimension it probes, so it
   feeds the same scoring machinery as the fixed question bank. The user never sees
   that these came from their resume — they're returned in the same shape as the
   fixed questions and blended into the quiz server-side.
"""
import io
import re

from pydantic import BaseModel, Field, field_validator, model_validator, ValidationError

from groq_client import groq_json, GroqError, GROQ_API_KEY
from llm_schemas import (
    PERSONA_DIMENSIONS,
    Option,
    describe_validation_error,
    normalize_dimension,
    normalize_options,
    require_non_blank,
)

# Dimensions the generated questions are allowed to probe (must match
# session_controller.DIMENSION_ORDER — both now derive from llm_schemas).
ALLOWED_DIMENSIONS = list(PERSONA_DIMENSIONS)


class ResumeError(Exception):
    """Raised when the resume can't be read or contains no usable text."""


# ---------------------------------------------------------------------------
# Step 1 — text extraction
# ---------------------------------------------------------------------------

def extract_text(file_storage):
    """Extract raw text from a Werkzeug FileStorage (PDF or DOCX).

    Raises ResumeError on unsupported types or empty/garbled content.
    """
    filename = (file_storage.filename or "").lower()
    raw = file_storage.read()
    if not raw:
        raise ResumeError("The uploaded file is empty.")

    if filename.endswith(".pdf"):
        text = _extract_pdf(raw)
    elif filename.endswith(".docx"):
        text = _extract_docx(raw)
    else:
        raise ResumeError("Please upload your resume as a PDF or DOCX file.")

    text = (text or "").strip()
    if len(text) < 50:
        # Logged, not just raised — the raw file itself is never persisted
        # (extract_text only ever sees it in-memory for this one request), so
        # this line is the ONLY record of what actually went wrong once the
        # request ends. Without it, a report of "resume upload failed" leaves
        # nothing to look at afterward.
        #
        # Metrics only: the filename used to be logged verbatim, and resumes are
        # overwhelmingly named after their owner ("Priya_Sharma_Resume.pdf"),
        # which put candidate names in the Render logs. The extension is the
        # only part with diagnostic value.
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "(none)"
        print(
            f"Resume text extraction failed: filetype={ext} "
            f"size={len(raw)}b final_extracted_chars={len(text)}"
        )
        raise ResumeError(
            "This looks like a photo or scan of your resume — it has no readable "
            "text inside, only an image, so we can't analyse it. Please upload a "
            "text-based file: export your resume as a PDF (or DOCX) straight from "
            "Word, Google Docs, or Canva — not a photo, screenshot, or scanned copy. "
            "Tip: if you can highlight and copy the text in your file, it'll work."
        )
    return text


def _extract_pdf(raw):
    """pypdf is fast but chokes on a real (non-scanned) chunk of PDFs — anything
    with embedded/subset fonts that lack a proper ToUnicode map (common from
    Canva, LinkedIn's "export as PDF", and some Word→PDF converters) comes back
    empty or garbled even though the file is genuinely text-based, not scanned.
    pdfminer.six uses a different extraction path and recovers text from most
    of those, so it's a fallback rather than the primary parser: it's slower
    and a bit heavier, not worth paying for the common case that already works.
    """
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    pages = reader.pages
    text = "\n".join((page.extract_text() or "") for page in pages)
    pypdf_chars = len((text or "").strip())
    if pypdf_chars >= 50:
        return text

    print(f"pypdf got only {pypdf_chars} chars from {len(pages)} page(s) — trying pdfminer.six")
    from pdfminer.high_level import extract_text as _pdfminer_extract_text
    try:
        fallback_text = _pdfminer_extract_text(io.BytesIO(raw))
        print(f"pdfminer.six got {len((fallback_text or '').strip())} chars")
        return fallback_text
    except Exception as e:
        print(f"pdfminer.six raised {type(e).__name__}: {e}")
        return text


def _extract_docx(raw):
    import docx
    document = docx.Document(io.BytesIO(raw))
    return "\n".join(p.text for p in document.paragraphs)


# ---------------------------------------------------------------------------
# Step 1.5 — narrow the text before it ever reaches the LLM
#
# The assessment only needs what the candidate BUILT and what they can DO:
# projects, technical skills, soft skills. Their name, phone, email, college,
# degree and employer names add nothing to a work-style persona, so there is no
# reason to hand them to a third-party model. Everything below runs locally.
# ---------------------------------------------------------------------------

# A heading whose section we keep. Matched by CONTAINMENT and checked BEFORE the
# denylist, because real headings are overwhelmingly "<qualifier> <keyword>":
# "Personal Projects", "Academic Projects", "Computer Skills", "Key Skills".
#
# These were previously matched as a prefix, which broke on every one of those
# forms — and worse, "Personal Projects"/"Academic Projects" then fell through to
# the denylist and were DISCARDED on "personal"/"academic". Losing the projects
# section empties tier 1, which drops the whole resume to the tier-3 fallback, so
# a too-narrow allowlist leaked MORE than a generous one. Hence containment, and
# hence keep-wins: a combined "Internships and Projects" is kept rather than
# risking the entire document going through unfiltered.
_KEEP_HEADINGS = (
    "project", "skill", "technolog", "tech stack", "competenc",
    "expertise", "proficienc", "toolkit", "strength", "portfolio",
)

# A heading whose section is dropped outright. Identity, education and employer
# history — the bulk of the PII and none of the signal.
_DROP_HEADINGS = (
    "education", "academic", "qualification", "school", "college", "university",
    "experience", "employment", "work history", "career history", "internship",
    "personal", "contact", "objective", "profile summary", "summary", "reference",
    "hobb", "interest", "declaration", "language", "address", "certification",
    "award", "achievement", "publication", "activities", "volunteer", "extracurricular",
)

# Bump whenever the filter's OUTPUT changes for the same input — a widened
# heading list, a new redaction pattern, a tier-logic fix. Stored per row as
# Persona.resume_sanitizer_version so a later improvement can be re-applied to
# exactly the rows that predate it (scrub_resume_text.py --below N) instead of
# rewriting the whole table blindly.
#   0 = never sanitized (raw text, predates this feature)
#   1 = allowlist/denylist sections + PII redaction, 3-tier fallback
#   2 = allowlist matched by containment instead of prefix — v1 missed (and in
#       the "Personal/Academic Projects" case discarded) the most common real
#       heading forms, forcing ~29% of resumes onto the wider tier-2/3 fallback
SANITIZER_VERSION = 2

_HEADING_MAX_CHARS = 60

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_HANDLE_RE = re.compile(r"\b(?:linkedin|github|gitlab|leetcode|twitter|x)\.com/\S*", re.I)
# 10+ digits allowing spaces/dashes/parens — phone numbers, not "Python 3.11".
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(?\d[\)\s.-]?){9,}\d")
_LONG_NUM_RE = re.compile(r"\b\d{6,}\b")


def _redact_pii(text):
    """Strip direct identifiers wherever they appear — they also turn up inside
    kept sections (a GitHub link under a project, an email in a footer)."""
    for pattern in (_EMAIL_RE, _HANDLE_RE, _URL_RE, _PHONE_RE, _LONG_NUM_RE):
        text = pattern.sub(" ", text)
    return text


def _classify_heading(line):
    """'keep' / 'drop' / 'unknown' for a section heading, or None for a content line.

    'unknown' matters as much as the other two: an unrecognised heading ENDS the
    previous section. Without it a denylisted section swallows everything that
    follows it, and an allowlisted one over-collects whatever comes next.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > _HEADING_MAX_CHARS:
        return None
    # Normalize "TECHNICAL SKILLS:" / "Technical Skills" / "• Projects" alike.
    norm = re.sub(r"[^a-z0-9 ]+", " ", stripped.lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    if not norm:
        return None

    # Strong signal: shouted or colon-terminated. Weak signal: merely short —
    # enough to confirm a keyword match, but NOT enough to declare an unknown
    # heading, because short content lines (a project's name on its own line)
    # look exactly like one and must not reset the section.
    strong = stripped.isupper() or stripped.endswith(":")
    if not (strong or len(norm.split()) <= 4):
        return None
    # Keep is checked first and wins outright — see the note on _KEEP_HEADINGS.
    if any(k in norm for k in _KEEP_HEADINGS):
        return "keep"
    if any(k in norm for k in _DROP_HEADINGS):
        return "drop"
    return "unknown" if strong else None


def sanitize_resume_text(text):
    """Reduce a raw resume to the parts the assessment actually uses.

    Three tiers, so tightening privacy can never make onboarding fail where it
    used to succeed:
      1. Allowlisted sections found -> send only those.
      2. Otherwise -> send everything except denylisted sections.
      3. Nothing survives (an unstructured resume) -> send the whole thing.
    PII is redacted in all three cases, so the raw text never leaves the server
    intact regardless of which tier applies.
    """
    text = _redact_pii(text or "")

    kept, other, mode = [], [], None
    for line in text.splitlines():
        verdict = _classify_heading(line)
        if verdict is not None:
            # 'unknown' closes the current section without opening a new one, so
            # its content lands in the neutral bucket instead of inheriting the
            # previous verdict. The heading line itself is never emitted for
            # unknown/drop — an unlabelled shouted line is often the name banner.
            mode = None if verdict == "unknown" else verdict
            if verdict == "keep":
                kept.append(line.strip())
            continue
        if not line.strip():
            continue
        if mode == "keep":
            kept.append(line.strip())
        elif mode != "drop":
            other.append(line.strip())

    for tier in (kept, other, text.splitlines()):
        body = "\n".join(l.strip() for l in tier if l.strip())
        if len(body) >= 50:
            if tier is not kept:
                print("Resume sanitizer: no projects/skills section detected — "
                      f"falling back to a wider slice ({len(body)} chars, PII redacted)")
            return body
    return "\n".join(l.strip() for l in text.splitlines() if l.strip())


# ---------------------------------------------------------------------------
# Step 2 — the agent (single Groq call)
# ---------------------------------------------------------------------------

def _build_prompt(resume_text, onboarding):
    onboarding = onboarding or {}
    context = ", ".join(
        f"{k}={v}" for k, v in onboarding.items() if v
    ) or "none provided"
    dims = ", ".join(ALLOWED_DIMENSIONS)
    # Narrowed BEFORE truncation, so the 6000-char budget is spent on projects
    # and skills instead of being eaten by a contact block and a degree table.
    filtered = sanitize_resume_text(resume_text)[:6000]

    return f"""You are a career assessment expert analysing a candidate's resume to
build a hidden professional persona. The candidate will NEVER see your analysis or
which trait each question targets.

ONBOARDING CONTEXT: {context}

RESUME (already reduced to projects and skills — identity, education and employer
details have been deliberately removed and are NOT needed):
\"\"\"
{filtered}
\"\"\"

Never infer, guess or output the candidate's name, contact details, school,
college, degree, marks, or employer names. If a fragment of one survives in the
text above, ignore it. Judge only what they built and what they can do.

Do two things and return them as ONE JSON object.

1) Extract a structured profile:
   - skills: array of concrete TECHNICAL skills/technologies actually evidenced
     (languages, frameworks, databases, tools)
   - soft_skills: array of interpersonal/working skills actually evidenced
     (e.g. mentoring, presenting, coordinating) — [] if the resume shows none
   - projects: array of {{"name","what_they_did"}} for their most substantial projects
   - experience_level: one of "student", "fresher", "intern", "experienced" — infer
     it from the onboarding context and the scale of their projects
   - likely_gaps: array of areas they appear weak in or that are missing for their goal

2) Write FOUR persona questions grounded in THIS resume. Phrase them the way a real
   interviewer would — natural and conversational. The candidate must never be able to
   tell which persona trait each question is secretly measuring.
   - TWO "technical" questions: free-text, about their OWN specific projects/skills,
     probing whether they genuinely understand what they built (depth, decisions,
     trade-offs). These validate resume claims — it's fine (and expected) that these
     reference their actual work, just like a real interviewer asking about it.
   - TWO "hr" questions: multiple-choice behavioural/HR-interview style (teamwork,
     conflict, motivation, ownership) with exactly 4 options each.

   Tag every question with the persona dimension it best probes. Allowed dimensions:
   {dims}.

Return ONLY this JSON, no markdown, no commentary:
{{
  "skills": ["..."],
  "soft_skills": ["..."],
  "projects": [{{"name": "...", "what_they_did": "..."}}],
  "experience_level": "student|fresher|intern|experienced",
  "likely_gaps": ["..."],
  "technical_questions": [
    {{"dimension": "<one allowed dimension>", "text": "free-text question about their project"}},
    {{"dimension": "<one allowed dimension>", "text": "free-text question about a skill they listed"}}
  ],
  "hr_questions": [
    {{"dimension": "<one allowed dimension>", "text": "behavioural question",
      "options": [
        {{"label": "A", "text": "...", "signal": "what choosing this reveals"}},
        {{"label": "B", "text": "...", "signal": "..."}},
        {{"label": "C", "text": "...", "signal": "..."}},
        {{"label": "D", "text": "...", "signal": "..."}}
      ]}},
    {{"dimension": "<one allowed dimension>", "text": "behavioural question",
      "options": [
        {{"label": "A", "text": "...", "signal": "..."}},
        {{"label": "B", "text": "...", "signal": "..."}},
        {{"label": "C", "text": "...", "signal": "..."}},
        {{"label": "D", "text": "...", "signal": "..."}}
      ]}}
  ]
}}"""


def parse_resume(resume_text, onboarding=None):
    """Single Groq call → structured profile + 4 tagged questions.

    Returns the parsed dict. Raises ResumeError on a genuine LLM/parse failure so the
    caller can surface a retryable error instead of silently storing junk.
    """
    if not GROQ_API_KEY:
        raise ResumeError("Resume analysis is unavailable right now (missing API key).")

    prompt = _build_prompt(resume_text, onboarding)
    try:
        # json_mode off: this prompt asks for a JSON object with nested arrays and the
        # historical behaviour relied on regex extraction rather than response_format.
        data = groq_json(prompt, max_tokens=1500, temperature=0.4, json_mode=False, label="parse_resume")
    except GroqError as e:
        print("Resume agent error:", e)
        if e.rate_limited:
            raise ResumeError(
                "Aira is handling a lot of resumes right now — please try again "
                "in a few minutes."
            )
        raise ResumeError("Couldn't analyse the resume. Please try again.")

    if not isinstance(data, dict):
        raise ResumeError("Resume analysis came back incomplete. Please try again.")
    return _normalize(data)


def _validated_dimension(cls, v):
    """Resolve the tag to a real dimension, or reject the question.

    This used to default anything unrecognised to "problem_solving_behavior".
    That was silent and it polluted scoring twice over: the mis-tagged question's
    evidence was credited to a dimension it was never about, and the dimension it
    SHOULD have covered was left with no question at all — yet the persona still
    scores all 8, so the model invented a level and note for it. Aliases are
    normalized; anything genuinely unresolvable fails validation so the batch is
    retried instead of quietly stored.
    """
    dim = normalize_dimension(v)
    if dim is None:
        raise ValueError(f"unrecognised persona dimension: {v!r}")
    return dim


class TechnicalQuestion(BaseModel):
    # Required, not defaulted: a `mode="before"` validator never runs for an
    # absent field, so a default here would let an untagged question through
    # under exactly the remapping this validator exists to prevent.
    dimension: str
    text: str

    _dim = field_validator("dimension", mode="before")(classmethod(_validated_dimension))
    _text = field_validator("text")(classmethod(lambda cls, v: require_non_blank(v)))


class HRQuestion(BaseModel):
    dimension: str
    text: str
    options: list[Option] = Field(default_factory=list)

    _dim = field_validator("dimension", mode="before")(classmethod(_validated_dimension))
    _text = field_validator("text")(classmethod(lambda cls, v: require_non_blank(v)))

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_opts(cls, v):
        return normalize_options(v)

    @model_validator(mode="after")
    def _min_two_options(self):
        if len(self.options) < 2:
            raise ValueError("hr_question needs at least 2 usable options")
        return self


class ResumeProfile(BaseModel):
    # `skills` stays the TECHNICAL list under its original name: it already meant
    # that, and four downstream readers plus every stored resume_data row key on
    # it. soft_skills is added alongside rather than renaming the pair.
    skills: list = Field(default_factory=list)
    soft_skills: list = Field(default_factory=list)
    projects: list = Field(default_factory=list)
    experience_level: str = "student"
    likely_gaps: list = Field(default_factory=list)
    technical_questions: list[TechnicalQuestion]
    hr_questions: list[HRQuestion]

    @field_validator("skills", "soft_skills", "projects", "likely_gaps", mode="before")
    @classmethod
    def _default_empty_list(cls, v):
        return v or []

    @field_validator("experience_level", mode="before")
    @classmethod
    def _default_experience_level(cls, v):
        return v or "student"

    @field_validator("technical_questions", mode="before")
    @classmethod
    def _need_2_tech(cls, v):
        v = v or []
        if len(v) < 2:
            raise ValueError("need at least 2 technical_questions")
        return v[:2]

    @field_validator("hr_questions", mode="before")
    @classmethod
    def _need_2_hr(cls, v):
        v = v or []
        if len(v) < 2:
            raise ValueError("need at least 2 hr_questions")
        return v[:2]


def _normalize(data):
    """Validate/repair the agent output so downstream code can trust its shape."""
    try:
        profile = ResumeProfile.model_validate(data)
    except ValidationError as e:
        # Metrics only (see llm_schemas.describe_validation_error). This path got
        # stricter when unknown dimensions started being rejected instead of
        # remapped, so without a trace of WHY, a spike in onboarding failures
        # would be undiagnosable — but the exception's own text would carry
        # resume-derived content into the logs.
        print(f"Resume profile failed validation ({describe_validation_error(e)})")
        raise ResumeError("Resume analysis came back incomplete. Please try again.")
    return profile.model_dump()


def build_resume_questions(resume_data):
    """Turn stored resume_data into question objects matching the fixed-bank shape.

    Output IDs are prefixed 'rz-' so they're traceable server-side, but the shape is
    identical to question_bank entries — the frontend (and the user) can't distinguish
    them from the fixed questions.
    """
    questions = []
    for i, q in enumerate(resume_data.get("technical_questions", [])):
        questions.append({
            "id": f"rz-tech-{i+1}",
            "dimension": q["dimension"],
            "type": "recall",
            "text": q["text"],
            "options": None,
        })
    for i, q in enumerate(resume_data.get("hr_questions", [])):
        questions.append({
            "id": f"rz-hr-{i+1}",
            "dimension": q["dimension"],
            "type": "situational",
            "text": q["text"],
            "options": q["options"],
        })
    return questions
