"""Pure-logic tests for softskill_agent — no network, no Flask app.

Covers the validation/finalize/fallback contract the /comm endpoints rely on:
the framework a session persists must always have name/why/steps/focus/total,
so a bad model reply is rejected (caller falls back) rather than shipping a
half-formed framework the adaptive engine would KeyError on.
"""
import pytest

from softskill_agent import (
    Framework, _finalize, _validate, fallback_framework,
    is_track, track_label, all_tracks,
    TRACK_BLUEPRINTS, MIN_STEPS, MAX_STEPS, MIN_TOTAL, MAX_TOTAL,
    SoftSkillGenerationError,
)
from pydantic import ValidationError


def _valid_framework_data(steps=3):
    return {
        "name": "A · B · C",
        "why": "it keeps a spoken answer short and clear",
        "steps": [{"label": f"S{i}", "hint": f"hint {i}"} for i in range(steps)],
        "focus": "speaking to the point",
        "total": 4,
    }


# --- track registry ---

def test_is_track_and_label():
    assert is_track("intro") is True
    assert is_track("nope") is False
    assert track_label("voice") == "Voice module"
    assert track_label("nope") == "Practice session"  # safe default, never KeyErrors


def test_all_tracks_matches_blueprints():
    assert set(all_tracks()) == set(TRACK_BLUEPRINTS.keys())
    assert len(all_tracks()) == 6


# --- Framework validation ---

def test_valid_framework_accepted():
    m = Framework.model_validate(_valid_framework_data())
    assert m.name and m.why and len(m.steps) == 3


def test_too_few_steps_rejected():
    with pytest.raises(ValidationError):
        Framework.model_validate(_valid_framework_data(steps=MIN_STEPS - 1))


def test_steps_capped_at_max():
    m = Framework.model_validate(_valid_framework_data(steps=MAX_STEPS + 3))
    assert len(m.steps) == MAX_STEPS


def test_blank_name_rejected():
    bad = _valid_framework_data()
    bad["name"] = "   "
    with pytest.raises(ValidationError):
        Framework.model_validate(bad)


def test_step_without_label_rejected():
    bad = _valid_framework_data()
    bad["steps"][0] = {"hint": "no label here"}
    with pytest.raises(ValidationError):
        Framework.model_validate(bad)


# --- _validate (the caller-facing gate) ---

def test_validate_wraps_bad_data_in_generation_error():
    # A malformed reply must raise the typed error so the endpoint falls back to the
    # skeleton instead of persisting a framework the adaptive engine can't read.
    with pytest.raises(SoftSkillGenerationError):
        _validate({"name": "x"}, "intro")


def test_validate_returns_finalized_dict():
    fw = _validate(_valid_framework_data(), "intro")
    assert set(fw.keys()) >= {"name", "why", "steps", "focus", "total"}


# --- _finalize: total derivation & focus fallback ---

def test_total_clamped_to_bounds():
    m = Framework.model_validate({**_valid_framework_data(), "total": 999})
    assert _finalize(m, "intent")["total"] == MAX_TOTAL


def test_total_never_below_step_count():
    # 4 steps but the model asked for total=3 → total must not drop below the steps.
    m = Framework.model_validate({**_valid_framework_data(steps=4), "total": 3})
    assert _finalize(m, "intent")["total"] >= 4


def test_total_defaults_when_junk():
    data = _valid_framework_data(steps=3)
    data["total"] = "not a number"
    m = Framework.model_validate(data)
    total = _finalize(m, "intent")["total"]
    assert MIN_TOTAL <= total <= MAX_TOTAL


def test_focus_falls_back_to_intent_when_blank():
    data = _valid_framework_data()
    data["focus"] = ""
    m = Framework.model_validate(data)
    assert _finalize(m, "the track intent")["focus"] == "the track intent"


# --- fallback_framework (resilience path) ---

def test_fallback_has_full_shape_for_every_track():
    # Every track's skeleton must carry the exact keys the endpoints read, or a Groq
    # outage would trade a 500 in the agent for a KeyError downstream.
    for track in all_tracks():
        fb = fallback_framework(track)
        assert set(fb.keys()) >= {"name", "why", "steps", "focus", "total"}
        assert MIN_STEPS <= len(fb["steps"]) <= MAX_STEPS
        for step in fb["steps"]:
            assert step["label"] and "hint" in step


def test_fallback_unknown_track_still_usable():
    fb = fallback_framework("mystery")
    assert fb["steps"] and fb["total"]
