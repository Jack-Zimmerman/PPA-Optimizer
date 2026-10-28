"""Latency-tolerant output checking.

A pipelined rewrite produces the model's output stream delayed by k extra cycles. For every
k in 0..K this tracker compares the DUT output at cycle t with the model output at cycle t-k,
and reports which k never mismatched. One simulation covers all k.

Reset: extra stages may hold pre-reset data (datapath registers usually have no reset), so
after a cycle where reset was asserted, outputs are not checked for the next k cycles
(the reset cycle itself plus k more cycles while the pipeline flushes).
"""
from __future__ import annotations


class LatencyTracker:
    def __init__(self, max_extra: int, widths: dict[str, int]):
        self.K = max_extra
        self.widths = widths
        self.expected: list[dict] = []           # model output per cycle
        self.last_reset: int | None = None       # most recent cycle with reset asserted
        self.first_bad: dict[int, dict | None] = {k: None for k in range(max_extra + 1)}

    def update(self, cycle: int, reset_asserted: bool, pins: dict, expected: dict, got: dict) -> None:
        """Call once per cycle, after `cycle`'s edge. `got` is the DUT's outputs (ints)."""
        self.expected.append(expected)
        if reset_asserted:
            self.last_reset = cycle
        for k in range(self.K + 1):
            if self.first_bad[k] is not None or cycle - k < 0:
                continue
            if self.last_reset is not None and cycle - self.last_reset <= k:
                continue                           # pipeline still flushing after a reset
            want = self.expected[cycle - k]
            for name, w in self.widths.items():
                exp = want.get(name)
                if exp is None:
                    continue
                if got[name] != exp & ((1 << w) - 1):
                    self.first_bad[k] = {"cycle": cycle, "signal": name, "expected": exp, "got": got[name],
                                         "inputs": dict(pins), "latency_tried": k}
                    break

    @property
    def all_failed(self) -> bool:
        return all(v is not None for v in self.first_bad.values())

    @property
    def latency(self) -> int | None:
        """Smallest extra latency with no mismatch, or None."""
        return next((k for k in range(self.K + 1) if self.first_bad[k] is None), None)

    def best_mismatch(self) -> dict | None:
        """For a failing design: the mismatch of the latency that survived longest (the likeliest intent)."""
        bad = [v for v in self.first_bad.values() if v is not None]
        return max(bad, key=lambda m: m["cycle"]) if bad else None

    def summary(self) -> str:
        """First mismatch for each tried latency, e.g. "+0: 'y' at cycle 5; +1: 'lt' at cycle 9"."""
        return "; ".join(f"+{k}: '{m['signal']}' at cycle {m['cycle']}" if m else f"+{k}: ok"
                         for k, m in self.first_bad.items())
