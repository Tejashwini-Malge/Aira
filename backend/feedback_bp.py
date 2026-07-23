"""Pre-report feedback capture — TEMPORARY, for market validation only.

A quick review sheet (frontend/feedback.html) sits in front of every path
that leads to report.html, so real signal (rating, NPS, indirect MCQs, free
text) gets collected in the moment instead of relying on users to volunteer
it after they've already left. This is a validation instrument, not a
permanent review/UGC feature — see models.Feedback and ../export_feedback.py.

Shown at most once per calendar day per user — /feedback/status lets the
frontend skip straight to the report on a repeat visit the same day instead
of re-asking every single time.
"""
from datetime import datetime

from flask import Blueprint, request, jsonify

from models import db, Feedback
from auth import current_user, login_required
from rate_limiter import limiter

feedback_bp = Blueprint("feedback_bp", __name__)

_VALID_CONTEXTS = {"quiz", "speaking", "onboarding", "general"}
_MAX_TEXT_LEN = 2000

# Deliberately indirect — none of these ask "did you like it" outright. Each
# is aimed at a specific product question we actually need answered next:
# does the adaptive difficulty/scoring/redo mechanic register as valuable,
# does a session move someone's actual confidence (not just their mood), and
# what pulls a user back tomorrow (curiosity, a fixable weak spot, habit, or
# nothing yet). Server-side validated so only known question/option pairs
# ever get persisted.
MCQ_QUESTIONS = {
    "confidence_shift": {
        "prompt": "Compared to before this session, how ready do you feel to "
                   "face something similar for real?",
        "options": [
            "Noticeably more ready",
            "A little more ready",
            "About the same",
            "Less sure, honestly",
        ],
    },
    "keep_one_part": {
        "prompt": "If you had to keep just ONE part of today, which would you keep?",
        "options": [
            "The way the questions adjusted to me",
            "The score breakdown",
            "The redo / retry",
            "The resources and tips at the end",
        ],
    },
    "return_reason": {
        "prompt": "What's most likely to bring you back tomorrow?",
        "options": [
            "Curiosity about my next score",
            "Wanting to fix my weak spot",
            "A reminder or habit",
            "Nothing in particular yet",
        ],
    },
}


@feedback_bp.route("/feedback/questions", methods=["GET"])
@login_required
def feedback_questions():
    return jsonify({"questions": {
        key: {"prompt": q["prompt"], "options": q["options"]}
        for key, q in MCQ_QUESTIONS.items()
    }}), 200


@feedback_bp.route("/feedback/status", methods=["GET"])
@login_required
def feedback_status():
    start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    given_today = db.session.query(Feedback.id).filter(
        Feedback.user_id == current_user().id,
        Feedback.created_at >= start_of_day,
    ).first() is not None
    return jsonify({"given_today": given_today}), 200


@feedback_bp.route("/feedback", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def submit_feedback():
    """Every field here is required — this sheet is the one guaranteed touch
    point before a user reaches their report, and a partial response (skipped
    NPS, blank text) is much weaker signal for market validation than a
    completed one. Enforced server-side since the frontend's "required"
    styling is only a UX nicety, not something a caller has to respect.
    """
    data = request.get_json() or {}

    try:
        rating = int(data.get("rating"))
    except (TypeError, ValueError):
        rating = None
    if rating is None or not (1 <= rating <= 5):
        return jsonify({"success": False, "message": "A 1-5 rating is required."}), 400

    try:
        recommend_score = int(data.get("recommend_score"))
    except (TypeError, ValueError):
        recommend_score = None
    if recommend_score is None or not (0 <= recommend_score <= 10):
        return jsonify({"success": False, "message": "A 0-10 recommend score is required."}), 400

    context = str(data.get("context") or "general").strip().lower()
    if context not in _VALID_CONTEXTS:
        context = "general"

    # Only ever persist question/option pairs we actually defined — a client
    # can't inject arbitrary keys or free-text into what's meant to stay a
    # clean, closed-option dataset. All three must be answered.
    raw_mcq = data.get("mcq_responses") or {}
    mcq_responses = {}
    if isinstance(raw_mcq, dict):
        for key, question in MCQ_QUESTIONS.items():
            chosen = raw_mcq.get(key)
            if chosen in question["options"]:
                mcq_responses[key] = chosen
    if len(mcq_responses) != len(MCQ_QUESTIONS):
        return jsonify({"success": False, "message": "Please answer all three quick questions."}), 400

    liked = (data.get("liked") or "").strip()[:_MAX_TEXT_LEN]
    improve = (data.get("improve") or "").strip()[:_MAX_TEXT_LEN]
    if not liked or not improve:
        return jsonify({"success": False, "message": "Both text fields are required."}), 400

    row = Feedback(
        user_id=current_user().id,
        rating=rating,
        recommend_score=recommend_score,
        liked=liked,
        improve=improve,
        mcq_responses=mcq_responses,
        context=context,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({"success": True}), 200
