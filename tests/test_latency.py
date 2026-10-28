from __future__ import annotations

from ppa_opt.verify.latency import LatencyTracker


def run(K, dut_delay, inputs, resets=(), dut_value=lambda x: x + 1):
    """Model: out[t] = f(in[t]) (reset -> 0). DUT: same stream delayed by dut_delay cycles."""
    tr = LatencyTracker(K, {"y": 8})
    model = []
    for t, x in enumerate(inputs):
        rst = t in resets
        model.append(0 if rst else dut_value(x))
        src = t - dut_delay
        got = model[src] if src >= 0 else 0
        if rst:
            got = 0
        tr.update(t, rst, {"x": x}, {"y": model[t]}, {"y": got})
    return tr


def test_finds_the_smallest_matching_latency():
    tr = run(3, 2, list(range(40)), resets={0})
    assert tr.latency == 2 and tr.first_bad[0] and tr.first_bad[1] and tr.first_bad[3]   # k=3 also off by one


def test_zero_delay_is_latency_zero():
    assert run(2, 0, list(range(30)), resets={0}).latency == 0


def test_no_latency_within_limit_fails_and_reports_closest():
    tr = run(1, 3, list(range(30)), resets={0})
    assert tr.latency is None and tr.all_failed and tr.best_mismatch() is not None


def test_outputs_are_not_checked_while_pipeline_flushes_after_reset():
    # the DUT keeps pre-reset data in flight for 2 cycles after a mid-run reset; that must not count
    tr = LatencyTracker(2, {"y": 8})
    stream = [10, 11, 12, 13, 14, 15, 16, 17]
    model = []
    for t, x in enumerate(stream):
        rst = t == 4
        model.append(0 if rst else x)
        got = stream[t - 2] if t >= 2 else 0       # DUT: pure 2-cycle delay, ignores reset for data in flight
        tr.update(t, rst, {"x": x}, {"y": model[t]}, {"y": got})
    assert tr.latency == 2


def test_unspecified_model_outputs_are_skipped():
    tr = LatencyTracker(1, {"y": 8})
    tr.update(0, False, {}, {"y": None}, {"y": 77})
    tr.update(1, False, {}, {"y": None}, {"y": 5})
    assert tr.latency == 0
