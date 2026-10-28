"""Suite for fp16_multiply, written from examples/fp16_multiply.resolved.md only."""
from __future__ import annotations

TOP = "fp16_multiply"
PORTS = {
    "clk": ("input", 1), "rst": ("input", 1),
    "a": ("input", 16), "b": ("input", 16), "en": ("input", 1),
    "y": ("output", 16), "valid": ("output", 1),
}
CLOCK = "clk"
RESET = ("rst", False)
NAN = 0x7E00
INF = 0x7C00


def _cls(x):
    e, f = (x >> 10) & 31, x & 1023
    if e == 31:
        return "nan" if f else "inf"
    if e == 0:
        return "sub" if f else "zero"
    return "norm"


def fp16_mul(a, b):
    sg = ((a ^ b) & 0x8000)
    ca, cb = _cls(a), _cls(b)
    if ca == "nan" or cb == "nan":
        return NAN
    if ca == "inf" or cb == "inf":
        if ca == "zero" or cb == "zero":
            return NAN
        return sg | INF
    if ca == "zero" or cb == "zero":
        return sg

    def dec(x):
        e, f = (x >> 10) & 31, x & 1023
        return (f, -24) if e == 0 else (f | 1024, e - 25)

    ma, ea = dec(a)
    mb, eb = dec(b)
    P, E = ma * mb, ea + eb          # value = P * 2**E exactly, P > 0
    x = max(P.bit_length() - 1 + E, -14)   # exponent of result binade
    sh = (x - 10) - E                # unit = 2**(x-10); n = P / 2**sh
    if sh <= 0:
        n = P << (-sh)
    else:
        n = P >> sh
        rem = P & ((1 << sh) - 1)
        half = 1 << (sh - 1)
        if rem > half or (rem == half and (n & 1)):
            n += 1
    if n >= 2048:
        n >>= 1
        x += 1
    if x > 15:
        return sg | INF
    if n < 1024:                      # subnormal (x == -14) or zero
        return sg | n
    return sg | ((x + 15) << 10) | (n - 1024)


class Model:
    LAT = 3

    def __init__(self):
        self.known = False
        self.pipe = [(0, None)] * self.LAT   # (valid, y)

    def step(self, inp):
        if inp["rst"]:
            self.known = True
            self.pipe = [(0, None)] * self.LAT
        else:
            new = (1, fp16_mul(inp["a"], inp["b"])) if inp["en"] else (0, None)
            self.pipe = [new] + self.pipe[:-1]
        if not self.known:
            return {"y": None, "valid": None}
        v, y = self.pipe[-1]
        return {"y": y if v else None, "valid": v}


def _prod(i):
    return fp16_mul(i["a"], i["b"])


def _op(i):
    return i["en"] == 1 and i["rst"] == 0


def _cc(i, c1, c2=None):
    if not _op(i):
        return False
    ca, cb = _cls(i["a"]), _cls(i["b"])
    if c2 is None:
        return c1 in (ca, cb)
    return (ca, cb) in ((c1, c2), (c2, c1))


def _exp(y):
    return (y >> 10) & 31


def _mag(i):
    return _prod(i) & 0x7FFF


