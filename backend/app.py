from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Load .env next to this file so the app works regardless of the working
# directory it's launched from (e.g. `python backend/app.py` from the repo root).
load_dotenv(os.path.join(BASE_DIR, ".env"))

from models import db, User
from auth import current_user, login_required
from session_controller import session_bp
from persona_bp import persona_bp
from ai_quiz_bp import quiz_bp
from communication_bp import comm_bp

app = Flask(__name__)

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
# In production we refuse to boot on the shared dev default rather than sign real
# sessions with a guessable key that also wouldn't survive a config change.
SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set. Configure a persistent secret (e.g. the "
            "Render env var from render.yaml) so signed session cookies stay valid "
            "across restarts and workers."
        )
    SECRET_KEY = "dev-only-change-me"  # local dev only — stable, never shipped
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
    db.create_all()

# Register Blueprints
app.register_blueprint(session_bp)
app.register_blueprint(persona_bp)
app.register_blueprint(quiz_bp)
app.register_blueprint(comm_bp)

# Frontend folder — resolved relative to this file, works from any working directory
FRONTEND_FOLDER = os.path.join(os.path.dirname(BASE_DIR), "frontend")


# --- Serve HTML pages ---
@app.route("/")
def index():
    return send_from_directory(FRONTEND_FOLDER, "index.html")


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
    user = User.query.filter_by(email=email).first()
    if user and user.check_password(password):
        session["user_id"] = user.id
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
