# Aira — backend

Aira is an AI career-coaching app for students and freshers. At onboarding it reads
the user's resume and a short assessment, builds a **private persona** server-side
(never shown to or accepted from the client), and uses it to personalise three
modules: mock interviews (`/quiz/*`), communication practice (`/comm/*`), and an
aggregated progress report (`/me/report`), which can also be archived as dated
snapshots (`/me/report/snapshot`).

The stack: Flask + SQLAlchemy (SQLite locally, Postgres in production),
signed-cookie sessions for auth, and the Groq chat API
(`llama-3.3-70b-versatile`) for all LLM calls via `groq_client.py`. The
plain-HTML frontend in `../frontend/` is served by this same Flask app —
there is no separate frontend server, in dev or in production.

## Run it locally

```bash
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env   # then fill in your keys
python backend/app.py                  # serves http://127.0.0.1:5000
```

With no `DATABASE_URL` set, it uses a local SQLite file (`backend/aira.db`) —
created automatically, along with all tables, on first boot.

## Environment variables (`backend/.env`)

| Variable | Required | Purpose |
|----------|----------|---------|
| `GROQ_API_KEY` | yes | All LLM calls (persona, resume parsing, interviews, practice). Without it, LLM features fail. |
| `FLASK_SECRET_KEY` | yes in prod | Signs the session cookie. Defaults to a dev-only value locally. |
| `DATABASE_URL` | no | Set by the host when a managed Postgres instance is attached (see Deployment below). Omit for local SQLite. |
| `FLASK_ENV` | no | Set to `production` in prod so the session cookie is marked `Secure` (HTTPS-only). |
| `GEMINI_API_KEY` | — | Present in `.env.example` from an earlier prototype; **not currently read by any code path.** Safe to leave blank. |

## Tests

```bash
python -m pytest backend/tests
```

The tests cover the pure helpers only (question assembly, answer validation,
resume normalisation, resource-link building) and make no network calls.

## Endpoint reference

See [`../ENDPOINTS.md`](../ENDPOINTS.md).

## Deployment (Render)

The repo root has a `render.yaml` Blueprint that provisions both pieces at once:
a free web service (gunicorn, not the Flask dev server) and a free managed
Postgres database, wired together via `DATABASE_URL`.

1. Push to GitHub.
2. On [render.com](https://render.com): **New → Blueprint** → pick this repo.
   Render reads `render.yaml` and proposes the web service + `aira-db` Postgres
   instance together.
3. When prompted, fill in the two secret env vars it can't infer:
   `GROQ_API_KEY` and `GEMINI_API_KEY` (from your local `.env`).
4. Click **Apply**. Render runs `pip install -r backend/requirements.txt`, then
   starts `gunicorn app:app` from inside `backend/`. `db.create_all()` runs on
   boot and creates every table fresh in the new Postgres database.
5. Your app is live at the `https://<name>.onrender.com` URL Render gives you.

Two free-tier things worth knowing:
- The web service spins down after ~15 minutes idle; the next request wakes it
  up but takes 30-60 seconds.
- Render's free Postgres plan is deleted after 90 days unless upgraded to a
  paid plan — fine for a demo, not for anything you need to keep long-term.

Redeploying (e.g. `git push`) never touches the database — since data lives in
Postgres instead of a file on the web service's disk, it survives every
redeploy, unlike SQLite would on most free hosts.
