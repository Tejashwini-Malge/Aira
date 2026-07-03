"""SQLAlchemy models and db instance for Aira.

One SQLite database (aira.db) holds users plus their per-account data:
a single persona, a history of quiz results, and a history of speaking
sessions. Passwords are stored only as Werkzeug hashes.
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Career-development details collected once at onboarding (study, goal, target
    # role, timeline, language, optional note). Stored as a flexible blob so the
    # field set can evolve without a migration. The resume itself feeds the Persona.
    onboarding = db.Column(db.JSON)
    onboarding_complete = db.Column(db.Boolean, default=False, nullable=False)

    persona = db.relationship(
        "Persona", backref="user", uselist=False,
        cascade="all, delete-orphan",
    )
    quiz_results = db.relationship(
        "QuizResult", backref="user", lazy=True,
        cascade="all, delete-orphan", order_by="QuizResult.created_at.desc()",
    )
    speaking_sessions = db.relationship(
        "SpeakingSession", backref="user", lazy=True,
        cascade="all, delete-orphan", order_by="SpeakingSession.created_at.desc()",
    )

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "onboarding_complete": self.onboarding_complete,
        }


class Persona(db.Model):
    __tablename__ = "personas"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    # Generated once after onboarding — never overwritten by this flow.
    # Future session-based updates read and write these same fields.
    title = db.Column(db.String(160))       # e.g. "Collaborative Strategic Thinker"
    summary = db.Column(db.Text)            # 1-2 sentence overview
    dimensions = db.Column(db.JSON)         # {dim_name: {level, source_id}}
    raw_responses = db.Column(db.JSON)      # the 10 structured onboarding responses

    # Resume artifacts produced by the resume agent at onboarding.
    resume_text = db.Column(db.Text)        # raw extracted text from the uploaded file
    resume_data = db.Column(db.JSON)        # {skills, projects, experience_level,
                                            #  likely_gaps, technical_questions[2],
                                            #  hr_questions[2]} — questions are tagged
                                            #  with the persona dimension they probe.

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        # No abstract "title" — the persona speaks plainly through its summary.
        return {
            "summary": self.summary,
            "dimensions": self.dimensions or {},
        }


class PendingAssessment(db.Model):
    """Server-held state for an in-flight assessment — the mock-interview question set
    or a communication session's prompt history. Evaluation grades THIS stored set,
    so a client can't forge its own transcript and have it scored into their history.
    One row per (user, kind); a new generate/start replaces the previous one.
    """
    __tablename__ = "pending_assessments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)   # "quiz" | "comm"
    payload = db.Column(db.JSON, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "kind"),)


def get_pending(user_id, kind):
    """The stored payload for this user's in-flight assessment, or None."""
    row = PendingAssessment.query.filter_by(user_id=user_id, kind=kind).first()
    return row.payload if row else None


def set_pending(user_id, kind, payload):
    """Create or replace the stored assessment state for this user."""
    from sqlalchemy.orm.attributes import flag_modified

    row = PendingAssessment.query.filter_by(user_id=user_id, kind=kind).first()
    if row is None:
        row = PendingAssessment(user_id=user_id, kind=kind)
        db.session.add(row)
    row.payload = payload
    # Callers mutate the payload they got from get_pending in place, which plain
    # JSON columns don't track — flag it so the update is never silently dropped.
    flag_modified(row, "payload")
    row.updated_at = datetime.utcnow()
    db.session.commit()


def clear_pending(user_id, kind):
    """Drop the stored state once it has been evaluated (or abandoned)."""
    PendingAssessment.query.filter_by(user_id=user_id, kind=kind).delete()
    db.session.commit()


class QuizResult(db.Model):
    __tablename__ = "quiz_results"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    topic = db.Column(db.String(255))
    score = db.Column(db.String(40))
    feedback = db.Column(db.Text)
    study_plan = db.Column(db.Text)
    weak_areas = db.Column(db.JSON, default=list)
    suggestions = db.Column(db.JSON, default=list)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "topic": self.topic,
            "score": self.score,
            "feedback": self.feedback,
            "study_plan": self.study_plan,
            "weak_areas": self.weak_areas or [],
            "suggestions": self.suggestions or [],
            "created_at": self.created_at.isoformat(),
        }


class SpeakingSession(db.Model):
    __tablename__ = "speaking_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    mode = db.Column(db.String(40))
    track = db.Column(db.String(40))        # communication | intro | project
    fluency = db.Column(db.Integer)
    clarity = db.Column(db.Integer)
    confidence = db.Column(db.Integer)
    structure = db.Column(db.Integer)
    summary = db.Column(db.Text)            # the AI feedback paragraph
    weak_areas = db.Column(db.JSON, default=list)
    suggestions = db.Column(db.JSON, default=list)
    transcript = db.Column(db.JSON)         # [{prompt, answer, difficulty}]
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "mode": self.mode,
            "track": self.track,
            "fluency": self.fluency,
            "clarity": self.clarity,
            "confidence": self.confidence,
            "structure": self.structure,
            "summary": self.summary,
            "weak_areas": self.weak_areas or [],
            "suggestions": self.suggestions or [],
            "created_at": self.created_at.isoformat(),
        }
