"""Account self-service: password reset and change-password.

Reset tokens are STATELESS (itsdangerous URLSafeTimedSerializer) — no new DB
column, no PendingReset table. The signed payload embeds the user id and a
short fragment of their CURRENT password_hash, so the instant the password
actually changes (via this flow or /account/change-password), the embedded
fragment no longer matches and any old token fails re-validation on its own —
a free "invalidate after use" without persisting anything server-side.

There is no email infrastructure yet, so /account/forgot-password has NO
self-service path: it accepts the address, says reset-by-email isn't live, and
mints nothing. It used to return the reset link in its own JSON response, which
meant anyone who posted a registered address received a valid reset token and
could take the account over; that is why the token is gone rather than merely
hidden in the UI. Wiring a real send is a one-line change at the marked call
site in forgot_password().

/account/reset-password still verifies any validly signed token, so an
out-of-band reset works today. From the backend/ directory, against the target
database, hand the user the printed path over a channel you trust:

    python -c "import app;from models import User;from account_bp import _make_token; \\
      app.app.app_context().push();u=User.query.filter_by(email='them@x.com').first(); \\
      print('/reset-password.html?token='+_make_token(u))"

The token is single-use by construction (see above) and expires in an hour.
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

# Same reply for a registered and an unregistered address, so the response can't
# be used to test whether an account exists. Says what actually happens rather
# than promising an email that no one sends.
NO_EMAIL_MESSAGE = (
    "Password reset by email isn't live yet. Get in touch and we'll reset it for you."
)


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

    # Never reveal whether an email is registered: identical body, identical
    # status, both branches. (This guard used to be undone two lines later by
    # handing the token back only for real accounts — the presence of the token
    # WAS the enumeration signal, on top of being the takeover itself.)
    if not user:
        return jsonify({"success": True, "message": NO_EMAIL_MESSAGE}), 200

    # A real send goes HERE, and nowhere else:
    #     send_reset_email(user.email, _make_token(user))
    # Until then this route deliberately mints nothing. Returning the token to
    # the caller (which it used to do) meant anyone who posted a registered
    # email got a working reset link back in the response — unauthenticated
    # takeover of any account whose address you could guess. Logging it instead
    # is the same secret in a different place, so we don't do that either.
    return jsonify({
        "success": True,
        "message": NO_EMAIL_MESSAGE,
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
