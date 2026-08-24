"""Per-user data endpoints: speaking sessions and the aggregated report.

(The old body-based /persona/generate lived here but was unused by the
frontend, which uses /session/generate-persona. These routes replace it.)
"""
import re

from flask import Blueprint, request, jsonify

from datetime import datetime, timedelta

from models import db, PendingAssessment, ReportSnapshot
from auth import current_user, login_required
from persona_eligibility import total_sessions as _total_sessions, refresh_eligible as _refresh_eligible

persona_bp = Blueprint("persona_bp", __name__)


# Speaking sessions are only ever written server-side by /comm/evaluate — there is
# deliberately no POST here, so the client can never self-report scores.
@persona_bp.route("/me/speaking", methods=["GET"])
@login_required
def list_speaking():
    rows = current_user().speaking_sessions
    return jsonify({"sessions": [s.to_dict() for s in rows]}), 200


def _build_report_payload(user):
    """Same shape returned by GET /me/report — shared with snapshot saving so
    a saved snapshot and the live view are never built from diverging logic.
    """
    persona = user.persona
    quizzes = [q.to_dict() for q in user.quiz_results]
    speaking = [s.to_dict() for s in user.speaking_sessions]

    avg = None
    if speaking:
        def _avg(key):
            vals = [s[key] for s in speaking if s[key] is not None]
            return round(sum(vals) / len(vals), 1) if vals else None
        avg = {"fluency": _avg("fluency"), "clarity": _avg("clarity"), "confidence": _avg("confidence")}

    # Weak areas that show up across BOTH quizzes and speaking sessions — the
    # habits worth calling out on a report card, not just a single-session blip.
    weak_counts = {}
    for row in quizzes + speaking:
        for area in (row.get("weak_areas") or []):
            key = str(area).strip()
            if not key:
                continue
            weak_counts[key] = weak_counts.get(key, 0) + 1
    recurring_weak_areas = sorted(
        [{"area": area, "count": count} for area, count in weak_counts.items() if count > 1],
        key=lambda r: r["count"], reverse=True,
    )

    return {
        "user": user.to_dict(),
        "onboarding": user.onboarding or {},
        "persona": persona.to_dict() if (persona and persona.summary) else None,
        "quizzes": quizzes,
        "quiz_history": quizzes,
        "speaking": speaking,
        "speaking_history": speaking,
        "speaking_averages": avg,
        "recurring_weak_areas": recurring_weak_areas,
    }


# Work the user began and never saw the end of. PendingAssessment already holds
# it — /quiz/evaluate and /comm/evaluate read the row back so they grade against
# the questions WE issued — but nothing has ever shown it to the user. Someone who
# closed the tab mid-paper simply never heard about it again.
#
# This is the cheapest return in the product: no persuasion needed, because they
# already chose to start and the work is half done. It beats nudging an idle user,
# who has to be talked into wanting it at all.
_PENDING_KINDS = {
    "quiz": {"label": "Mock interview", "page": "ai_quiz.html"},
    "comm": {"label": "Speaking practice", "page": "soft_skills.html"},
}

# Past this, an abandoned paper is archaeology rather than an invitation, and
# "pick up where you left off" about something from two months ago reads as the
# product having lost track rather than having remembered.
_PENDING_MAX_AGE_DAYS = 21


def _unfinished(user):
    rows = PendingAssessment.query.filter_by(user_id=user.id).all()
    cutoff = datetime.utcnow() - timedelta(days=_PENDING_MAX_AGE_DAYS)
    out = []
    for row in rows:
        kind = _PENDING_KINDS.get(row.kind)
        if not kind or row.updated_at is None or row.updated_at < cutoff:
            continue
        payload = row.payload or {}
        out.append({
            "kind": row.kind,
            # The quiz payload carries the paper's own topic; fall back to the
            # generic label so a malformed payload still produces a usable card.
            "label": (payload.get("topic") or kind["label"]) if isinstance(payload, dict) else kind["label"],
            "page": kind["page"],
            "updatedAt": row.updated_at.isoformat(),
            "daysAgo": (datetime.utcnow() - row.updated_at).days,
        })
    out.sort(key=lambda item: item["updatedAt"], reverse=True)
    return out


