"""Tests for the Groq fallback ladder — no network.

Groq's free tier is a per-model tokens-per-day cap, so the ladder is the only
thing standing between one busy feature and a dead app. The property that
matters is not the exact order but the tiering: a cheap call must exhaust the
cheap tier before it spends quota the coaching calls need, and no call may ever
lose access to a model just because it started somewhere unusual.
"""
from groq_client import CHEAP_MODELS, FAST_MODEL, MODEL_NAME, QUALITY_MODELS, _ladder

ALL_MODELS = QUALITY_MODELS + CHEAP_MODELS


def test_default_ladder_leads_with_the_primary():
    assert _ladder()[0] == MODEL_NAME
    assert _ladder() == QUALITY_MODELS + CHEAP_MODELS


def test_cheap_calls_exhaust_the_cheap_tier_before_touching_quality_quota():
    """The whole point of the split: a burst of resume parses must not eat the
    daily allowance that persona generation depends on."""
    ladder = _ladder(FAST_MODEL)
    assert ladder[: len(CHEAP_MODELS)] == CHEAP_MODELS
    assert ladder[len(CHEAP_MODELS) :] == QUALITY_MODELS


def test_a_quality_call_still_degrades_into_the_cheap_tier():
    """Falling back to a smaller model beats failing the user's request."""
    ladder = _ladder(MODEL_NAME)
    assert ladder[0] == MODEL_NAME
    assert set(ladder[-len(CHEAP_MODELS) :]) == set(CHEAP_MODELS)


def test_every_entry_point_reaches_every_model_exactly_once():
    """Preferring a model must never drop another one from the chain — that
    would make a cheap route MORE likely to fail outright, not less."""
    for preferred in [None, MODEL_NAME, FAST_MODEL] + ALL_MODELS:
        ladder = _ladder(preferred)
        assert ladder == list(dict.fromkeys(ladder)), preferred
        assert set(ladder) == set(ALL_MODELS), preferred


def test_the_preferred_model_is_always_tried_first():
    for preferred in ALL_MODELS:
        assert _ladder(preferred)[0] == preferred


def test_fast_model_is_in_the_cheap_tier():
    """FAST_MODEL naming a quality model would silently invert the routing."""
    assert FAST_MODEL in CHEAP_MODELS


def test_the_tiers_do_not_overlap():
    assert not set(QUALITY_MODELS) & set(CHEAP_MODELS)
