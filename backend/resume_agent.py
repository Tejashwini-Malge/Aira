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

from groq_client import groq_json, GroqError, GROQ_API_KEY

# Dimensions the generated questions are allowed to probe (must match
# session_controller.DIMENSION_ORDER).
ALLOWED_DIMENSIONS = [
    "work_culture_preferences",
    "teamwork_style",
    "leadership_tendencies",
    "decision_making_approach",
    "problem_solving_behavior",
    "professional_values",
    "career_goals",
    "communication_style",
]


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
        raise ResumeError(
            "Couldn't read enough text from that resume — if it's a scanned image, "
            "please upload a text-based PDF or a DOCX instead."
        )
    return text


def _extract_pdf(raw):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(raw):
    import docx
    document = docx.Document(io.BytesIO(raw))
    return "\n".join(p.text for p in document.paragraphs)


# ---------------------------------------------------------------------------
# Step 2 — the agent (single Groq call)
# ---------------------------------------------------------------------------

def _build_prompt(resume_text, onboarding):
    onboarding = onboarding or {}
    context = ", ".join(
        f"{k}={v}" for k, v in onboarding.items() if v
    ) or "none provided"
    dims = ", ".join(ALLOWED_DIMENSIONS)

    return f"""You are a career assessment expert analysing a candidate's resume to
build a hidden professional persona. The candidate will NEVER see your analysis or
which trait each question targets.

ONBOARDING CONTEXT: {context}

RESUME:
\"\"\"
{resume_text[:6000]}
\"\"\"

Do two things and return them as ONE JSON object.

1) Extract a structured profile:
   - skills: array of concrete skills/technologies actually evidenced in the resume
   - projects: array of {{"name","what_they_did"}} for their most substantial projects
   - experience_level: one of "student", "fresher", "intern", "experienced"
   - likely_gaps: array of areas they appear weak in or that are missing for their goal

2) Write FOUR persona questions grounded in THIS resume. The candidate must not be
   able to tell these came from their resume, so phrase them naturally.
   - TWO "technical" questions: free-text, about their OWN specific projects/skills,
     probing whether they genuinely understand what they built (depth, decisions,
     trade-offs). These validate resume claims.
   - TWO "hr" questions: multiple-choice behavioural/HR-interview style (teamwork,
     conflict, motivation, ownership) with 3 options each.

   Tag every question with the persona dimension it best probes. Allowed dimensions:
   {dims}.

Return ONLY this JSON, no markdown, no commentary:
{{
  "skills": ["..."],
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
        {{"label": "C", "text": "...", "signal": "..."}}
      ]}},
    {{"dimension": "<one allowed dimension>", "text": "behavioural question",
      "options": [
        {{"label": "A", "text": "...", "signal": "..."}},
        {{"label": "B", "text": "...", "signal": "..."}},
        {{"label": "C", "text": "...", "signal": "..."}}
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
        data = groq_json(prompt, max_tokens=1500, temperature=0.4, json_mode=False)
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


def _normalize(data):
    """Validate/repair the agent output so downstream code can trust its shape."""
    tech = data.get("technical_questions") or []
    hr = data.get("hr_questions") or []
    if len(tech) < 2 or len(hr) < 2:
        raise ResumeError("Resume analysis came back incomplete. Please try again.")

    def _dim(q):
        d = q.get("dimension")
        return d if d in ALLOWED_DIMENSIONS else "problem_solving_behavior"

    technical_questions = [
        {"dimension": _dim(q), "text": (q.get("text") or "").strip()}
        for q in tech[:2]
    ]
    hr_questions = []
    for i, q in enumerate(hr[:2]):
        opts = q.get("options") or []
        norm_opts = [
            {
                "label": o.get("label") or chr(65 + j),
                "text": (o.get("text") or "").strip(),
                "signal": (o.get("signal") or "").strip(),
            }
            for j, o in enumerate(opts) if (o.get("text") or "").strip()
        ]
        if len(norm_opts) < 2:
            raise ResumeError("Resume analysis came back incomplete. Please try again.")
        hr_questions.append({
            "dimension": _dim(q),
            "text": (q.get("text") or "").strip(),
            "options": norm_opts,
        })

    if any(not q["text"] for q in technical_questions + hr_questions):
        raise ResumeError("Resume analysis came back incomplete. Please try again.")

    return {
        "skills": data.get("skills") or [],
        "projects": data.get("projects") or [],
        "experience_level": data.get("experience_level") or "student",
        "likely_gaps": data.get("likely_gaps") or [],
        "technical_questions": technical_questions,
        "hr_questions": hr_questions,
    }


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