COVER = {
    "reset_asserted": lambda i, o: i["rst"] == 1,
    "reset_with_en": lambda i, o: i["rst"] == 1 and i["en"] == 1,
    "en_low_bubble": lambda i, o: i["rst"] == 0 and i["en"] == 0,
    "valid_high_out": lambda i, o: o["valid"] == 1,
    "valid_low_out": lambda i, o: o["valid"] == 0,
    "back_to_back_ops": lambda i, o: o["valid"] == 1 and _op(i),
    "bubble_between_ops": lambda i, o: o["valid"] == 1 and i["en"] == 0,
    "op_normal_x_normal": lambda i, o: _cc(i, "norm", "norm"),
    "op_zero_x_normal": lambda i, o: _cc(i, "zero", "norm"),
    "op_zero_x_subnormal": lambda i, o: _cc(i, "zero", "sub"),
    "op_zero_x_zero": lambda i, o: _cc(i, "zero", "zero"),
    "op_inf_x_zero_nan": lambda i, o: _cc(i, "inf", "zero"),
    "op_inf_x_normal": lambda i, o: _cc(i, "inf", "norm"),
    "op_inf_x_subnormal": lambda i, o: _cc(i, "inf", "sub"),
    "op_inf_x_inf": lambda i, o: _cc(i, "inf", "inf"),
    "op_nan_input": lambda i, o: _cc(i, "nan"),
    "op_nan_x_zero": lambda i, o: _cc(i, "nan", "zero"),
    "op_nan_x_inf": lambda i, o: _cc(i, "nan", "inf"),
    "op_nan_negative_sign": lambda i, o: _cc(i, "nan") and ((i["a"] | i["b"]) >> 15) & 1 == 1,
    "op_nan_payload_low": lambda i, o: _cc(i, "nan") and (
        _cls(i["a"]) == "nan" and (i["a"] & 0x3FF) < 0x200 or _cls(i["b"]) == "nan" and (i["b"] & 0x3FF) < 0x200),
    "op_nan_payload_quiet": lambda i, o: _cc(i, "nan") and (
        _cls(i["a"]) == "nan" and (i["a"] & 0x3FF) >= 0x200 or _cls(i["b"]) == "nan" and (i["b"] & 0x3FF) >= 0x200),
    "op_subnormal_input": lambda i, o: _cc(i, "sub"),
    "op_subnormal_x_subnormal": lambda i, o: _cc(i, "sub", "sub"),
    "op_subnormal_x_normal": lambda i, o: _cc(i, "sub", "norm"),
    "op_negative_result": lambda i, o: _cc(i, "norm", "norm") and _prod(i) >> 15 == 1,
    "op_positive_result": lambda i, o: _cc(i, "norm", "norm") and _prod(i) >> 15 == 0,
    "op_neg_zero_result": lambda i, o: _op(i) and _prod(i) == 0x8000,
    "op_pos_zero_result": lambda i, o: _op(i) and _prod(i) == 0x0000,
    "op_pos_inf_result": lambda i, o: _op(i) and _prod(i) == 0x7C00,
    "op_neg_inf_result": lambda i, o: _op(i) and _prod(i) == 0xFC00,
    "op_overflow_pos": lambda i, o: _cc(i, "norm", "norm") and _prod(i) == 0x7C00,
    "op_overflow_neg": lambda i, o: _cc(i, "norm", "norm") and _prod(i) == 0xFC00,
    "op_max_finite_result": lambda i, o: _cc(i, "norm", "norm") and _mag(i) == 0x7BFF,
    "op_underflow_to_zero": lambda i, o: _cc(i, "norm", "norm") and _mag(i) == 0,
    "op_underflow_sub_to_zero": lambda i, o: _cc(i, "sub", "norm") and _mag(i) == 0,
    "op_subnormal_result": lambda i, o: _op(i) and _cls(_mag(i)) == "sub" and _cls(i["a"]) != "sub" and _cls(i["b"]) != "sub",
    "op_subnormal_result_from_subs": lambda i, o: _cc(i, "sub", "norm") and _cls(_mag(i)) == "sub",
    "op_smallest_subnormal_result": lambda i, o: _op(i) and _mag(i) == 1 and _cc(i, "norm"),
    "op_min_normal_result": lambda i, o: _cc(i, "norm", "norm") and _mag(i) == 0x0400,
    "op_one_times_x": lambda i, o: _cc(i, "norm", "norm") and (i["a"] & 0x7FFF) == 0x3C00,
    "op_exact_product": lambda i, o: _cc(i, "norm", "norm") and (i["a"] & 0x3FF) == 0 and (i["b"] & 0x3FF) == 0 and 0 < _exp(_prod(i)) < 31,
    "op_max_x_max": lambda i, o: _op(i) and (i["a"] & 0x7FFF) == 0x7BFF and (i["b"] & 0x7FFF) == 0x7BFF,
    "op_a_equals_b": lambda i, o: _cc(i, "norm", "norm") and i["a"] == i["b"],
    "op_neg_x_neg": lambda i, o: _cc(i, "norm", "norm") and i["a"] >> 15 == 1 and i["b"] >> 15 == 1,
    "op_mixed_sign": lambda i, o: _cc(i, "norm", "norm") and (i["a"] >> 15) != (i["b"] >> 15),
    "out_canonical_nan": lambda i, o: o["valid"] == 1 and o["y"] == NAN,
    "out_normal_result": lambda i, o: o["valid"] == 1 and 0 < _exp(o["y"]) < 31,
}

_SPECIAL = [
    0x0000, 0x8000, 0x0001, 0x8001, 0x0002, 0x03FF, 0x83FF, 0x0200, 0x0400, 0x8400, 0x0401,
    0x3C00, 0xBC00, 0x3C01, 0x3BFF, 0x4000, 0xC000, 0x3800, 0x3400, 0x4200, 0x4500,
    0x7BFF, 0xFBFF, 0x7800, 0x5BFF, 0x5C00, 0x2400, 0x0C00, 0x1400,
    0x7C00, 0xFC00, 0x7E00, 0xFE00, 0x7C01, 0xFC01, 0x7DFF, 0x7FFF, 0xFFFF,
]


