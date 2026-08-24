---
name: aira-context
description: Internal context pack for the Aira codebase — stack, architecture, LLM agents, data model, endpoints, invariants, deployment, and conventions. Load before working on any Aira backend or frontend file, when reasoning about the persona/quiz/communication pipelines, Groq usage, or when a session has lost the shape of the project.
---

# Aira — internal context pack

Single-file orientation for this repo. Read it before touching code so you don't
re-derive the architecture. Details that live better in code are pointed at, not
copied — where a file is named, it is the source of truth.

---

## 1. What Aira is

An AI career-prep companion for **students and freshers** (two bands, no
"experienced" tier — see §5). Styled end-to-end as an early-2000s Indian
classroom: green chalkboard, chalk type, a synthesized school bell, and a
90s-style report card at the end.

The product is one loop:

```
Enrol → Onboarding (+resume) → 10-question assessment → PRIVATE persona
      → Practice (Mock Interview | Soft Skills) → Report Card (archived as snapshots)
```

Everything personal flows from the persona, which the user never sees.

Scale reality: this is a small product (double-digit lifetime users). Don't
propose infrastructure sized for a bigger one.

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| Backend | Flask 3 + Flask-SQLAlchemy 2.0, blueprints |
| Auth | Flask signed-cookie sessions (`user_id`), `auth.py` helpers |
| DB | SQLite locally (`backend/aira.db`), managed Postgres in prod via `DATABASE_URL` |
| LLM | Groq chat completions API only, through `groq_client.py` |
| Validation | Pydantic v2 for every LLM response; `onboarding_schema.py` for form input |
| Rate limiting | Flask-Limiter, in-memory, keyed by user id (`rate_limiter.py`) |
| Resume parsing | `pypdf` / `pdfminer.six` / `python-docx` (mechanical, no AI) |
| Frontend | Plain HTML/CSS/JS. No framework, no build step, no bundler |
| Serving | The same Flask app serves `frontend/` — there is no separate frontend server, in dev or prod |
| Prod | Render: `gunicorn --chdir backend app:app`, gthread, 1 worker / 10 threads |
| Tests | pytest, `backend/tests/`, no network calls |

There is no migration tool. Schema changes go through
`models.migrate_new_columns(engine)`, which runs at boot after `db.create_all()`.

---

## 3. Repo map

```
backend/
  app.py                  Flask app factory-ish module scope: config, cookies, ProxyFix,
                          db init, usage sink wiring, blueprint registration, static serving,
                          /signup /login /logout /me
  models.py               All SQLAlchemy models + migrate_new_columns + pending-assessment helpers
  auth.py                 current_user(), @login_required
  rate_limiter.py         Shared deferred-init Limiter
  groq_client.py          THE only Groq transport. Model ladder, JSON parsing, usage logging
  usage_tracker.py        GroqUsage model + record(): daily per-model/per-feature token spend
  llm_schemas.py          8 persona dimensions, Option model, dimension aliasing, shared validators
  onboarding_schema.py    Allowlist + caps + closed vocabularies for the onboarding form
  candidate_profile.py    Deterministic student/fresher band → scenario setting + stakes rules
  persona_eligibility.py  Persona refresh threshold (3 sessions) — shared to dodge a circular import

  # agents (one Groq call each, Pydantic-validated, typed error)
  persona_agent.py        Builds the Core Persona across 8 dimensions
  question_agent.py       Generates dimension questions per user
  resume_agent.py         Extract → sanitize (PII redaction) → parse → 4 resume-grounded questions
  softskill_agent.py      Generates the speaking framework per user, per track

  # blueprints
  session_controller.py   /onboarding/* , /session/* , /me/persona
  persona_bp.py           /me/speaking , /me/report , /me/report/snapshot(s)
  ai_quiz_bp.py           /quiz/generate , /quiz/evaluate , /me/quizzes
  communication_bp.py     /comm/setup|quota|start|next|evaluate|redo
  account_bp.py           forgot/reset/change password (stateless itsdangerous tokens)
  feedback_bp.py          /feedback* — TEMPORARY validation instrument

  question_bank.json      Now only the 2 hardcoded open reflection questions still matter
  scrub_resume_text.py    One-off/repeatable PII re-scrub of personas.resume_text
  diagnose_resume_headings.py
  tests/

frontend/                 One HTML file per screen, no build
  assets/aira.js          window.Aira: api(), requireAuth(), progress(), ringBell(), decodePaperRef()
  assets/chalkboard.css   Shared theme + CSS variables
  assets/brand.js         Injects the brand SVG into .brand-badge

ENDPOINTS.md              Full endpoint contract — keep in sync with route changes
render.yaml               Blueprint: web service + env vars

# root-level operator scripts — read-only, pointed at PRODUCTION
db_url.py                 Shared DB URL resolution for the three scripts below
check_stats.py            Signups / funnel counts / today's Groq spend  (run_stats.bat)
export_feedback.py        Feedback table → CSV                          (export_feedback.bat)
export_feedback_to_sheets.py  Feedback table → Google Sheet, append-only (…_to_sheets.bat)
```

