"""Read-only diagnostic: how well does the sanitizer's heading vocabulary match
the resumes actually in your database?

Answers two questions the scrub's dry run raises but can't explain:
  1. How many rows reach tier 1 (projects/skills only) vs fall back to the
     wider tier 2/3 slice?
  2. Which headings is the filter failing to recognise? Those are the concrete
     candidates for _KEEP_HEADINGS / _DROP_HEADINGS.

This script NEVER writes, and never prints resume content. Unrecognised headings
are only reported when they appear in at least MIN_ROWS DIFFERENT resumes — a
person's name or a one-off line won't clear that bar, but a real section heading
will. Raise MIN_ROWS if you want to be stricter still.

Usage (PowerShell), from the backend/ directory:

    $env:DB_URL = "postgresql://...external database url from Render..."
    python diagnose_resume_headings.py

DB_URL falls back to a DB_URL=... line in backend/.env, matching check_stats.py.
"""
import os
import sys

import psycopg2

from resume_agent import _classify_heading, _redact_pii, sanitize_resume_text

# An unrecognised heading is only shown if this many distinct resumes contain it.
MIN_ROWS = 2
# Belt and braces: never echo anything longer than a plausible heading.
MAX_HEADING_CHARS = 40

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

conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute("SELECT id, resume_text FROM personas "
            "WHERE resume_text IS NOT NULL AND resume_text <> ''")
rows = cur.fetchall()
conn.close()

if not rows:
    sys.exit("No stored resume text to analyse.")

tier1 = tier_fallback = 0
no_headings_at_all = 0
unknown_rows = {}   # normalized heading -> set of row ids
verdict_counts = {"keep": 0, "drop": 0, "unknown": 0}

for pid, raw in rows:
    redacted = _redact_pii(raw or "")
    saw_keep = saw_any_heading = False

    for line in redacted.splitlines():
        verdict = _classify_heading(line)
        if verdict is None:
            continue
        saw_any_heading = True
        verdict_counts[verdict] += 1
        if verdict == "keep":
            saw_keep = True
        elif verdict == "unknown":
            label = " ".join(line.strip().split())[:MAX_HEADING_CHARS]
            unknown_rows.setdefault(label.lower(), set()).add(pid)

    if saw_keep:
        tier1 += 1
    else:
        tier_fallback += 1
        if not saw_any_heading:
            no_headings_at_all += 1

total = len(rows)
print(f"Analysed {total} stored resume(s)\n")
print(f"  reach tier 1 (projects/skills only): {tier1:>3}  ({tier1 * 100 // total}%)")
print(f"  fall back to tier 2/3 wider slice  : {tier_fallback:>3}  "
      f"({tier_fallback * 100 // total}%)")
print(f"     ...of which have NO detectable headings at all: {no_headings_at_all}")
print(f"\nHeading lines classified: keep={verdict_counts['keep']} "
      f"drop={verdict_counts['drop']} unrecognised={verdict_counts['unknown']}")

common = {h: ids for h, ids in unknown_rows.items() if len(ids) >= MIN_ROWS}
print(f"\nUnrecognised headings appearing in >= {MIN_ROWS} different resumes "
      f"({len(common)} distinct):")
if not common:
    print("  (none — the vocabulary covers everything the filter could detect)")
for heading, ids in sorted(common.items(), key=lambda kv: -len(kv[1])):
    print(f"  {len(ids):>3} resume(s):  {heading}")

print("\nAdd anything above that names projects/skills to _KEEP_HEADINGS, and\n"
      "anything naming identity/education/employers to _DROP_HEADINGS, then bump\n"
      "SANITIZER_VERSION and re-run scrub_resume_text.py --below <new version>.")

if no_headings_at_all:
    print(f"\nNOTE: {no_headings_at_all} resume(s) have no detectable heading lines at "
          "all.\n      Those are usually PDF extractions that lost their line structure, "
          "so no\n      vocabulary change will help them — they will always take tier 3.")
