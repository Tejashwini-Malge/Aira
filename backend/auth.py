"""Session-based auth helpers shared across blueprints.

Login stores the user id in Flask's signed session cookie. Protected
endpoints use @login_required and read the user via current_user().
"""
from datetime import datetime
from functools import wraps
from flask import session, jsonify
from models import User


def current_user():
    """Return the logged-in User, or None."""
    uid = session.get("user_id")
    if uid is None:
        return None
    return db_session_get(uid)


def db_session_get(uid):
    # Imported lazily to avoid import-time coupling with the db instance.
    from models import db
    return db.session.get(User, uid)


def touch_last_seen(uid):
    """Record that this account was seen today. At most one write per user per day.

    WHY DAY GRANULARITY. The question the column exists to answer is "did they come
    back on a different day". Writing on every authenticated request would add a
    database write to every request in the app to sharpen a signal nobody needs
    sharper.

    WHY IT RUNS BEFORE THE HANDLER. The commit here flushes the whole session. Run
    mid-handler it would save a half-finished unit of work the handler had not
    decided to save yet — a request that later fails would leave part of itself
    behind. Before the handler, there is nothing pending but this.

    Failures are swallowed on purpose: this is a statistic, and a statistic must
    never turn a working request into a 500 for the user in front of it.
    """
    from models import db
    try:
        user = db.session.get(User, uid)
        if user is None:
            return
        now = datetime.utcnow()
        if user.last_seen_at and user.last_seen_at.date() == now.date():
            return
        user.last_seen_at = now
        db.session.commit()
    except Exception:
        db.session.rollback()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        uid = session.get("user_id")
        if uid is None:
            return jsonify({"success": False, "message": "Not authenticated"}), 401
        touch_last_seen(uid)
        return fn(*args, **kwargs)
    return wrapper
