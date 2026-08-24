"""One-off read-only stats check against the production database.

Usage (PowerShell):
    $env:DB_URL = "postgresql://...external database url from Render..."
    python check_stats.py
"""
import sys

import psycopg2

import db_url

url = db_url.resolve()
if not url:
    sys.exit(db_url.MISSING_MESSAGE)

conn = psycopg2.connect(url)
cur = conn.cursor()


def q(sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def rows(sql):
    cur.execute(sql)
    return cur.fetchall()


# Accounts that are yours, not the market's. Edit OWNER_NAME/OWNER_EMAILS as you
# make more of them — an inflated "real users" number is the one statistic that
# can talk you out of doing something about a real one.
#
# The old filter only stripped claude-diag-% and fixtest-%, so on 2026-08-10 it
# reported 15 real users when six accounts were named "Tejashwini Malge", one had
# the literal email "rr", and one was arjun@example.com. Roughly a third of the
# "users" were the developer.
OWNER_NAME = "Tejashwini Malge"
OWNER_EMAILS = ("rr", "tejashwinimalge@gmail.com", "demo@gmail.com")

TEST_ACCOUNT_FILTER = " AND ".join([
    "email NOT LIKE 'claude-diag-%'",
    "email NOT LIKE 'fixtest-%'",
    "email NOT LIKE '%@example.com'",
    "email LIKE '%@%.%'",                 # "rr", "fghf" — not addresses at all
    f"COALESCE(name, '') <> '{OWNER_NAME}'",
    "email NOT IN ({})".format(", ".join(f"'{e}'" for e in OWNER_EMAILS)),
])

print("Total signups:             ", q("SELECT COUNT(*) FROM users"))
print("Real users (no test accts):", q(f"SELECT COUNT(*) FROM users WHERE {TEST_ACCOUNT_FILTER}"))

# Every funnel step below is restricted to real users too. Counting your own
# testing as engagement is how a dead funnel looks alive.
real = f"user_id IN (SELECT id FROM users WHERE {TEST_ACCOUNT_FILTER})"
print("Completed onboarding:      ", q(
    f"SELECT COUNT(*) FROM users WHERE onboarding_complete AND {TEST_ACCOUNT_FILTER}"))
print("Took a quiz:               ", q(f"SELECT COUNT(DISTINCT user_id) FROM quiz_results WHERE {real}"))
print("Did a speaking session:    ", q(f"SELECT COUNT(DISTINCT user_id) FROM speaking_sessions WHERE {real}"))

excluded = rows(f"SELECT name, email FROM users WHERE NOT ({TEST_ACCOUNT_FILTER}) ORDER BY created_at")
print(f"\nExcluded as yours/test ({len(excluded)}) — check this list stays right:")
for name, email in excluded:
    print(f"  {(name or '?')[:22]:<22} {email}")

# "When did anyone last sign up" is the question a total can't answer: 17 users
# reads the same whether the last one arrived yesterday or in March. Test
# accounts are excluded here for the same reason they are from the count above —
# they'd otherwise show up as the most recent signup and hide the real one.
latest = rows(
    f"SELECT name, email, created_at, onboarding_complete FROM users "
    f"WHERE {TEST_ACCOUNT_FILTER} ORDER BY created_at DESC LIMIT 5"
)
print("\nLatest signups (newest first):")
if not latest:
    print("  none yet")
for name, email, created_at, done in latest:
    stage = "onboarded" if done else "signed up only"
    print(f"  {created_at:%Y-%m-%d %H:%M}  {(name or '?')[:22]:<22} {email:<34} {stage}")

# The only question a signup count cannot answer: did anybody COME BACK. A return
# visit leaves no other trace unless the user finishes a quiz or a speaking session,
# and hardly anyone does — so without last_seen_at, three logins and one login look
# identical. NULL means "not seen since the column existed", not "never active":
# rows are deliberately not back-filled from created_at, because dating a return
# visit that never happened is worse than admitting we don't know.
try:
    returned = rows(
        f"SELECT name, email, created_at, last_seen_at FROM users "
        f"WHERE {TEST_ACCOUNT_FILTER} AND last_seen_at IS NOT NULL "
        f"ORDER BY last_seen_at DESC LIMIT 8"
    )
    print("\nLast seen (real users — the only signal that anyone came back):")
    if not returned:
        print("  nobody tracked yet — the column starts filling on the next login")
    for name, email, created_at, seen in returned:
        days = (seen.date() - created_at.date()).days
        note = "same day as signup" if days == 0 else f"{days}d after signup"
        print(f"  {seen:%Y-%m-%d}  {(name or '?')[:22]:<22} {email:<34} {note}")
except Exception as e:
    conn.rollback()
    print("\nLast seen: column not deployed yet —", e)

# Groq's free tier is a per-model tokens-per-day cap and unused quota does NOT
# carry over, so the useful question is always "how much of today has gone, on
# which model" — asked before a 429 answers it for you.
try:
    usage = rows(
        "SELECT model, SUM(total_tokens), SUM(calls), SUM(truncated_calls) "
        "FROM groq_usage WHERE day = CURRENT_DATE GROUP BY model "
        "ORDER BY SUM(total_tokens) DESC"
    )
    print("\nGroq tokens used today (per model — quota is tracked per model):")
    if not usage:
        print("  none yet")
    for model, total, calls, truncated in usage:
        warn = f"  <-- {truncated} TRUNCATED (spend that bought nothing)" if truncated else ""
        print(f"  {model:<32} {total:>8,} tokens  {calls:>3} calls{warn}")

    features = rows(
        "SELECT label, SUM(total_tokens) FROM groq_usage WHERE day = CURRENT_DATE "
        "GROUP BY label ORDER BY SUM(total_tokens) DESC LIMIT 8"
    )
    if features:
        print("\nWhere today's tokens went:")
        for label, total in features:
            print(f"  {label:<32} {total:>8,}")
except Exception as e:
    # The table only exists once the app has booted against this database.
    conn.rollback()
    print("\nGroq usage: not available yet —", e)

conn.close()