---

## 4. The LLM layer

### `groq_client.groq_json(prompt, max_tokens, temperature, json_mode, timeout, label, model)`

One function, every LLM call in the app. It:

- posts to `https://api.groq.com/openai/v1/chat/completions`
- uses `response_format=json_object` by default, with a regex fence-stripping fallback
- walks a **model ladder** on HTTP 429, because Groq's free tier is a
  tokens-per-day quota tracked **per model**:

  The ladder is **two tiers**, because the work splits in two — judgement calls
  (persona, evaluation) want the big models, extraction calls do not:

  | Tier | Models | Tokens/day | Why |
  |---|---|---|---|
  | `QUALITY_MODELS` | `openai/gpt-oss-120b` → `llama-3.3-70b-versatile` | 200k, 100k | judgement calls |
  | `CHEAP_MODELS` | `openai/gpt-oss-20b` → `llama-3.1-8b-instant` | 200k, 500k | extraction/formatting |
  | `FAST_MODEL` | `openai/gpt-oss-20b` | — | routed first for the three generation/extraction features below |

  Only three call sites pass `model=FAST_MODEL` — `parse_resume`,
  `generate_dimension_questions` and `generate_quiz`. Everything else (persona,
  `evaluate_quiz`, the three `comm_*` turns, the soft-skill lessons) takes the
  default quality-first ladder. The dividing line is **writing vs judging**:
  composing questions or reading a resume into fields is generation, grading
  someone's answers is not.

  `_ladder()` picks the tier order from where the call *starts*: a cheap call
  finishes the cheap tier before spending quality quota, so a burst of resume
  parses can't starve persona generation. A quality call still degrades into the
  cheap tier rather than failing. A caller's `model=` preference goes first but
  the rest of the ladder always applies, so routing cheap never makes a call
  *more* likely to fail.

  8b-instant sits **last**, not first, despite the deepest pool (500k/day): it is
  the reserve that's still there when everything else is dry. Note the separate
  **per-minute** ceiling (20b 8k TPM, 8b 6k TPM) — a ~2.3k-token resume parse
  means roughly 3 concurrent parses/min before 429s, which is a burst limit, not
  a daily one.
- sets `reasoning_effort="low"` — gpt-oss bills reasoning tokens against
  `max_tokens`, and callers cap as low as 200
- raises `GroqError(rate_limited=…)`. **Error policy stays with the caller**:
  `session_controller` → `PersonaGenerationError` (503, retryable),
  `ai_quiz_bp` → `None` fallback, `resume_agent` → `ResumeError`.
- emits a `[groq_usage]` line and calls the usage sink with `label` (the calling
  feature). `app.py` wires `usage_tracker.record` into that sink at startup —
  deliberately separate modules so `groq_client` stays importable with no Flask/DB.

