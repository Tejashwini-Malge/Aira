"""Per-user data endpoints: speaking sessions and the aggregated report.

(The old body-based /persona/generate lived here but was unused by the
frontend, which uses /session/generate-persona. These routes replace it.)
"""
from flask import Blueprint, request, jsonify

from models import db, SpeakingSession
from auth import current_user, login_required

persona_bp = Blueprint("persona_bp", __name__)


# Speaking sessions are only ever written server-side by /comm/evaluate — there is
# deliberately no POST here, so the client can never self-report scores.
@persona_bp.route("/me/speaking", methods=["GET"])
@login_required
def list_speaking():
    rows = current_user().speaking_sessions
    return jsonify({"sessions": [s.to_dict() for s in rows]}), 200


@persona_bp.route("/me/report", methods=["GET"])
@login_required
def report():
    user = current_user()
    persona = user.persona
    quizzes = [q.to_dict() for q in user.quiz_results]
    speaking = [s.to_dict() for s in user.speaking_sessions]

    avg = None
    if speaking:
        def _avg(key):
            vals = [s[key] for s in speaking if s[key] is not None]
            return round(sum(vals) / len(vals), 1) if vals else None
        avg = {"fluency": _avg("fluency"), "clarity": _avg("clarity"), "confidence": _avg("confidence")}

    return jsonify({
        "user": user.to_dict(),
        "persona": persona.to_dict() if (persona and persona.summary) else None,
        "quizzes": quizzes,
        "speaking": speaking,
        "speaking_averages": avg,
    }), 200
