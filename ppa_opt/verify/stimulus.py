"""Stimulus for the bench: reset, the suite's own directed vectors, an automatic corner sweep
(exhaustive when the input space is small), then seeded random with occasional mid-run resets.

Pure Python (no cocotb import) so it can be unit tested. Each item is a dict of pin values
for every data input plus, when the design has one, the reset pin (actual pin level).
"""
from __future__ import annotations

import itertools
import random
from typing import Callable, Iterable, Iterator

EXHAUSTIVE_MAX_BITS = 16
CORNER_PRODUCT_MAX = 4096
RESET_PROB = 1 / 200


def corners(width: int) -> list[int]:
    top = (1 << width) - 1
    vals = {0, top, 1 & top, top >> 1, 1 << (width - 1)}
    vals.update(1 << i for i in range(min(width, 8)))
    return sorted(vals)


def vectors(widths: dict[str, int], n_random: int, seed: int, reset: tuple[str, bool] | None = None,
            directed: Iterable[dict] = (), gen: Callable[[random.Random], dict] | None = None,
            ) -> Iterator[dict[str, int]]:
    rng = random.Random(seed)
    names = list(widths)
    rname, low = reset if reset else (None, False)
    asserted, idle = (0, 1) if low else (1, 0)

    def fill(v: dict) -> dict:
        out = {n: 0 for n in names}
        out.update({k: val & ((1 << widths[k]) - 1) for k, val in v.items() if k in widths})
        if rname:
            out[rname] = v.get(rname, idle)
        return out

    def rand_vec() -> dict:
        if gen:
            return fill(gen(rng))
        v = {n: (rng.choice(corners(w)) if rng.random() < 0.25 else rng.getrandbits(w))
             for n, w in widths.items()}
        return fill(v)

    def in_reset(v: dict | None = None) -> dict:
        out = fill(v or {})
        if rname:
            out[rname] = asserted
        return out

    # 1. power-up reset
    if rname:
        for _ in range(3):
            yield in_reset()

    # 2. the suite's directed tests (these come from the spec)
    for v in directed:
        yield fill(v)

    # 3. automatic corner sweep, then exhaustive when cheap
    per_input = [corners(widths[n]) for n in names]
    size = 1
    for c in per_input:
        size *= len(c)
    if size <= CORNER_PRODUCT_MAX:
        for combo in itertools.product(*per_input):
            yield fill(dict(zip(names, combo)))
    total_bits = sum(widths.values())
    if total_bits <= EXHAUSTIVE_MAX_BITS:
        space = list(range(1 << total_bits))
        rng.shuffle(space)
        for code in space:
            v, shift = {}, 0
            for n in names:
                v[n] = (code >> shift) & ((1 << widths[n]) - 1)
                shift += widths[n]
            yield fill(v)

    # 4. constrained random, with occasional resets to catch reset-value bugs
    for _ in range(n_random):
        if rname and rng.random() < RESET_PROB:
            for _ in range(rng.randint(1, 3)):
                yield in_reset(rand_vec())
        yield rand_vec()
