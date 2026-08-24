"""Tests for daily Groq token accounting — no network.

Groq's free tier is a per-model tokens-per-day cap with no rollover, so the only
lever left is knowing where the day stands before a 429 arrives. groq_client
printed a line per call and nothing summed them.
"""
from datetime import date, timedelta

import pytest

from models import db
from usage_tracker import GroqUsage, day_summary, record

MODEL = "llama-3.1-8b-instant"


@pytest.fixture(autouse=True)
def _app_context(app):
    """Every test here needs the in-memory database from conftest."""
    yield


def test_a_single_call_is_recorded():
    record(MODEL, "generate_dimension_questions",
           prompt_tokens=1175, completion_tokens=1317, total_tokens=2492)
    row = GroqUsage.query.one()
    assert (row.calls, row.total_tokens) == (1, 2492)


def test_repeat_calls_accumulate_into_one_row_per_day_model_feature():
    for _ in range(3):
        record(MODEL, "generate_dimension_questions",
               prompt_tokens=100, completion_tokens=200, total_tokens=300)
    row = GroqUsage.query.one()
    assert row.calls == 3
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (300, 600, 900)


def test_different_features_are_tracked_separately():
    record(MODEL, "generate_dimension_questions", total_tokens=2000)
    record(MODEL, "parse_resume", total_tokens=3000)
    assert GroqUsage.query.count() == 2
    assert day_summary()["by_feature"] == {"parse_resume": 3000,
                                           "generate_dimension_questions": 2000}


def test_different_models_are_tracked_separately():
    """The quota is per model — a combined total would hide which ladder rung is
    actually close to its ceiling."""
    record(MODEL, "parse_resume", total_tokens=1000)
    record("openai/gpt-oss-120b", "parse_resume", total_tokens=4000)
    assert day_summary()["by_model"] == {"openai/gpt-oss-120b": 4000, MODEL: 1000}


def test_days_do_not_bleed_into_each_other():
    yesterday = date.today() - timedelta(days=1)
    record(MODEL, "parse_resume", total_tokens=5000, day=yesterday)
    record(MODEL, "parse_resume", total_tokens=700)

    assert day_summary()["total_tokens"] == 700
    assert day_summary(yesterday)["total_tokens"] == 5000


def test_truncated_calls_are_counted_as_their_own_signal():
    """A truncated reply cost its full completion budget and returned broken
    JSON — spend that bought nothing."""
    record(MODEL, "generate_dimension_questions", total_tokens=1500, truncated=True)
    record(MODEL, "generate_dimension_questions", total_tokens=1400)
    row = GroqUsage.query.one()
    assert (row.calls, row.truncated_calls) == (2, 1)
    assert day_summary()["truncated_calls"] == 1


def test_summary_orders_the_biggest_spender_first():
    record(MODEL, "parse_resume", total_tokens=500)
    record(MODEL, "generate_core_persona", total_tokens=9000)
    record(MODEL, "generate_quiz", total_tokens=3000)
    assert list(day_summary()["by_feature"]) == [
        "generate_core_persona", "generate_quiz", "parse_resume"]


def test_an_empty_day_summarises_to_zero_rather_than_failing():
    summary = day_summary()
    assert summary["total_tokens"] == 0
    assert summary["calls"] == 0
    assert summary["by_model"] == {} and summary["by_feature"] == {}


def test_missing_token_counts_do_not_corrupt_the_totals():
    """Groq has returned usage blocks with fields absent; a None must not turn a
    running total into a TypeError halfway through a day."""
    record(MODEL, "parse_resume", prompt_tokens=None, completion_tokens=None,
           total_tokens=None)
    record(MODEL, "parse_resume", total_tokens=100)
    row = GroqUsage.query.one()
    assert (row.calls, row.total_tokens) == (2, 100)


# --- the wiring, at the point it actually matters ---

def test_the_sink_hook_is_invoked_once_per_successful_call():
    import groq_client

    seen = []
    groq_client.set_usage_sink(lambda **kw: seen.append(kw))
    try:
        groq_client._record_usage(model=MODEL, label="x", total_tokens=42)
    finally:
        groq_client.set_usage_sink(None)
    assert seen == [{"model": MODEL, "label": "x", "total_tokens": 42}]


def test_a_failing_recorder_never_breaks_the_call_that_produced_it(capsys):
    """The Groq call already cost real quota. Losing its accounting beats failing
    a user's onboarding over a bookkeeping write."""
    import groq_client

    def explode(**kw):
        raise RuntimeError("database is down")

    groq_client.set_usage_sink(explode)
    try:
        groq_client._record_usage(model=MODEL, label="x", total_tokens=1)
    finally:
        groq_client.set_usage_sink(None)
    assert "recorder failed" in capsys.readouterr().out


def test_no_sink_registered_is_a_silent_no_op():
    """Scripts and tests import groq_client with no Flask app or database."""
    import groq_client

    groq_client.set_usage_sink(None)
    groq_client._record_usage(model=MODEL, label="x", total_tokens=1)