`usage_tracker.GroqUsage` is one row per **(UTC day, model, label)** with
calls / prompt / completion / total tokens and `truncated_calls` (spend that
bought broken JSON — the clearest waste signal).

### Agent discipline (all four agents follow it)

1. Build a prompt from persona/resume/onboarding context.
2. One `groq_json` call with a token budget derived from the output size.
3. Validate with Pydantic; normalize near-miss dimension names via
   `llm_schemas.normalize_dimension`.
4. Raise a typed error — **except** `softskill_agent`, which returns a minimal
   `fallback_framework(track)` skeleton, because a framework outage must degrade
   gracefully rather than hard-block practice the way a missing persona does.

### The 8 persona dimensions (`llm_schemas.PERSONA_DIMENSIONS`)

`work_culture_preferences`, `teamwork_style`, `leadership_tendencies`,
`decision_making_approach`, `problem_solving_behavior`, `professional_values`,
`career_goals`, `communication_style`.

Every generated question is tagged with the dimension it probes, so resume
questions, generated questions and reflections all feed the same scoring path.

---

## 5. Candidate model (`candidate_profile.py`)

Two bands only: **student** and **fresher**. An `experienced` band and a
fresh/continuing/switching transition axis were removed on 2026-08-07 — no real
users occupied them, and rules written for segments with no users can't be
validated.

The band drives two *separately* defined things:

- `BAND_RULES` — **where** a scenario may be set
- `STAKES_RULES` — **how hard** it bites there

They're separate because they fail differently: a student handed a
budget-allocation scenario can't answer at all; a fresher handed a trivially
obvious choice answers correctly and reveals nothing.

**Declaration beats inference.** The onboarding answer wins.
`resume_data["experience_level"]` (LLM-written) proved unreliable in production
and survives only as a fallback for rows predating the onboarding question.

---

## 6. Data model (`models.py`)

| Table | Notes |
|---|---|
| `users` | + `onboarding` (JSON), `onboarding_complete`, `unlocked_tracks` (JSON) |
| `personas` | one per user. `title`, `summary`, `dimensions`, `raw_responses`, `dimension_questions`, `resume_text`, `resume_sanitizer_version`, `resume_data`, `session_count_at_generation` |
| `pending_assessments` | `(user_id, kind)` where kind ∈ `quiz` \| `comm`. Server-held in-flight state |
| `quiz_results` | topic, score, feedback, study_plan, weak_areas, suggestions, `paper_ref` |
| `speaking_sessions` | track, fluency/clarity/confidence/structure, summary, transcript |
| `report_snapshots` | full `/me/report` payload + overall_pct/grade/result_text |
| `feedback` | TEMPORARY validation instrument |
| `groq_usage` | see §4 |

`paper_ref` format (decoded client-side in `aira.js::decodePaperRef`, the single
decoding site): `AIRA-MMDD-TD` for role interviews (round Type + Difficulty),
`REQ-MMDD-XX` for topic practice (first 2 letters of topic).

---

## 7. Non-negotiable invariants

Break one of these and you've broken the product's trust model.

1. **The persona is private.** Never returned to the client, never accepted from
   it. Endpoints only ever confirm `{ ready: true }` or `null`. It is read
   server-side and used to steer prompts.
2. **Scores are never client-reported.** Quiz answers are graded against the
   exact question set the *server* stored and then cleared; speaking sessions
   are only ever written by `/comm/evaluate`. There is deliberately no POST on
   `/me/speaking`.
3. **Server-held session state.** `/comm/start` generates and persists the
   framework; `/comm/next` sends only the latest answer; the server records it
   against the beat it actually asked. The client never sends prompts back.
4. **All onboarding writes go through `onboarding_schema.clean_onboarding`.**
   `users.onboarding` is flattened into both the question and persona prompts,
   so an unallowlisted field is a direct prompt-injection channel. Allowlist +
   length caps + closed vocabularies for CHOICE fields (which are also the
   `candidate_profile` routing keys). Adding a field to `onboarding.html`
   *requires* adding it here.
