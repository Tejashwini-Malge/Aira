"""Session-based auth helpers shared across blueprints.

Login stores the user id in Flask's signed session cookie. Protected
endpoints use @login_required and read the user via current_user().
"""
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


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("user_id") is None:
            return jsonify({"success": False, "message": "Not authenticated"}), 401
        return fn(*args, **kwargs)
    return wrapper
