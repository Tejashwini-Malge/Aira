"""One-off / repeatable scrub of personas.resume_text on an existing database.

Rows written before resume filtering shipped hold the RAW resume: the
candidate's name, phone, email, college, marks and employers. Nothing in the
app has ever read this column, so replacing each row with its filtered
equivalent loses no functionality.

Each row records the resume_agent.SANITIZER_VERSION that produced its text, so
this script is also the tool for rolling out a FUTURE filter improvement: bump
SANITIZER_VERSION, then re-run here to rewrite only the rows below it.

ORDER OF OPERATIONS — deploy BEFORE applying. personas.resume_sanitizer_version
is created by models.migrate_new_columns, which only runs when the app boots, so
--apply cannot stamp a version until the deploy has happened (it refuses rather
than writing unversioned rows). A dry run works either side of the deploy: run
before it for a preview, after it for the real numbers.

Usage (PowerShell), from the backend/ directory:

    $env:DB_URL = "postgresql://...external database url from Render..."

    # 1. Optional preview, safe before deploying — reads only, writes nothing:
    python scrub_resume_text.py

    # 2. Deploy. Startup adds the column; new onboardings store filtered text.

    # 3. Dry run again, now version-aware:
    python scrub_resume_text.py

    # 4. Apply once the report looks right:
    python scrub_resume_text.py --apply

    # 5. Later: after bumping SANITIZER_VERSION to 2, re-scrub only older rows:
    python scrub_resume_text.py --below 2 --apply

DB_URL falls back to a DB_URL=... line in backend/.env, matching check_stats.py.

This rewrite is NOT reversible — the raw text is gone afterwards. That is the
point, but take a Render database snapshot first if you want a way back.
"""
import os
import re
import sys

import psycopg2

from resume_agent import sanitize_resume_text, SANITIZER_VERSION

APPLY = "--apply" in sys.argv

# Default: rewrite anything not already at the current version.
below = SANITIZER_VERSION
if "--below" in sys.argv:
    try:
        below = int(sys.argv[sys.argv.index("--below") + 1])
    except (IndexError, ValueError):
        sys.exit("--below needs an integer, e.g. --below 2")

url = os.environ.get("DB_URL")
if not url:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("DB_URL="):
                    url = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
if not url:
    sys.exit('Set DB_URL first ($env:DB_URL = "postgresql://...") '
             "or add a DB_URL=... line to backend/.env")

# Only ever counted, never printed — this script must not spill the very data
# it exists to remove into a terminal or CI log.
_PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "phone": re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(?\d[\)\s.-]?){9,}\d"),
    "url/handle": re.compile(r"(?:https?://|www\.)\S+|\b(?:linkedin|github)\.com/\S*", re.I),
}

conn = psycopg2.connect(url)
cur = conn.cursor()

# personas.resume_sanitizer_version is created by models.migrate_new_columns,
# which only runs when the app boots. Before the first deploy of that migration
# the column does not exist, and naming it in a WHERE clause would abort the
# transaction with UndefinedColumn. Checked up front so a pre-deploy dry run
# still works as a preview instead of dying on a cryptic Postgres error.
cur.execute(
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_name = 'personas' AND column_name = 'resume_sanitizer_version'"
)
has_version_column = cur.fetchone() is not None

if not has_version_column:
    if APPLY:
        sys.exit(
            "personas.resume_sanitizer_version does not exist yet, so this run could not\n"
            "record which sanitizer version it wrote — refusing to rewrite rows.\n"
            "Deploy first (the column is added at app startup), then re-run with --apply."
        )
    print("NOTE: personas.resume_sanitizer_version does not exist yet — this is a\n"
          "      PRE-DEPLOY preview. Every row is treated as v0 (raw). Deploy to add\n"
          "      the column, then re-run before using --apply.\n")

if has_version_column:
    cur.execute(
        "SELECT id, resume_text, COALESCE(resume_sanitizer_version, 0) "
        "FROM personas "
        "WHERE resume_text IS NOT NULL AND resume_text <> '' "
        "  AND COALESCE(resume_sanitizer_version, 0) < %s",
        (below,),
    )
else:
    cur.execute(
        "SELECT id, resume_text, 0 "
        "FROM personas "
        "WHERE resume_text IS NOT NULL AND resume_text <> ''"
    )
rows = cur.fetchall()

print(f"{'APPLYING' if APPLY else 'DRY RUN'} — sanitizer v{SANITIZER_VERSION}, "
      f"rewriting rows below v{below}")
print(f"{len(rows)} persona row(s) match\n")

if not rows:
    print("Nothing to do — every stored resume is already at or above that version.")
    conn.close()
    sys.exit(0)

total_before = total_after = 0
hits = {k: 0 for k in _PII_PATTERNS}
by_version = {}
updates = []

for pid, raw, version in rows:
    cleaned = sanitize_resume_text(raw)
    total_before += len(raw)
    total_after += len(cleaned)
    by_version[version] = by_version.get(version, 0) + 1
    for name, pattern in _PII_PATTERNS.items():
        if pattern.search(raw):
            hits[name] += 1
    updates.append((pid, cleaned))

print("Current version of matched rows:")
for version, count in sorted(by_version.items()):
    label = "raw, never sanitized" if version == 0 else f"sanitizer v{version}"
    print(f"  v{version} ({label}): {count} row(s)")

print("\nDirect identifiers currently stored:")
for name, count in hits.items():
    print(f"  rows containing a(n) {name:<11}: {count}")

print(f"\nStored characters: {total_before} -> {total_after}"
      f"  ({100 - (total_after * 100 // total_before) if total_before else 0}% reduction)")
print(f"Rows to rewrite:   {len(updates)}")

if not APPLY:
    print("\nNothing was written. Re-run with --apply to perform the scrub.")
    conn.close()
    sys.exit(0)

for pid, cleaned in updates:
    cur.execute(
        "UPDATE personas SET resume_text = %s, resume_sanitizer_version = %s WHERE id = %s",
        (cleaned, SANITIZER_VERSION, pid),
    )
conn.commit()
conn.close()
print(f"\nDone — {len(updates)} row(s) rewritten and stamped v{SANITIZER_VERSION}.")