# Streak days are counted in India Standard Time, not UTC.
#
# created_at is datetime.utcnow() and IST is UTC+5:30, so a student practising at
# 1am local time is stamped 19:30 the PREVIOUS day in UTC. Counting in UTC would
# split one late-night session away from the day it belonged to and break streaks
# for practising at night — which is exactly when students practise. A streak that
# resets for a reason the user cannot see is worse than having no streak at all.
#
# A constant, not a per-user setting, because every user so far is in India. If the
# audience broadens this has to become per-user — see the audience-broadening note.
_STREAK_TZ_OFFSET = timedelta(hours=5, minutes=30)


def _practice_days(user):
    """The distinct LOCAL dates this user actually practised on.

    Practice, not visits: a streak built from logins rewards opening the app and
    closing it again, which is the opposite of what a practice product wants.
    """
    stamps = [q.created_at for q in user.quiz_results if q.created_at]
    stamps += [s.created_at for s in user.speaking_sessions if s.created_at]
    return {(stamp + _STREAK_TZ_OFFSET).date() for stamp in stamps}


def _streak(user):
    days = _practice_days(user)
    today = (datetime.utcnow() + _STREAK_TZ_OFFSET).date()
    if not days:
        return {"current": 0, "totalDays": 0, "practisedToday": False}

    # A streak is not broken until a whole day has passed with nothing in it. At 9am
    # someone who practised yesterday still has all of today to keep it going, so the
    # count starts at yesterday when today is empty. Without that, the number would
    # drop to zero every morning and climb back every evening, which teaches people
    # to distrust it.
    cursor = today if today in days else today - timedelta(days=1)
    current = 0
    while cursor in days:
        current += 1
        cursor -= timedelta(days=1)

    return {
        "current": current,
        "totalDays": len(days),
        "practisedToday": today in days,
    }


@persona_bp.route("/me/report", methods=["GET"])
@login_required
def report():
    user = current_user()
    payload = _build_report_payload(user)
    # Live-only, like the persona flags below: a report card archived in March must
    # not still claim there is a paper waiting to be finished.
    payload["unfinished"] = _unfinished(user)
    payload["streak"] = _streak(user)
    # Live-only fields (not baked into _build_report_payload, since that's also
    # used to persist historical snapshots — a frozen "can refresh" flag inside
    # an old report card wouldn't mean anything once time has passed).
    if payload["persona"] is not None:
        total = _total_sessions(user)
        payload["persona"]["totalSessions"] = total
        payload["persona"]["canRefresh"] = _refresh_eligible(total, user.persona)
    return jsonify(payload), 200


# ── Report-card grading — mirrors the mark-sheet math in report.html so a
# snapshot's summary (grade/result shown in the history list) matches what the
# live page would have shown at save time. ──
def _grade_for(pct):
    if pct is None:
        return None
    if pct >= 90: return "A+"
    if pct >= 80: return "A"
    if pct >= 70: return "B+"
    if pct >= 60: return "B"
    if pct >= 50: return "C"
    if pct >= 35: return "D"
    return "E"


def _parse_score_pct(score):
    if score is None:
        return None
    s = str(score)
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", s)
    if m:
        return round((float(m.group(1)) / float(m.group(2))) * 100)
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    if m:
        return round(float(m.group(1)))
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if m:
        n = float(m.group(1))
        if n <= 10:
            return round(n * 10)
        if n <= 100:
            return round(n)
    return None


