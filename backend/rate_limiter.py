"""Shared Flask-Limiter instance.

Blueprints (session_controller, ai_quiz_bp, communication_bp) are imported at
the top of app.py, before the Flask `app` object exists, and they decorate
their own routes with @limiter.limit(...) at import time. So this object is
created without an app (Flask-Limiter's deferred-init pattern) — app.py calls
limiter.init_app(app) once `app` exists.

In-memory storage (the default) is correct as long as the app runs as a single
process — see render.yaml, which starts gunicorn with no --workers flag (one
worker). If that ever changes to multiple workers/dynos, this needs a shared
backend (e.g. Redis) or each worker enforces its own separate limit.
"""
from flask import session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


def _rate_limit_key():
    """Key by the logged-in user, not raw IP — a shared campus/hostel network
    behind one IP shouldn't throttle every student together, and a script can't
    dodge the limit just by rotating IPs while logged in as the same account."""
    uid = session.get("user_id")
    return f"user:{uid}" if uid else get_remote_address()


limiter = Limiter(
    key_func=_rate_limit_key,
    # Safety net only — every Groq-costing route already sets its own tighter
    # @limiter.limit(...) reflecting its actual cost, and those override this.
    # This just caps whatever route a future PR forgets to decorate, so a bug
    # (or a script hammering some new endpoint) can't go fully unbounded.
    # High enough that no legitimate GET/POST pattern in the app should ever hit it.
    default_limits=["300 per hour"],
)
