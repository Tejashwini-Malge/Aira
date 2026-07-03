"""Pure-helper tests for communication_bp — no request context, no network."""
from communication_bp import _clamp, MIN_LEVEL, MAX_LEVEL, START_LEVEL


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