5. **The persona is locked once built.** Enforced in
   `session_controller._reject_if_locked`, not just at the final generate call,
   so a non-eligible user can't burn a Groq call or overwrite `raw_responses` by
   hitting `get-questions`/`save-answers` directly. Frontend guards are UX only.
   Refresh needs `SESSIONS_REQUIRED_TO_REFRESH = 3` new sessions — an **internal**
   threshold; surface only the boolean verdict, never a countdown.
6. **`FLASK_SECRET_KEY` has no fallback**, in any environment. A hardcoded
   default would be a session-forgery hole with no error pointing at it. Boot
   fails loudly instead.
7. **Resume text is PII-sanitized before storage** (`resume_agent.sanitize_resume_text`,
   `SANITIZER_VERSION`). Rows carry the version that produced them so a future
   filter improvement can be rolled out with `scrub_resume_text.py --apply`.
8. **The frontend never mirrors server state into localStorage.** `Aira.progress()`
   (one `/me/report` call) answers every "how far along are they" question.
   localStorage is for purely client-side UI state only.
9. **Tests must never import `app.py`** — it builds at module scope and reads
   the real `DATABASE_URL` from `backend/.env`, which points at production.
   `tests/conftest.py` mounts the blueprint under test on a bare Flask app with
   in-memory SQLite.
10. **One Groq transport.** New LLM features call `groq_client.groq_json` with a
    new `label`; they don't open their own HTTP path.
11. **`/account/forgot-password` must never return a reset token.** It once did,
    which made knowing a registered email enough to take the account over. Both
    branches now return a byte-identical body and mint nothing; the real send
    goes at the marked call site. `tests/test_account_bp.py` asserts on the whole
    response body so any re-introduction fails there.

**Local dev talks to production.** `backend/.env` sets `DATABASE_URL` to the live
Render Postgres, so `python backend/app.py` is pointed at real user data —
`db.create_all()` and `migrate_new_columns` run against it on every boot.
`tests/conftest.py` documents this hazard and sidesteps it; the dev server has no
such guard. Override `DATABASE_URL` before exercising any write path locally.

**Render publishes two hostnames for the same database**: internal `dpg-xxxx-a`
(resolves only inside Render) and external
`dpg-xxxx-a.<region>-postgres.render.com`. `.env` has `DB_URL` on the internal
one and `DATABASE_URL` on the external one, which is why a script reading only
`DB_URL` dies with `could not translate host name`. `db_url.resolve()` handles
the fallthrough — new operator scripts should use it rather than re-deriving it.

---

## 8. Flows worth knowing

**Onboarding.** `POST /onboarding/save` is multipart and one-shot. The resume is
**optional** (changed 2026-08-10 — it was the heaviest first ask and the step
users stopped at); with one it runs the resume agent, without one it skips the
parse and costs nothing. `POST /onboarding/update` edits fields only — no resume,
no LLM. Changing a routing field (`experience`/`goal`) clears an unanswered
cached question set so the next `/session/get-questions` regenerates at the new
stage.

**Late resume.** `POST /me/resume` is the way back in for anyone who skipped it.
It stores the analysis, clears `dimension_questions` so the next assessment
regenerates *with* the 4 resume-grounded questions, and sets
`Persona.resume_refresh_pending` when a persona already exists — which
`persona_eligibility.refresh_eligible` honours as an immediate bypass of the
3-session threshold. Rationale: the lock exists to stop someone re-rolling after
one bad day, but a resume is the largest piece of evidence the assessment can
have, and the existing persona was built without it. The flag is cleared when the
refreshed persona is generated, not at upload, so an abandoned upload keeps the
offer open and a completed one can't be re-spent.

