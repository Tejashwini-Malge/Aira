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

    # The last day this account was seen making an authenticated request.
    #
    # Without it there is NO way to ask "did anyone come back?". A return visit
    # otherwise only leaves a trace if the user finishes a quiz or a speaking
    # session, and almost nobody does — so someone who logged in three times and
    # practised nothing is indistinguishable from someone who never returned.
    #
    # Nullable on purpose: existing rows genuinely have no such date, and back-
    # filling created_at would invent return visits that never happened.
    last_seen_at = db.Column(db.DateTime, index=True)

    # Career-development details collected once at onboarding (study, goal, target
    # role, timeline, language, optional note). Stored as a flexible blob so the
    # field set can evolve without a migration. The resume itself feeds the Persona.
    onboarding = db.Column(db.JSON)
    onboarding_complete = db.Column(db.Boolean, default=False, nullable=False)

    # Free plan: the student picks the 2 speaking tracks they care about most;
    # everything else on the Soft Skills page stays locked until they change it.
    unlocked_tracks = db.Column(db.JSON)

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
            "unlocked_tracks": self.unlocked_tracks or [],
            "onboarding": self.onboarding or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
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

    # Total (quiz + speaking) session count at the moment this persona was last
    # generated. A deliberate re-generation is only allowed once 3 MORE sessions
    # have happened since then — see persona_eligibility.SESSIONS_REQUIRED_TO_REFRESH.
    session_count_at_generation = db.Column(db.Integer, default=0, nullable=False)

    # The dynamically generated (not hardcoded) dimension questions for the
    # CURRENT in-progress attempt — cached so reloading questions.html mid-quiz
    # doesn't reshuffle the set or burn another LLM call. Cleared/regenerated at
    # the start of each fresh attempt (first build, or an eligible refresh).
    dimension_questions = db.Column(db.JSON)

    # Resume artifacts produced by the resume agent at onboarding.
    # FILTERED resume text only — projects/skills sections with contact details
    # redacted (resume_agent.sanitize_resume_text). The raw upload is never
    # persisted: it holds the candidate's name, phone, email, college and
    # employers, none of which any code path reads. Rows written before this
    # change still hold raw text — scrub them with backend/scrub_resume_text.py.
    resume_text = db.Column(db.Text)
    # Which resume_agent.SANITIZER_VERSION produced resume_text. 0 means the row
    # predates filtering and still holds raw text. Lets a future filter
    # improvement be re-applied to only the rows below the new version.
    resume_sanitizer_version = db.Column(db.Integer, default=0, nullable=False)
    resume_data = db.Column(db.JSON)        # {skills (technical), soft_skills, projects,
                                            #  experience_level, likely_gaps,
                                            #  technical_questions[2], hr_questions[2]}
                                            #  — questions are tagged with the persona
                                            #  dimension they probe. Rows written before
                                            #  soft-skill extraction have no soft_skills
                                            #  key; readers must tolerate its absence.

    # Set when a resume arrives AFTER a persona was already built — i.e. the user
    # onboarded without one and uploaded it later from their profile. It makes the
    # locked persona eligible for one refresh immediately, without waiting out
    # SESSIONS_REQUIRED_TO_REFRESH.
    #
    # The lock exists to stop a user re-rolling their persona after one bad day,
    # which is noise. A resume is the opposite of noise: it is the single largest
    # piece of evidence the assessment can have, and the persona that exists right
    # now was built without it. Cleared once the refreshed persona is generated.
    resume_refresh_pending = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        # No abstract "title" — the persona speaks plainly through its summary.
        return {
            "summary": self.summary,
            "dimensions": self.dimensions or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
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
    # Issued once, server-side, the moment /quiz/generate actually starts this
    # attempt (AIRA-#### for a role interview, REQ-#### for a topic round) —
    # a real per-session reference, not a decorative client-side random string.
    paper_ref = db.Column(db.String(40))
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
            "paper_ref": self.paper_ref,
            "created_at": self.created_at.isoformat(),
        }


class ReportSnapshot(db.Model):
    """A saved copy of the report card at one point in time — issued the moment
    the user opens the PTM folder on report.html. Past snapshots let a
    returning user compare "Term 1" against "Term 2" instead of only ever
    seeing one live, ever-changing view.
    """
    __tablename__ = "report_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    payload = db.Column(db.JSON, nullable=False)   # same shape as GET /me/report
    overall_grade = db.Column(db.String(4))
    overall_pct = db.Column(db.Integer)
    result_text = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_summary(self):
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "overall_grade": self.overall_grade,
            "overall_pct": self.overall_pct,
            "result_text": self.result_text,
        }


