"""Daily Groq token accounting, so free-tier headroom is a number you can look at
rather than something you discover by getting a 429 mid-request.

Groq's free tier is a tokens-per-day cap tracked PER MODEL, and unused quota does
not carry into the next day — there is no rollover to opt into. What can be done
is knowing, before the ceiling arrives, how much of each model's day has gone and
which feature spent it. groq_client already printed a line per call; nothing added
those lines up, and print output does not survive a restart or a log rotation.

One row per (day, model, feature). Rows are tiny and bounded — days x 3 models x a
handful of labels — so this never needs pruning at Aira's scale.

Deliberately a separate module from groq_client: that one is the HTTP transport
and is imported by scripts and tests with no Flask app or database. It exposes a
sink hook; app.py wires `record` into it at startup.
"""
from datetime import date, datetime

from models import db


class GroqUsage(db.Model):
    """Accumulated token spend for one model/feature on one UTC day.

    UTC because Groq's own quota window is UTC — bucketing by local time would
    put a single quota day across two rows and make "how much is left today"
    unanswerable at exactly the hour it matters.
    """
    __tablename__ = "groq_usage"

    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False, index=True)
    model = db.Column(db.String(80), nullable=False)
    label = db.Column(db.String(60), nullable=False)      # the calling feature

    calls = db.Column(db.Integer, default=0, nullable=False)
    prompt_tokens = db.Column(db.Integer, default=0, nullable=False)
    completion_tokens = db.Column(db.Integer, default=0, nullable=False)
    total_tokens = db.Column(db.Integer, default=0, nullable=False)
    # A truncated reply still cost its full completion budget but produced broken
    # JSON, so this is spend that bought nothing — the clearest waste signal there is.
    truncated_calls = db.Column(db.Integer, default=0, nullable=False)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (db.UniqueConstraint("day", "model", "label"),)


def record(model, label, prompt_tokens=0, completion_tokens=0, total_tokens=0,
           truncated=False, day=None):
    """Add one call to today's totals. Never raises — see groq_client._record_usage."""
    day = day or date.today()

    row = GroqUsage.query.filter_by(day=day, model=model, label=label).first()
    if row is None:
        row = GroqUsage(day=day, model=model, label=label)
        db.session.add(row)

    row.calls = (row.calls or 0) + 1
    row.prompt_tokens = (row.prompt_tokens or 0) + (prompt_tokens or 0)
    row.completion_tokens = (row.completion_tokens or 0) + (completion_tokens or 0)
    row.total_tokens = (row.total_tokens or 0) + (total_tokens or 0)
    if truncated:
        row.truncated_calls = (row.truncated_calls or 0) + 1
    row.updated_at = datetime.utcnow()

    # Committed immediately rather than riding on the caller's transaction: an
    # LLM call that already cost real quota must stay counted even if the request
    # that made it later fails and rolls back. Otherwise the spend that hurts most
    # — failed requests — is exactly the spend that goes unrecorded.
    db.session.commit()
    return row


def day_summary(day=None):
    """Per-model and per-feature totals for one day, biggest spender first."""
    day = day or date.today()
    rows = GroqUsage.query.filter_by(day=day).all()

    by_model, by_label = {}, {}
    for r in rows:
        by_model[r.model] = by_model.get(r.model, 0) + (r.total_tokens or 0)
        by_label[r.label] = by_label.get(r.label, 0) + (r.total_tokens or 0)

    return {
        "day": day.isoformat(),
        "total_tokens": sum(by_model.values()),
        "calls": sum(r.calls or 0 for r in rows),
        "truncated_calls": sum(r.truncated_calls or 0 for r in rows),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
        "by_feature": dict(sorted(by_label.items(), key=lambda kv: -kv[1])),
    }
