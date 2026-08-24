from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import timedelta
import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Load .env next to this file so the app works regardless of the working
# directory it's launched from (e.g. `python backend/app.py` from the repo root).
load_dotenv(os.path.join(BASE_DIR, ".env"))

from models import db, User, migrate_new_columns
from groq_client import set_usage_sink
from auth import current_user, login_required
from rate_limiter import limiter
from session_controller import session_bp
from persona_bp import persona_bp
from ai_quiz_bp import quiz_bp
from communication_bp import comm_bp
from account_bp import account_bp, MIN_PASSWORD_LEN
from feedback_bp import feedback_bp

app = Flask(__name__)
limiter.init_app(app)

IS_PRODUCTION = os.getenv("FLASK_ENV") == "production"

# Render (and most hosts) terminate TLS at a proxy and forward plain HTTP to the
# app, with the real scheme in X-Forwarded-Proto. Without this, request.is_secure
# is always False behind the proxy, which breaks any scheme-aware redirect and
# makes the app think an HTTPS request arrived over HTTP. Trust one proxy hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# The session cookie is a *signed* cookie — its signature depends on SECRET_KEY.
# If the key changes between the request that set the cookie and the one that
# reads it (e.g. a key randomly generated at startup, so it differs on every
# restart / across workers), the signature no longer verifies, Flask silently
# discards the session, and the user looks logged out on their very next click.
# So the key MUST come from a persistent env var and stay identical everywhere.
#
# Always required — no dev-only fallback. A fallback value here would be a
# fixed, publicly-readable string (it's right here in the source), so if
# IS_PRODUCTION were ever wrong (FLASK_ENV unset/mistyped on some future host)
# the app would silently sign real user sessions with a key anyone could read
# out of this file — a full session-forgery hole with no error to point at it.
# Requiring the var unconditionally means that failure mode can't exist: a
# misconfigured host fails loudly at boot instead of shipping quietly broken.
SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set. Generate one (e.g. "
        "python -c \"import secrets;print(secrets.token_hex(32))\") and add it "
        "to backend/.env for local dev, or as the Render env var in production."
    )
app.config["SECRET_KEY"] = SECRET_KEY

# DATABASE_URL is set by the host (Render/Railway) when a managed Postgres
# instance is attached. Falls back to a local SQLite file for local dev.
# Older Postgres URLs use the "postgres://" scheme, which SQLAlchemy 1.4+
# no longer accepts — normalize to "postgresql://".
database_url = os.getenv("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "aira.db"))
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# The frontend is served by THIS app, so the session cookie is same-origin.
#   HTTPONLY : JS can't read it (XSS can't steal the session).
#   SAMESITE : "Lax" is correct for a same-origin app. Only a cross-site frontend
#              (different domain calling this API) would need "None" — and "None"
#              REQUIRES Secure, so it can't be combined with plain HTTP.
#   SECURE   : in production the cookie is only sent over HTTPS. Thanks to ProxyFix
#              above, scheme detection is correct behind Render's proxy.
# Overridable via env so a future cross-site split is a config change, not a code one.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION or \
    os.getenv("SESSION_COOKIE_SAMESITE", "Lax").lower() == "none"

# "Stay signed in" (login route) keeps a session alive until explicit logout.
# True "forever" isn't achievable — Chrome caps every cookie's Max-Age/Expires
# at 400 days and silently clamps anything longer — so 400 days is the actual
# ceiling, not an arbitrary number. Only applies when session.permanent is set
# True at login; everyone else keeps today's behavior (dies at browser close).
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=400)

# CORS is only needed if a DIFFERENT origin calls this API with credentials.
# A wildcard origin ("*") is invalid with credentialed requests, so we never use
# one: enable credentialed CORS only for an explicit allowlist (CORS_ORIGINS,
# comma-separated). Same-origin requests don't trigger CORS at all, so by default
# (allowlist empty) there is nothing to configure.
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    CORS(app, resources={r"/*": {"origins": _cors_origins}}, supports_credentials=True)

