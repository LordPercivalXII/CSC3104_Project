from eval.plot import calls_per_window


def test_rolling_window_captures_peak_between_integer_seconds():
    xs, counts = calls_per_window([0.3, 60.2], 61)
    assert max(counts) == 2
    assert counts[xs.index(60.3)] == 1


def test_window_left_boundary_is_exclusive_and_duplicates_count():
    xs, counts = calls_per_window([0, 0, 60], 60)
    assert counts[xs.index(0)] == 2
    assert counts[xs.index(60)] == 1
