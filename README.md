# Aira — your own classroom, run by an AI that actually knows you

Aira is an AI-powered career-prep companion for students and fresh graduates,
built around a single idea: **your progress deserves a teacher who remembers
you**, not another generic quiz app. The entire product is styled as an early-2000s
Indian classroom — a green chalkboard, white chalk, a duster, a school bell, and
at the end of it, an actual 90s-style report card with grades and a teacher's
remarks — because that's the last time most of us got personal, one-on-one
feedback on how we think and communicate.

Under the chalk dust, it's a real assessment engine: a private AI persona built
from your resume and a 10-question assessment, mock interviews and topic
quizzes graded server-side, speaking practice scored on fluency/clarity/
confidence/structure, and a report card that ties all three together —
personalised to your actual target role, and archived over time so you can see
how you've grown.

## The experience

- **Enrolment (`signup.html` / `login.html`)** — "Take a seat" / "Mark me
  present" instead of generic sign-up copy. The whole app commits to the bit.
- **Onboarding (`onboarding.html`)** — first day of class: your course of
  study, your goal, your target role, and your resume, so Aira has something
  real to build a profile from.
- **Persona (`questions.html` → `persona.html`)** — 10 questions (5 fixed
  scenarios, 4 grounded in your actual resume, 1 open reflection) build a
  **private** AI persona. It's never shown to you directly or accepted back
  from the client — only its effects show up in how everything else adapts to you.
- **Classroom dashboard (`Welcome.html`)** — your three "periods" for the day:
  Persona → Practice (Mock Interview or Soft Skills) → Report Card, gated in
  order, with an attendance-style progress bar.
- **Mock Interview / Class Test (`ai_quiz.html`)** — role-based interviews or
  topic quizzes, timed like an exam, graded server-side against the exact
  question set the server generated (so answers can't be forged after the fact).
- **Soft Skills (`soft_skills.html` → `practice.html` → `reflection.html`)** —
  the flagship page: a scroll-driven illustrated timeline, **"From the green
  board to a coach that knows you,"** walking through four hand-drawn eras
  (2000s chalkboard → 2010s whiteboard/projector → 2020s video calls →
  today's AI) before you pick a track (Communication / Self-intro / Talk about
  your project) and practice live, scored on delivery.
- **The bell** — a real two-tone school bell, synthesized in-browser via the
  Web Audio API (no audio file needed), rings at the end of every quiz and
  every speaking session.
- **Report Card (`report.html`)** — a Parent-Teacher-Meeting moment: a 3D
  folder sits on the teacher's desk, you tap it, the bell rings, the cover
  swings open, and it reveals an actual mark-sheet-style report card —
  subjects table (Persona / Class Test / Soft Skills), marks out of 100,
  letter grades, a remarks column, a rotated "PASS" ink stamp, and a
  personalized Class Teacher's Remarks paragraph that references your actual
  target role and recurring weak habits. Every time the folder opens, that
  report card is archived as a dated snapshot, so returning students can pull
  up and compare past report cards instead of only ever seeing one live view.

## Why it's built this way

Nothing here is decoration for its own sake — every module feeds the report:

- The **persona** shapes the tone and difficulty of every interview question,
  every practice prompt, and the report's own remarks.
- **Quiz and speaking results** are never client-reported — they're graded
  against a question/prompt set the server generated and held, so a session
  can't be faked after the fact.
- **Weak areas** are tracked across *both* quizzes and speaking sessions —
  a habit that shows up more than once becomes a "recurring weak area" the
  report calls out by name.
- The **report card** pulls in your onboarding goal/target role, your persona
  summary, your latest quiz and speaking scores, and those recurring weak
  areas into one personalised document — not just an aggregate of numbers.

## Tech stack

- **Backend:** Flask + SQLAlchemy, session-cookie auth, Groq's
  `llama-3.3-70b-versatile` for every LLM call (persona generation, resume
  parsing, interview questions/grading, speaking prompts/grading).
  SQLite locally, Postgres in production (same ORM, just a different
  connection string — see [`backend/README.md`](backend/README.md)).
- **Frontend:** plain HTML/CSS/JS, no build step, no framework — served
  directly by the same Flask app. The whole visual theme lives in one shared
  stylesheet, [`frontend/assets/chalkboard.css`](frontend/assets/chalkboard.css)
  (chalk textures, Schoolbell/Kalam fonts, pinned-index-card components), plus
  a shared helper, [`frontend/assets/aira.js`](frontend/assets/aira.js), for
  API calls, auth gating, and the bell sound.
- **Data model:** a private `Persona` per user, an append-only history of
  `QuizResult` and `SpeakingSession` rows, and `ReportSnapshot` rows archiving
  the report card at each PTM moment. Nothing about a past session is ever
  overwritten — the report always reflects full history.

## Project layout

```
backend/            Flask app, models, blueprints, Groq client, tests
  app.py             entry point — also serves the frontend as static files
  models.py          User, Persona, QuizResult, SpeakingSession, ReportSnapshot
  persona_bp.py       /me/report, /me/report/snapshot(s), /me/speaking
  ai_quiz_bp.py       /quiz/*  (mock interview / topic quiz)
  communication_bp.py /comm/*  (soft skills practice)
  session_controller.py /onboarding/*, /session/*  (persona assessment flow)
frontend/            Plain HTML/CSS/JS pages, one per screen (see above)
  assets/chalkboard.css   shared theme
  assets/aira.js          shared API/auth/bell helper
render.yaml          Render Blueprint: web service + free Postgres, one click
ENDPOINTS.md         Full API reference
```

## Running it locally

```bash
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env   # fill in GROQ_API_KEY
python backend/app.py                  # http://127.0.0.1:5000 — serves both API and frontend
```

Full environment variable reference, tests, and the Render deployment guide
live in [`backend/README.md`](backend/README.md). The full API surface is
documented in [`ENDPOINTS.md`](ENDPOINTS.md).