def _summarize(payload):
    """Overall grade/pct/result for a report payload — the same rollup the
    mark sheet computes client-side, ported so the snapshot list can show a
    grade without the frontend re-fetching and recomputing every payload.
    """
    persona = payload.get("persona")
    quizzes = payload.get("quizzes") or []
    speaking = payload.get("speaking") or []
    quiz = quizzes[0] if quizzes else None

    persona_pct = None
    if persona and persona.get("dimensions"):
        dims = persona["dimensions"]
        strong = sum(1 for d in dims.values() if (d or {}).get("level") == "Strong")
        developing = sum(1 for d in dims.values() if (d or {}).get("level") == "Developing")
        total = strong + developing
        if total:
            persona_pct = round((strong / total) * 100)

    quiz_pct = _parse_score_pct(quiz["score"]) if quiz and quiz.get("score") else None

    skills_pct = None
    if speaking:
        vals = []
        for s in speaking:
            for k in ("fluency", "clarity", "confidence", "structure"):
                if s.get(k) is not None:
                    vals.append(s[k])
        if vals:
            skills_pct = round((sum(vals) / len(vals)) * 10)

    pcts = [p for p in (persona_pct, quiz_pct, skills_pct) if p is not None]
    overall_pct = round(sum(pcts) / len(pcts)) if pcts else None
    overall_grade = _grade_for(overall_pct)

    persona_done = bool(persona and persona.get("summary"))
    mcq_done = quiz is not None
    skills_done = bool(speaking)
    if persona_done and mcq_done and skills_done:
        result_text = "All periods complete"
    else:
        done_count = sum([persona_done, mcq_done, skills_done])
        result_text = f"{done_count} of 3 periods complete"

    return overall_pct, overall_grade, result_text


def _activity_signature(payload):
    """A fingerprint of the ACTIVITY behind a report — what actually changes only
    when the user does something new: takes/retakes a quiz, completes a speaking
    round, or (re)builds their persona. Merely viewing the report doesn't move it.
    """
    persona = payload.get("persona") or {}
    return (
        len(payload.get("quizzes") or []),
        len(payload.get("speaking") or []),
        (persona.get("summary") or "").strip(),
    )


@persona_bp.route("/me/report/snapshot", methods=["POST"])
@login_required
def save_report_snapshot():
    user = current_user()
    payload = _build_report_payload(user)

    # Archive a snapshot ONLY when the underlying activity has actually changed since
    # the last one — a new quiz, a new speaking round, or a rebuilt persona. Reloading
    # or revisiting the report changes nothing, so it must not create a new row (this is
    # what filled the history with dozens of identical "All periods complete" entries).
    latest = ReportSnapshot.query.filter_by(user_id=user.id) \
        .order_by(ReportSnapshot.created_at.desc()).first()
    if latest and _activity_signature(latest.payload) == _activity_signature(payload):
        return jsonify({"success": True, "created": False, "snapshot": latest.to_summary()}), 200

    overall_pct, overall_grade, result_text = _summarize(payload)
    snap = ReportSnapshot(
        user_id=user.id, payload=payload,
        overall_pct=overall_pct, overall_grade=overall_grade, result_text=result_text,
    )
    db.session.add(snap)
    db.session.commit()
    return jsonify({"success": True, "created": True, "snapshot": snap.to_summary()}), 201


@persona_bp.route("/me/report/snapshots", methods=["GET"])
@login_required
def list_report_snapshots():
    rows = ReportSnapshot.query.filter_by(user_id=current_user().id) \
        .order_by(ReportSnapshot.created_at.desc()).all()
    return jsonify({"snapshots": [r.to_summary() for r in rows]}), 200


@persona_bp.route("/me/report/snapshots/<int:snap_id>", methods=["GET"])
@login_required
def get_report_snapshot(snap_id):
    row = ReportSnapshot.query.filter_by(id=snap_id, user_id=current_user().id).first()
    if not row:
        return jsonify({"message": "Report card not found."}), 404
    return jsonify({"snapshot": row.to_summary(), "payload": row.payload}), 200
