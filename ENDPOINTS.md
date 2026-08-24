# Aira API Endpoints

## Public Endpoints (No Authentication Required)

### Static Files
- `GET /` → Serves `index.html`
- `GET /<filename>` → Serves HTML pages and assets (`/login` → `login.html`, etc.)

### Auth Endpoints
- `POST /signup` → Create new user account
  - Input: `{ name, email, password }` — password must be at least
    `account_bp.MIN_PASSWORD_LEN` (8) characters, the same floor the reset flow
    enforces, so an account can't be created below a length it can't be reset to.
  - Output: `{ success, message, id, name, email, onboarding_complete }` + session cookie
  - Status: 200 (success), 400 (validation / email already registered)

- `POST /login` → Authenticate existing user
  - Input: `{ email, password }`
  - Output: `{ success, name, email }` + session cookie
  - Status: 200 (success), 401 (invalid credentials)

- `POST /account/forgot-password` → Input `{ email }`. Always
  `200 { success: true, message }` with a **byte-identical body** whether or not
  the address is registered — the response is not an account-existence oracle.
  It returns **no token and no reset link**: until email is wired up there is no
  self-service reset. (It previously returned a working token for any registered
  address, which made knowing an email sufficient to take the account over.)

- `POST /account/reset-password` → Input `{ token, password }`. Verifies a signed
  token issued out of band (see `account_bp`'s docstring) and sets the password.
  Tokens expire after 1 hour and stop verifying the moment the password changes.
  Status: 200, 400 (missing fields / password too short / invalid or expired token).

---

## Protected Endpoints (Authentication Required)

All routes below return `401 { success: false, message: "Not authenticated" }`
without a valid session cookie.

### User & Session
- `GET /me` → Current user info: `{ user: { id, name, email, onboarding_complete } }`
- `POST /logout` → Clear session: `{ success: true }`

### Onboarding
- `GET /onboarding/status` → `{ complete, onboarding, resume }` — `resume` is a
  boolean "is one on file", never its contents. Now that the resume is optional,
  `complete` no longer implies the user has one.
- `POST /onboarding/save` → multipart form: career detail fields + an **optional**
  `resume` file (PDF/DOCX). With a resume it runs the resume agent and stores the
  analysis on the persona; without one it skips the parse entirely (no Groq call)
  and everything downstream takes its existing resume-less path — the assessment
  covers all 8 dimensions with generated questions instead of 5, and the persona
  prompt records "(no resume on file)". Output `{ success, resume: bool }`.
  400 if the resume is present but unreadable, or if onboarding is already
  complete (with a resume it costs a Groq call, so it runs once).
  The requirement was dropped because it was the heaviest possible first ask and
  the step users actually stopped at; `POST /me/resume` is the way back in.

- `POST /me/resume` → multipart form with a `resume` file. Adds or replaces the
  resume after onboarding. Costs a Groq call, so it carries save's rate limit.
  Beyond storing the analysis it also clears the cached `dimension_questions`
  (so the next `/session/get-questions` regenerates *with* the 4 resume-grounded
  questions) and, when a persona already exists, sets `resume_refresh_pending`
  so that locked persona becomes refreshable immediately instead of waiting out
  `SESSIONS_REQUIRED_TO_REFRESH`. A persona built with no resume at all is
  exactly the case the session threshold should not apply to.
  Output `{ success, persona_refresh_available }`. 400 before onboarding is
  complete, when no file is attached, or when the file is unreadable.
- `POST /onboarding/update` → form fields only, no resume, no LLM call. Edits the
  details captured at onboarding — a career stage changes, and `experience`/`goal`
  decide which scenarios every practice question is built from. Partial: only the
  fields present in the request are touched. Changing a routing field clears an
  unanswered cached question set so the next `/session/get-questions` regenerates
  at the new stage. → `{ success, onboarding }`. 400 before onboarding is complete
  or when nothing valid was submitted.

Both write paths run every field through `backend/onboarding_schema.py`, which
allowlists the field set, caps free text, and validates the dropdown values
against closed vocabularies. `users.onboarding` is flattened into the question
and persona prompts, so an unfiltered field here would be a prompt-injection
channel — any new write path into that column must use the same schema.

### Persona Assessment
The persona is **private**: its content is never returned to the client and never
accepted from it. Endpoints only ever confirm that it exists.

- `GET /session/get-questions` → `{ questions: [...] }` — 10 questions: 5 fixed
  scenarios + 4 resume-grounded + 1 open reflection, blended indistinguishably.
- `POST /session/save-answers` → Input `{ responses: [...] }`. 400 with an
  `unanswered` id list if any question lacks a real answer.
- `POST /session/generate-persona` → Builds the persona once from the saved
  answers (`force` re-runs it). Output: `{ persona: { ready: true } }` only.
  Status: 200, 400 (no/incomplete responses), 503 (LLM failure — retryable).
- `GET /me/persona` → Existence check for gating:
  `{ persona: { ready: true } }` or `{ persona: null }`.

### Mock Interview / Quiz
- `POST /quiz/generate` → Input `{ mode: "role"|"topic", topic?, type?, difficulty?, duration? }`.
  Output: `{ questions: [{id, question}], mode, topic, ...meta }`.
  The generated set is also **stored server-side** for this user; evaluation only
  ever grades that stored set.
- `POST /quiz/evaluate` → Input `{ answers: { "Q<id>": "..." } }` — answers only,
  keyed by question id. The server grades its stored questions and persists the
  result. Output: `{ feedback: { feedback, score, weak_areas, study_plan,
  suggestions, resources } }`.
  Status: 400 if `answers` is not an object or there is no stored question set
  (generate first). The stored set is cleared after evaluation.
- `GET /me/quizzes` → `{ quizzes: [...] }` quiz history.

### Communication Practice
- `GET /comm/setup?track=communication|intro|project` → framework, model example,
  and (for `project`) the user's resume projects.
- `POST /comm/start` → Input `{ track, project_id? }`. Output
  `{ track, label, beat: {id, prompt, hint, difficulty}, total }`. Starts a
  **server-held** session: all prompts are accumulated server-side.
- `POST /comm/next` → Input `{ answer }` — the latest answer only. The server
  records it against the beat it actually asked, adapts difficulty, and returns
  `{ done, reaction, level, beat }` (or `{ done: true }` after the last beat).
  400 if no active session.
- `POST /comm/evaluate` → Input `{ metrics?: {wpm, fillers, words}, answer? }`.
  Grades the server-held transcript, persists a SpeakingSession, clears the
  stored state. Output: `{ feedback: {...} }`. 400 if no active session.
- `POST /comm/redo` → Input `{ beat, attempt1, attempt2 }` → one-line improvement
  verdict (not persisted).

### Speaking
- `GET /me/speaking` → `{ sessions: [...] }`. Read-only — sessions are only ever
  written server-side by `/comm/evaluate`, so a client can never self-report a score.

### Reports
- `GET /me/report` → aggregated progress used by `report.html`:
  ```
  {
    user, onboarding,               // onboarding = { study, goal, target_role, target_industry, timeline, language, note }
    persona,                        // { summary, dimensions } or null — the report-page view of the persona
    quizzes, quiz_history,          // both the full quiz_results list (same data, two keys for frontend compatibility)
    speaking, speaking_history,     // both the full speaking_sessions list
    speaking_averages,              // { fluency, clarity, confidence } or null
    recurring_weak_areas            // [{ area, count }] — weak areas appearing more than once across quizzes + speaking
  }
  ```
- `POST /me/report/snapshot` → Archives the current report (same shape as above,
  plus a computed `overall_pct`/`overall_grade`/`result_text`) as a permanent,
  timestamped copy. Called when the user opens the PTM folder on the report page.
  Output: `{ success, snapshot: { id, created_at, overall_grade, overall_pct, result_text } }`.
- `GET /me/report/snapshots` → List of saved snapshots (summaries only, newest
  first): `{ snapshots: [{ id, created_at, overall_grade, overall_pct, result_text }] }`.
- `GET /me/report/snapshots/<id>` → One archived snapshot in full:
  `{ snapshot: {...summary}, payload: {...same shape as GET /me/report} }`.
  404 if the id doesn't belong to the logged-in user.

---

## Authentication Implementation

### Session-Based Auth
- Flask `flask.session` stores `user_id` in a signed, HttpOnly cookie
- All protected endpoints use the `@login_required` decorator
- Missing or invalid session cookie → 401 Unauthorized

### Cookie Configuration
```python
app.config["SESSION_COOKIE_HTTPONLY"] = True  # No JS access
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # Same-origin
```

### Frontend Credentials
All fetch requests to protected endpoints use `credentials: 'include'`:
```javascript
fetch(url, {
  method: 'POST/GET',
  credentials: 'include',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data)
})
```

---

## Anti-forgery Design

Question/prompt sets for `/quiz/*` and `/comm/*` are stored in the
`pending_assessments` table keyed to the logged-in user. Evaluation endpoints
grade only that stored set against client-sent answers and reject requests with
no active stored session (400). Clients can no longer post a fabricated
transcript and have it scored into their history.