**The resume-less path is load-bearing, not a fallback.** `_assemble_questions`
targets all 8 dimensions (not 5) and returns 8 generated + 1 reflection;
`question_agent` and `persona_agent` both take `resume_data=None`;
`persona_agent._format_context` writes `"(no resume on file)"`;
`candidate_profile` is declaration-first so band routing never needed the resume;
`communication_bp._projects()` returns `[]`. Don't "fix" any of these by making
resume data required again.

**Assessment.** `/session/get-questions` returns 10 blended, indistinguishable
questions: dimension questions from `question_agent` (a floor, not a fixed
count) + 4 resume-grounded from `resume_agent` (2 technical recall + 2 HR
situational MCQ) + open reflections from `question_bank.json`. The reflections
stay hardcoded on purpose — they're deliberately generic, so personalizing them
buys nothing.

**Mock interview.** `role` mode needs no topic (Aira already knows the user);
`topic` mode takes one. Generate stores the set server-side; evaluate grades
only that set, persists, then clears it.

**Communication.** Six tracks in `softskill_agent.TRACK_BLUEPRINTS`:
`communication`, `intro`, `project`, `voice`, `leadership`, `technical`. Only
the track **id + label** are structural constants (navigation, URLs, the unlock
key); every pedagogical detail — framework name, beats, hints, model example —
is generated per user from their résumé. Teach → Try → Adapt → Refine, with
adaptive difficulty on a 1–5 ladder.

**Report.** `/me/report` aggregates everything; opening the PTM folder on
`report.html` POSTs a snapshot, so returning students can compare past report
cards instead of only seeing a live view.

---

## 9. Frontend conventions

- One HTML file per screen, styled from `assets/chalkboard.css` variables
  (`--board`, `--chalk`, `--wood`, fonts `--fd` Schoolbell / `--fb` Nunito).
- Every fetch goes through `Aira.api()`: same-origin cookies, JSON in/out, and a
  single place that turns 401 into the login redirect.
- The school bell is synthesized with the Web Audio API (`Aira.ringBell()`) —
  no audio file, works offline.
- Pretty URLs work by convention: `app.py`'s catch-all tries the exact file,
  then `<name>.html`.

---

## 10. Running it

```bash
pip install -r backend/requirements.txt
```

```bash
python backend/app.py
```

```bash
python -m pytest backend/tests
```

Env vars (`backend/.env`): `GROQ_API_KEY` and `FLASK_SECRET_KEY` required;
`DATABASE_URL` optional (omit for local SQLite); `FLASK_ENV=production` marks the
cookie Secure; `CORS_ORIGINS` only if a different origin ever calls the API.

Deployment is Render via `render.yaml`. Notes that bite:

- **One gunicorn worker, ten gthread threads.** The in-memory rate limiter is
  only correct in a single process — adding `--workers` requires a shared
  backend like Redis first.
- `--timeout 60` is deliberately above `groq_client`'s 45s request timeout so
  gunicorn never kills a worker mid-call.
- `DATABASE_URL` is a **manually set secret**, not a `fromDatabase` link: free
  Postgres instances are deleted ~90 days after creation and the auto-link
  silently keeps pointing at the dead host. Rotate it in the dashboard.
- Free tier spins down after ~15 min idle; the next request takes 30–60s.
- `postgres://` URLs are normalized to `postgresql://` in `app.py`.

Never commit: `.env`, `google_creds.json`, `feedback_export_*.csv` (real user
emails and free text). All gitignored.

---

## 11. Working conventions in this repo

- **Comments explain *why*, at length.** The existing code documents the failure
  mode a decision prevents, not what the line does. Match that density — a new
  guard without its rationale is out of place here.
- Docstrings at module top carry the design story. Read them before editing a module.
- Keep `ENDPOINTS.md` in sync with any route contract change.
- Tests cover pure helpers and endpoint contracts; no network.
- The feedback module is explicitly temporary — don't build on it.
- Framing across the app is currently engineering/placement-coded. Broadening to
  all fields is a known, deferred decision, not an oversight.
