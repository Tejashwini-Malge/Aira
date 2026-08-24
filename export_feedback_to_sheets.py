"""One-off sync of the `feedback` table into a Google Sheet — a TEMPORARY
market-validation instrument (see backend/models.py::Feedback), not a
permanent integration. Appends only rows the sheet doesn't already have
(matched by feedback id in column A), so re-running this on a schedule just
extends the same sheet instead of duplicating rows.

One-time setup:
  1. In Google Cloud Console, create a service account and download its JSON
     key. Save it as google_creds.json in the repo root (already gitignored).
  2. Open the JSON, copy the "client_email" value, and share your target
     Google Sheet with that email as an Editor.
  3. Copy the Sheet ID out of its URL:
     https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit

Usage (PowerShell):
    $env:DB_URL = "postgresql://...external database url from Render..."
    $env:GOOGLE_SHEET_ID = "...sheet id from step 3..."
    python export_feedback_to_sheets.py

GOOGLE_CREDS_PATH overrides the default google_creds.json location if needed.
"""
import os
import sys

import gspread
import psycopg2
from google.oauth2.service_account import Credentials

import db_url

HEADER = [
    "id", "user_id", "email", "rating", "recommend_score",
    "confidence_shift", "keep_one_part", "return_reason",
    "liked", "improve", "context", "created_at",
]


def _env_or_dotenv(key):
    return os.environ.get(key) or db_url.from_env_file(key)


url = db_url.resolve()
sheet_id = _env_or_dotenv("GOOGLE_SHEET_ID")
creds_path = _env_or_dotenv("GOOGLE_CREDS_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "google_creds.json"
)

if not url:
    sys.exit(db_url.MISSING_MESSAGE)
if not sheet_id:
    sys.exit('Set GOOGLE_SHEET_ID first ($env:GOOGLE_SHEET_ID = "...") '
             "or add a GOOGLE_SHEET_ID=... line to backend/.env")
if not os.path.exists(creds_path):
    sys.exit(f"No service account key found at {creds_path} — see the setup "
              "steps in this script's docstring.")

creds = Credentials.from_service_account_file(
    creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheet = gspread.authorize(creds).open_by_key(sheet_id).sheet1

existing_rows = sheet.get_all_values()
if not existing_rows:
    sheet.append_row(HEADER)
    seen_ids = set()
else:
    seen_ids = {row[0] for row in existing_rows[1:] if row}

conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute("""
    SELECT f.id, f.user_id, u.email, f.rating, f.recommend_score,
           f.mcq_responses, f.liked, f.improve, f.context, f.created_at
    FROM feedback f
    JOIN users u ON u.id = f.user_id
    ORDER BY f.created_at ASC
""")
rows = cur.fetchall()
conn.close()

new_rows = []
for fid, user_id, email, rating, nps, mcq, liked, improve, context, created_at in rows:
    if str(fid) in seen_ids:
        continue
    mcq = mcq or {}
    new_rows.append([
        fid, user_id, email, rating, nps,
        mcq.get("confidence_shift", ""), mcq.get("keep_one_part", ""), mcq.get("return_reason", ""),
        liked, improve, context, created_at.isoformat(),
    ])

if new_rows:
    sheet.append_rows(new_rows, value_input_option="RAW")
print(f"Added {len(new_rows)} new row(s); {len(seen_ids)} were already in the sheet.")
