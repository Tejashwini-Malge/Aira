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
| `FLASK_SECRET_KEY` | yes | Signs the session cookie. Always required — no fallback value, in dev or prod. |
| `DATABASE_URL` | no | Set by the host when a managed Postgres instance is attached (see Deployment below). Omit for local SQLite. |
| `FLASK_ENV` | no | Set to `production` in prod so the session cookie is marked `Secure` (HTTPS-only). |

## Tests

```bash
python -m pytest backend/tests
```

The tests cover the pure helpers only (question assembly, answer validation,
resume normalisation, resource-link building) and make no network calls.

## Endpoint reference

See [`../ENDPOINTS.md`](../ENDPOINTS.md).

## Feedback data (TEMPORARY — market validation only)

`frontend/feedback.html` sits in front of every path to the report card and
captures a star rating, an NPS-style recommend score, three indirect
multiple-choice questions, and free text — every field required. It's saved to the same
database as everything else (`feedback` table, `backend/models.py::Feedback`)
— SQLite locally, the production Postgres instance in prod. No separate
storage, no third-party survey tool.

This is explicitly a **temporary instrument to validate the product**, not a
permanent review/UGC feature — expect it to be revisited or removed once
we've learned what we need from it. To pull it out as a CSV for review:

```bash
$env:DB_URL = "postgresql://...production DB URL..."   # PowerShell
python ../export_feedback.py                            # writes feedback_export_<timestamp>.csv
```

(or double-click `export_feedback.bat`, same pattern as `run_stats.bat`).
`check_stats.py` also prints a quick summary (average rating/NPS, latest 20
reviews) without needing to open the CSV.

For sharing with others doing market validation, `../export_feedback_to_sheets.py`
syncs the same data straight into a Google Sheet instead — appends only the
rows the sheet doesn't already have (matched by feedback id), so running it
again just extends the same sheet rather than duplicating anything. One-time
setup (a Google service account + sharing your sheet with it) is documented
in that script's docstring; after that:

```bash
$env:DB_URL = "postgresql://...production DB URL..."
$env:GOOGLE_SHEET_ID = "...id from the sheet's URL..."
python ../export_feedback_to_sheets.py
```

(or double-click `export_feedback_to_sheets.bat`). The service account key
(`google_creds.json`) and any `feedback_export_*.csv` are gitignored — never
commit either, they carry user emails and free-text responses.

## Deployment (Render)

The repo root has a `render.yaml` Blueprint that provisions both pieces at once:
a free web service (gunicorn, not the Flask dev server) and a free managed
Postgres database, wired together via `DATABASE_URL`.

1. Push to GitHub.
2. On [render.com](https://render.com): **New → Blueprint** → pick this repo.
   Render reads `render.yaml` and proposes the web service + `aira-db` Postgres
   instance together.
3. When prompted, fill in the secret env var it can't infer:
   `GROQ_API_KEY` (from your local `.env`).
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
