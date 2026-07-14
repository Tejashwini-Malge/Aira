"""One-off read-only stats check against the production database.

Usage (PowerShell):
    $env:DB_URL = "postgresql://...external database url from Render..."
    python check_stats.py
"""
import os
import sys

import psycopg2

url = os.environ.get("DB_URL")
if not url:
    # Fall back to a DB_URL line in backend/.env so the credential can live
    # in a local file instead of a shell command.
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend", ".env")
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


def q(sql):
    cur.execute(sql)
    return cur.fetchone()[0]


print("Total signups:             ", q("SELECT COUNT(*) FROM users"))
print("Real users (no test accts):", q(
    "SELECT COUNT(*) FROM users "
    "WHERE email NOT LIKE 'claude-diag-%' AND email NOT LIKE 'fixtest-%'"))
print("Completed onboarding:      ", q("SELECT COUNT(*) FROM users WHERE onboarding_complete"))
print("Took a quiz:               ", q("SELECT COUNT(DISTINCT user_id) FROM quiz_results"))
print("Did a speaking session:    ", q("SELECT COUNT(DISTINCT user_id) FROM speaking_sessions"))

conn.close()
