"""Pure-helper tests for communication_bp — no request context, no network."""
import time

from communication_bp import (
    _clamp, _next_level, _sanitize_answer,
    MIN_LEVEL, MAX_LEVEL, START_LEVEL, MAX_ANSWER_CHARS,
)


def test_clamp_passes_in_range_levels():
    for n in range(MIN_LEVEL, MAX_LEVEL + 1):
        assert _clamp(n) == n


def test_clamp_bounds_out_of_range_levels():
    assert _clamp(0) == MIN_LEVEL
    assert _clamp(-3) == MIN_LEVEL
    assert _clamp(99) == MAX_LEVEL


def test_clamp_coerces_numeric_strings():
    assert _clamp("4") == 4


def test_clamp_falls_back_to_start_level_on_junk():
    assert _clamp(None) == START_LEVEL
    assert _clamp("hard") == START_LEVEL


# --- _next_level: difficulty is derived from the model's quality call, in Python --


def test_next_level_moves_with_quality():
    assert _next_level(3, "strong") == 4
    assert _next_level(3, "ok") == 3
    assert _next_level(3, "weak") == 2


def test_next_level_tolerates_model_formatting():
    assert _next_level(3, "STRONG") == 4
    assert _next_level(3, " weak ") == 2


def test_next_level_holds_steady_on_junk():
    """An unusable quality must not move the student's difficulty either way."""
    for junk in (None, "", "excellent", 7):
        assert _next_level(3, junk) == 3


def test_next_level_stays_in_bounds():
    assert _next_level(MAX_LEVEL, "strong") == MAX_LEVEL
    assert _next_level(MIN_LEVEL, "weak") == MIN_LEVEL


# --- _sanitize_answer: recovering an answer from a broken dictation loop ---------
#
# The bug this guards against (seen in production, speaking_sessions.id 11): the mic
# re-delivered each phrase one word longer every time, so a ~40-word answer reached
# the server as 37,719 characters. That blew the evaluation prompt's context window,
# the model returned nothing, and the student silently got the 5/5/5/5 fallback
# scores as though they had been graded.

def _growing_prefix_loop(sentence):
    """The exact corruption shape: 'so', 'so basically', 'so basically the', …"""
    words = sentence.split()
    return " ".join(" ".join(words[:i]) for i in range(1, len(words) + 1))


def test_sanitize_leaves_an_ordinary_answer_alone():
    answer = "I built a web scraping tool in Python using BeautifulSoup and Requests."
    assert _sanitize_answer(answer) == answer


def test_sanitize_handles_blank_and_missing_answers():
    assert _sanitize_answer("") == ""
    assert _sanitize_answer(None) == ""
    assert _sanitize_answer("   ") == ""


def test_sanitize_drops_stutters():
    assert _sanitize_answer("So I I think the the answer is yes.") == "So I think the answer is yes."


def test_sanitize_recovers_the_sentence_from_a_growing_prefix_loop():
    sentence = ("so basically the interest for artificial intelligence came because I had "
                "a lot of interest in technology and I am eager to learn new things")
    assert _sanitize_answer(_growing_prefix_loop(sentence)) == sentence


def test_sanitize_recovers_a_loop_that_restarts_more_than_once():
    first = "I took a diploma in computer engineering to understand how computer systems work"
    second = "then I took a BTech in machine learning to specialise in artificial intelligence"
    recovered = _sanitize_answer(_growing_prefix_loop(first) + " " + _growing_prefix_loop(second))
    assert second in recovered
    assert len(recovered) < len(first) + len(second) + 50


def test_sanitize_caps_length_even_when_nothing_collapses():
    # Random-ish words with no repeating structure: the collapse passes cannot help,
    # so the hard cap is the only thing keeping this out of the prompt.
    answer = " ".join(f"word{i}" for i in range(20000))
    cleaned = _sanitize_answer(answer)
    assert len(cleaned) <= MAX_ANSWER_CHARS
    assert not cleaned.endswith("wor")   # cut on a word boundary, not mid-token


def test_sanitize_stays_fast_enough_for_a_request_path():
    # The production case was 37,719 characters; this is worse. It runs on every
    # answer submission, so a quadratic blow-up here would stall the session.
    sentence = " ".join(f"word{i}" for i in range(300))
    started = time.monotonic()
    _sanitize_answer(_growing_prefix_loop(sentence))
    assert time.monotonic() - started < 2.0