class SpeakingSession(db.Model):
    __tablename__ = "speaking_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    mode = db.Column(db.String(40))
    track = db.Column(db.String(40))        # communication | intro | project | voice
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


class Feedback(db.Model):
    """TEMPORARY — a market-validation instrument, not a permanent feature.

    Captured right before the report card, at every path that leads there
    (reflection, full analysis, dashboard), so real signal gets collected in
    the moment instead of relying on users to volunteer it after they've
    already left. This table exists to answer "is this actually working for
    people" while we validate the product — expect it to be revisited,
    reshaped, or torn out once that question is answered, not maintained
    indefinitely as a review/UGC system. Export via ../export_feedback.py.
    """
    __tablename__ = "feedback"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    rating = db.Column(db.Integer, nullable=False)     # 1-5 stars, required
    recommend_score = db.Column(db.Integer)            # 0-10 NPS-style, optional
    liked = db.Column(db.Text)                         # "what's working"
    improve = db.Column(db.Text)                        # "what to fix/add"
    # Indirect, single-select MCQs (see feedback_bp.MCQ_QUESTIONS) — phrased
    # around a moment or feeling rather than "did you like it", so the answer
    # says something about which specific mechanic to invest in next, not just
    # sentiment. {question_key: chosen_option}.
    mcq_responses = db.Column(db.JSON)
    context = db.Column(db.String(40))                  # which flow it followed
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "rating": self.rating,
            "recommend_score": self.recommend_score,
            "liked": self.liked,
            "improve": self.improve,
            "mcq_responses": self.mcq_responses or {},
            "context": self.context,
            "created_at": self.created_at.isoformat(),
        }


def migrate_new_columns(engine):
    """db.create_all() only creates TABLES that don't exist yet — it never
    ALTERs a table that's already there. Any database created before the
    Persona.session_count_at_generation/dimension_questions columns existed
    (including production's persistent Postgres DB) would 500 on the first
    request that touches a Persona row. This is a one-time, idempotent check
    run at startup right after db.create_all() so an existing DB gets the
    missing columns instead of erroring. Works for both SQLite (local) and
    Postgres (production) since the ALTER SQL used is plain standard SQL.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    if "personas" in table_names:
        existing = {c["name"] for c in inspector.get_columns("personas")}
        with engine.begin() as conn:
            if "session_count_at_generation" not in existing:
                conn.execute(text(
                    "ALTER TABLE personas ADD COLUMN session_count_at_generation "
                    "INTEGER NOT NULL DEFAULT 0"
                ))
            if "dimension_questions" not in existing:
                conn.execute(text("ALTER TABLE personas ADD COLUMN dimension_questions JSON"))
            if "resume_sanitizer_version" not in existing:
                # Defaults to 0 — every pre-existing row holds RAW resume text
                # until scrub_resume_text.py rewrites it.
                conn.execute(text(
                    "ALTER TABLE personas ADD COLUMN resume_sanitizer_version "
                    "INTEGER NOT NULL DEFAULT 0"
                ))
            if "resume_refresh_pending" not in existing:
                # FALSE for every existing row: they all onboarded when the resume
                # was compulsory, so none of them is waiting on a late upload.
                conn.execute(text(
                    "ALTER TABLE personas ADD COLUMN resume_refresh_pending "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                ))

    if "users" in table_names:
        existing = {c["name"] for c in inspector.get_columns("users")}
        with engine.begin() as conn:
            if "unlocked_tracks" not in existing:
                conn.execute(text("ALTER TABLE users ADD COLUMN unlocked_tracks JSON"))
            if "last_seen_at" not in existing:
                # Left NULL for every existing row rather than defaulted to
                # created_at: a signup in March that never came back must not be
                # recorded as having been seen in March-and-therefore-active.
                conn.execute(text("ALTER TABLE users ADD COLUMN last_seen_at TIMESTAMP"))

    if "feedback" in table_names:
        existing = {c["name"] for c in inspector.get_columns("feedback")}
        with engine.begin() as conn:
            if "mcq_responses" not in existing:
                conn.execute(text("ALTER TABLE feedback ADD COLUMN mcq_responses JSON"))

    if "quiz_results" in table_names:
        existing = {c["name"] for c in inspector.get_columns("quiz_results")}
        with engine.begin() as conn:
            if "paper_ref" not in existing:
                conn.execute(text("ALTER TABLE quiz_results ADD COLUMN paper_ref VARCHAR(40)"))
