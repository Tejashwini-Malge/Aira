from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
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
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")

# DATABASE_URL is set by the host (Render/Railway) when a managed Postgres
# instance is attached. Falls back to a local SQLite file for local dev.
# Older Postgres URLs use the "postgres://" scheme, which SQLAlchemy 1.4+
# no longer accepts — normalize to "postgresql://".
database_url = os.getenv("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "aira.db"))
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Same-origin session cookie (frontend is served by this app). In production
# (behind HTTPS) the cookie is also marked Secure so it's never sent over plain http.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV") == "production"

# Credentials must be allowed for the session cookie to round-trip.
CORS(app, supports_credentials=True)

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
