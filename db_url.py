"""Where the production database URL comes from, for the root-level scripts.

check_stats.py, export_feedback.py and export_feedback_to_sheets.py each used to
carry their own copy of "read DB_URL from the environment, else scrape it out of
backend/.env". Three copies of one rule is three places to fix when the rule
changes — and it did change, for the reason below.

RENDER HANDS OUT TWO HOSTNAMES for the same instance:

    internal   dpg-xxxxxxxx-a
    external   dpg-xxxxxxxx-a.<region>-postgres.render.com

The internal one resolves only from inside Render's own network. A DB_URL
holding that form fails on a laptop with

    could not translate host name "dpg-xxxxxxxx-a" to address

which reads like the database is down rather than like the URL is the wrong one
of two valid URLs. backend/.env in this repo has exactly that split: DB_URL
internal, DATABASE_URL external, same database. So resolution prefers whichever
value is actually reachable from here instead of dying on the first one found.

These are operator scripts pointed at PRODUCTION. They only ever read.
"""
import os

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend", ".env")


def from_env_file(*keys):
    """First of `keys` present in backend/.env, so a credential can live in a
    local file instead of being pasted into a shell command."""
    if not os.path.exists(_ENV_PATH):
        return None
    found = {}
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            for key in keys:
                if line.startswith(key + "="):
                    found[key] = line.split("=", 1)[1].strip().strip('"').strip("'")
    for key in keys:
        if found.get(key):
            return found[key]
    return None


def _looks_reachable(url):
    """A Render URL is reachable from outside only in its external form. Local
    URLs (localhost/127.0.0.1) and non-Render hosts are assumed fine."""
    if not url:
        return False
    if "render.com" in url:
        return True
    return "dpg-" not in url


def resolve(verbose=True):
    """The database URL to use, or None. DB_URL wins as an explicit override;
    if it holds Render's internal hostname, fall through to DATABASE_URL's
    external one rather than failing with an unhelpful DNS error."""
    url = os.environ.get("DB_URL") or from_env_file("DB_URL", "DATABASE_URL")
    if not _looks_reachable(url):
        external = from_env_file("DATABASE_URL")
        if _looks_reachable(external):
            if verbose:
                print("DB_URL is Render's internal hostname (not reachable from "
                      "here) — using DATABASE_URL's external host instead.\n")
            url = external
    return url


MISSING_MESSAGE = (
    'Set DB_URL first ($env:DB_URL = "postgresql://...") '
    "or add a DB_URL=... line to backend/.env"
)