db.init_app(app)
with app.app_context():
    # Imported here, after db.init_app: importing usage_tracker registers its
    # model on the same `db` metadata, and it must be registered BEFORE
    # create_all or the groq_usage table isn't created on a fresh database.
    from usage_tracker import record as _record_groq_usage

    db.create_all()
    migrate_new_columns(db.engine)


def _usage_sink(**usage):
    """Persist one Groq call's token spend. Runs inside an app context of its own
    because Groq calls happen during a request but the recorder must also work if
    a future caller runs outside one (a script, a scheduled job)."""
    with app.app_context():
        _record_groq_usage(**usage)


set_usage_sink(_usage_sink)

@app.errorhandler(429)
def rate_limited(e):
    return jsonify({
        "error": "Aira is handling a lot of requests from your account right now — please wait a bit and try again.",
    }), 429


# Register Blueprints
app.register_blueprint(session_bp)
app.register_blueprint(persona_bp)
app.register_blueprint(quiz_bp)
app.register_blueprint(comm_bp)
app.register_blueprint(account_bp)
app.register_blueprint(feedback_bp)

# Frontend folder — resolved relative to this file, works from any working directory
FRONTEND_FOLDER = os.path.join(os.path.dirname(BASE_DIR), "frontend")


# --- Serve HTML pages ---
@app.route("/")
def index():
    return send_from_directory(FRONTEND_FOLDER, "index.html")


# Browsers auto-request /favicon.ico on every page load; without this route the
# catch-all below 404s it, which shows up (deduped) in the console on every visit.
# Served inline as the chalk "A" badge so there's no binary asset to ship.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#1c3a2b"/>'
    '<text x="16" y="23" font-family="Georgia,\'Times New Roman\',serif" font-size="21"'
    ' font-weight="700" fill="#f4f1e4" text-anchor="middle">A</text></svg>'
)


@app.route("/favicon.ico")
def favicon():
    return app.response_class(_FAVICON_SVG, mimetype="image/svg+xml")


@app.route("/<path:filename>")
def serve_file(filename):
    # Try exact file first (handles assets/study.jpg, etc.)
    exact = os.path.join(FRONTEND_FOLDER, filename)
    if os.path.exists(exact) and os.path.isfile(exact):
        return send_from_directory(FRONTEND_FOLDER, filename)
    # Try as .html page (handles /login → login.html)
    html_file = f"{filename.lower()}.html"
    html_path = os.path.join(FRONTEND_FOLDER, html_file)
    if os.path.exists(html_path):
        return send_from_directory(FRONTEND_FOLDER, html_file)
    return f"Page not found: {filename}", 404


# --- Signup ---
@app.route("/signup", methods=["POST"])
def signup():
    data = request.json or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if not name or not email or not password:
        return jsonify({"success": False, "message": "Name, email and password are required"}), 400
    # Same floor the reset flow enforces, imported rather than repeated: signup
    # used to accept a one-character password, so an account could be created
    # below a length its owner could never reset back to.
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({
            "success": False,
            "message": f"Password must be at least {MIN_PASSWORD_LEN} characters",
        }), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"success": False, "message": "An account with this email already exists"}), 400

    user = User(name=name, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    session["user_id"] = user.id
    return jsonify({"success": True, "message": "Account created", **user.to_dict()})


# --- Login ---
@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    remember = bool(data.get("remember"))
    user = User.query.filter_by(email=email).first()
    if user and user.check_password(password):
        session["user_id"] = user.id
        # Explicit even for False: documents the choice (session dies at browser
        # close) rather than relying on Flask's default, which could change.
        session.permanent = remember
        return jsonify({"success": True, "name": user.name, "email": user.email})
    return jsonify({"success": False, "message": "Incorrect email or password"}), 401


# --- Logout ---
@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return jsonify({"success": True})


# --- Current user ---
@app.route("/me", methods=["GET"])
@login_required
def me():
    return jsonify({"success": True, "user": current_user().to_dict()})


if __name__ == "__main__":
    # Local dev only — in production this module is served by gunicorn (see Procfile),
    # which never calls this block, so debug mode never accidentally ships live.
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", 5000)), debug=True)
