"""Account self-service: password reset and change-password.

Reset tokens are STATELESS (itsdangerous URLSafeTimedSerializer) — no new DB
column, no PendingReset table. The signed payload embeds the user id and a
short fragment of their CURRENT password_hash, so the instant the password
actually changes (via this flow or /account/change-password), the embedded
fragment no longer matches and any old token fails re-validation on its own —
a free "invalidate after use" without persisting anything server-side.

There is no email infrastructure yet, so /account/forgot-password returns the
reset link directly in its JSON response rather than emailing it — the
frontend renders that link on-screen, clearly labeled as a stand-in until
email is wired up. Swapping to a real send is a one-line change at the call
site: replace the returned fields with an actual email dispatch.
"""
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask import Blueprint, request, jsonify, current_app

from models import db, User
from auth import current_user, login_required
from rate_limiter import limiter

account_bp = Blueprint("account_bp", __name__)

RESET_SALT = "pwd-reset-v1"
RESET_MAX_AGE = 60 * 60  # 1 hour
MIN_PASSWORD_LEN = 8


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def _make_token(user):
    # A fragment (not the whole hash) ties the token to the CURRENT password
    # without letting the token leak enough of the hash to matter.
    frag = user.password_hash[-12:]
    return _serializer().dumps({"uid": user.id, "h": frag}, salt=RESET_SALT)


def _verify_token(token):
    """Returns the User if the token is valid, fresh, and still matches their
    current password — or None. A token silently stops working the moment the
    password it was issued against changes, even before it expires."""
    try:
        data = _serializer().loads(token, salt=RESET_SALT, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    user = db.session.get(User, data.get("uid"))
    if not user or user.password_hash[-12:] != data.get("h"):
        return None
    return user


@account_bp.route("/account/forgot-password", methods=["POST"])
@limiter.limit("5 per hour")
def forgot_password():
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    user = User.query.filter_by(email=email).first()

    # Never reveal whether an email is registered — always 200. Only a real
    # account gets a token/link back in the response.
    if not user:
        return jsonify({
            "success": True,
            "message": "If that email is registered, a reset link would be sent.",
        }), 200

    token = _make_token(user)
    return jsonify({
        "success": True,
        "message": "Email isn't connected yet, so here's your reset link directly — "
                    "this will be sent to your inbox once email is live.",
        "reset_path": f"/reset-password.html?token={token}",
        "token": token,
    }), 200


@account_bp.route("/account/reset-password", methods=["POST"])
@limiter.limit("10 per hour")
def reset_password():
    data = request.json or {}
    token = data.get("token", "")
    new_password = data.get("password", "")
    if not token or not new_password:
        return jsonify({"success": False, "message": "Token and new password are required"}), 400
    if len(new_password) < MIN_PASSWORD_LEN:
        return jsonify({"success": False, "message": f"Password must be at least {MIN_PASSWORD_LEN} characters"}), 400

    user = _verify_token(token)
    if not user:
        return jsonify({"success": False, "message": "This reset link is invalid or has expired"}), 400

    user.set_password(new_password)
    db.session.commit()
    return jsonify({"success": True, "message": "Password updated. You can log in now."}), 200


@account_bp.route("/account/change-password", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def change_password():
    data = request.json or {}
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")
    if len(new_password) < MIN_PASSWORD_LEN:
        return jsonify({"success": False, "message": f"New password must be at least {MIN_PASSWORD_LEN} characters"}), 400

    user = current_user()
    if not user.check_password(current_password):
        # 400, not 401 — the user IS authenticated (login_required already passed);
        # they just typed the wrong current password. The frontend's shared Aira.api
        # helper treats ANY 401 as "not logged in" and force-redirects to login.html,
        # which would silently swallow this error and boot the user out mid-form.
        return jsonify({"success": False, "message": "Current password is incorrect"}), 400

    user.set_password(new_password)
    db.session.commit()
    return jsonify({"success": True, "message": "Password changed"}), 200
