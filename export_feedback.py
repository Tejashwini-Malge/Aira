"""Read-only export of collected feedback from the production database.

This is the companion to the pre-report feedback sheet (frontend/feedback.html
+ backend/feedback_bp.py). Users answer in the moment; this is how you actually
*read* those answers. Read-only — it never writes to the DB.

Usage (PowerShell):
    $env:DB_URL = "postgresql://...external database url from Render..."
    python export_feedback.py

Or add a DB_URL=... line to backend/.env and just run:
    python export_feedback.py

Prints a summary to the console and writes feedback_export_<timestamp>.csv
next to this file.
"""
import csv
import os
import sys
from collections import Counter
from datetime import datetime

import psycopg2

url = os.environ.get("DB_URL")
if not url:
    # Fall back to a DB_URL line in backend/.env so the credential can live
    # in a local file instead of a shell command (same pattern as check_stats.py).
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("DB_URL="):
                    url = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
if not url:
    sys.exit('Set DB_URL first ($env:DB_URL = "postgresql://...") '
             "or add a DB_URL=... line to backend/.env")

# The three indirect MCQ keys, in the order they're asked (see
# feedback_bp.MCQ_QUESTIONS). Kept here so the CSV columns and the summary
# read in a predictable order rather than whatever JSON key order comes back.
MCQ_KEYS = ["confidence_shift", "keep_one_part", "return_reason"]

conn = psycopg2.connect(url)
cur = conn.cursor()

# LEFT JOIN so a row still exports even if its user was somehow removed.
cur.execute(
    """
    SELECT f.id, f.created_at, u.email, u.name,
           f.rating, f.recommend_score, f.context,
           f.mcq_responses, f.liked, f.improve
    FROM feedback f
    LEFT JOIN users u ON u.id = f.user_id
    ORDER BY f.created_at
    """
)
rows = cur.fetchall()
conn.close()

if not rows:
    print("No feedback has been submitted yet.")
    sys.exit(0)

# --- Console summary ---
ratings = [r[4] for r in rows if r[4] is not None]
nps = [r[5] for r in rows if r[5] is not None]

print(f"Total feedback rows: {len(rows)}")
if ratings:
    print(f"Average rating:      {sum(ratings) / len(ratings):.2f} / 5   "
          f"(distribution: {dict(sorted(Counter(ratings).items()))})")
if nps:
    print(f"Average recommend:   {sum(nps) / len(nps):.2f} / 10")

for key in MCQ_KEYS:
    counts = Counter((r[7] or {}).get(key) for r in rows if (r[7] or {}).get(key))
    if counts:
        print(f"\n{key}:")
        for option, n in counts.most_common():
            print(f"    {n:>3}  {option}")

# --- CSV export ---
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f"feedback_export_{timestamp}.csv")
with open(out_path, "w", newline="", encoding="utf-8") as fh:
    writer = csv.writer(fh)
    writer.writerow(
        ["id", "created_at", "email", "name", "rating", "recommend_score",
         "context"] + MCQ_KEYS + ["liked", "improve"]
    )
    for (fid, created_at, email, name, rating, recommend_score, context,
         mcq, liked, improve) in rows:
        mcq = mcq or {}
        writer.writerow(
            [fid, created_at, email, name, rating, recommend_score, context]
            + [mcq.get(k, "") for k in MCQ_KEYS]
            + [liked, improve]
        )

print(f"\nWrote {len(rows)} rows to {out_path}")
