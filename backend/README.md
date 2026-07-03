# Aira — backend

Aira is an AI career-coaching app for students and freshers. At onboarding it reads
the user's resume and a short assessment, builds a **private persona** server-side
(never shown to or accepted from the client), and uses it to personalise three
modules: mock interviews (`/quiz/*`), communication practice (`/comm/*`), and an
aggregated progress report (`/me/report`).

The stack: Flask + SQLite (SQLAlchemy), signed-cookie sessions for auth, and the
Groq chat API (`llama-3.3-70b-versatile`) for all LLM calls via `groq_client.py`.
The plain-HTML frontend in `../frontend/` is served by this same Flask app.

## Run it

```bash
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env   # then fill in your keys
python backend/app.py                  # serves http://127.0.0.1:5000
```

The SQLite database (`backend/aira.db`) and its tables are created automatically on
first boot.

## Environment variables (`backend/.env`)

| Variable | Required | Purpose |
|----------|----------|---------|
| `GROQ_API_KEY` | yes | All LLM calls (persona, resume parsing, interviews, practice). Without it, LLM features fall back or return errors. |
| `FLASK_SECRET_KEY` | yes in prod | Signs the session cookie. Defaults to a dev-only value. |

## Tests

```bash
python -m pytest backend/tests
```

The tests cover the pure helpers only (question assembly, answer validation,
resume normalisation, resource-link building) and make no network calls.

## Endpoint reference

See [`../ENDPOINTS.md`](../ENDPOINTS.md).