def _v(a, b, en=1, rst=0):
    return {"rst": rst, "a": a, "b": b, "en": en}


def DIRECTED():
    # power-up reset, with en high during reset (dropped)
    yield _v(0x3C00, 0x3C00, 1, 1)
    yield _v(0x3C00, 0x3C00, 1, 1)
    for _ in range(4):
        yield _v(0x4000, 0x4000, 0)
    # one isolated op, then idle to drain
    yield _v(0x4000, 0x4200)
    for _ in range(4):
        yield _v(0x1234, 0x5678, 0)
    # streaming all special pairs back-to-back
    for a in _SPECIAL:
        for b in _SPECIAL:
            yield _v(a, b)
    for _ in range(4):
        yield _v(0xFFFF, 0xFFFF, 0)
    # bubbles interleaved
    for k, (a, b) in enumerate([(0x3C00, 0x4000), (0x7BFF, 0x4000), (0x0001, 0x3800),
                                (0x3C01, 0x3C01), (0x7C00, 0x0000)]):
        yield _v(a, b)
        for _ in range(k % 3):
            yield _v(0x7C00, 0x4000, 0)
    for _ in range(4):
        yield _v(0, 0, 0)
    # reset mid-pipeline at each depth: ops in flight discarded
    for depth in range(1, 4):
        for k in range(depth + 1):
            yield _v(0x4000 + k, 0x4200)
        yield _v(0x4400, 0x4400, 1, 1)
        for _ in range(4):
            yield _v(0x4400, 0x4400, 0)
    # reset (en low) followed by ops, then reset again mid-stream
    yield _v(1, 1, 0, 1)
    for k in range(5):
        yield _v(0x3C00 + k, 0x4000)
    yield _v(0x3C00, 0x3C00, 0, 1)
    for _ in range(4):
        yield _v(0, 0, 0)
    # rounding boundary cases
    for a, b in [(0x3C01, 0x3C01), (0x3C01, 0x3BFF), (0x7BFF, 0x3C01), (0x7BFF, 0x3C00),
                 (0x5BFF, 0x5BFF), (0x0001, 0x3800), (0x0001, 0x3400), (0x0001, 0x3C00),
                 (0x0003, 0x3800), (0x0002, 0x3800), (0x0400, 0x3800), (0x0401, 0x3800),
                 (0x03FF, 0x3C01), (0x0200, 0x0200), (0x1400, 0x1400), (0x0400, 0x3BFF),
                 (0x3BFF, 0x3BFF), (0x7800, 0x4000), (0x77FF, 0x4000), (0x7BFF, 0x3BFF),
                 (0x0001, 0x0001), (0x0001, 0x7BFF), (0x0001, 0x5800), (0x0001, 0x5BFF),
                 (0x03FF, 0x3800), (0x03FF, 0x3BFF), (0x0003, 0x3A00), (0x0005, 0x3800)]:
        yield _v(a, b)
        yield _v(a | 0x8000, b)
    for _ in range(4):
        yield _v(0, 0, 0)


def RANDOM(rng):
    def pick():
        t = rng.random()
        if t < 0.25:
            return rng.choice(_SPECIAL)
        if t < 0.45:  # subnormal
            return (rng.getrandbits(1) << 15) | rng.getrandbits(10)
        if t < 0.55:
            return (rng.getrandbits(1) << 15) | (rng.choice([1, 2, 29, 30]) << 10) | rng.getrandbits(10)
        if t < 0.65:  # sparse mantissa => ties
            return (rng.getrandbits(1) << 15) | (rng.randrange(1, 31) << 10) | (
                rng.getrandbits(10) & rng.getrandbits(10) & rng.getrandbits(10))
        if t < 0.7:
            return rng.getrandbits(16)
        return (rng.getrandbits(1) << 15) | (rng.randrange(1, 31) << 10) | rng.getrandbits(10)

    en = 1 if rng.random() < 0.8 else 0
    rst = 1 if rng.random() < 0.02 else 0
    a = pick()
    if rng.random() < 0.3:
        # choose b so the product exponent lands near under/overflow or subnormal range
        ea = (a >> 10) & 31
        target = rng.choice([0, 1, 15, 30, 31, 20, 10])
        eb = max(1, min(30, target - ea + 15))
        b = (rng.getrandbits(1) << 15) | (eb << 10) | rng.getrandbits(10)
    else:
        b = pick()
    return {"rst": rst, "a": a, "b": b, "en": en}


N_RANDOM = 30000
