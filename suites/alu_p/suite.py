"""Suite for alu_p, written from examples/alu_spec/spec.md only."""
TOP = "alu_p"
PORTS = {
    "clk": ("input", 1), "rst": ("input", 1),
    "a": ("input", 8), "b": ("input", 8), "op": ("input", 3),
    "y": ("output", 8), "lt": ("output", 1),
}
CLOCK = "clk"
RESET = ("rst", False)


def _alu(a, b, op):
    s = b & 7
    return [
        (a + b) & 255, (a - b) & 255, a & b, a | b, a ^ b,
        (a << s) & 255, a >> s, 0,
    ][op]


class Model:
    def __init__(self):
        self.y = None   # unspecified before first reset
        self.lt = None
        self.seen_reset = False

    def step(self, inp):
        if inp["rst"]:
            self.y, self.lt = 0, 0
            self.seen_reset = True
        else:
            self.y = _alu(inp["a"], inp["b"], inp["op"])
            self.lt = 1 if inp["a"] < inp["b"] else 0
        if not self.seen_reset:
            return {"y": None, "lt": None}
        return {"y": self.y, "lt": self.lt}


def _n(i):
    return not i["rst"]


COVER = {
    "reset_active": lambda i, o: i["rst"] == 1,
    "reset_with_nonzero_inputs": lambda i, o: i["rst"] == 1 and (i["a"] | i["b"]) != 0,
    "reset_deasserted": lambda i, o: i["rst"] == 0,
    "op_add": lambda i, o: _n(i) and i["op"] == 0,
    "op_add_wraps": lambda i, o: _n(i) and i["op"] == 0 and i["a"] + i["b"] > 255,
    "op_add_max": lambda i, o: _n(i) and i["op"] == 0 and o["y"] == 255,
    "op_add_zero_result": lambda i, o: _n(i) and i["op"] == 0 and o["y"] == 0,
    "op_sub": lambda i, o: _n(i) and i["op"] == 1,
    "op_sub_borrow_wraps": lambda i, o: _n(i) and i["op"] == 1 and i["a"] < i["b"],
    "op_sub_zero": lambda i, o: _n(i) and i["op"] == 1 and i["a"] == i["b"],
    "op_and": lambda i, o: _n(i) and i["op"] == 2,
    "op_and_zero": lambda i, o: _n(i) and i["op"] == 2 and o["y"] == 0 and i["a"] != 0 and i["b"] != 0,
    "op_or": lambda i, o: _n(i) and i["op"] == 3,
    "op_or_all_ones": lambda i, o: _n(i) and i["op"] == 3 and o["y"] == 255,
    "op_xor": lambda i, o: _n(i) and i["op"] == 4,
    "op_xor_equal_operands": lambda i, o: _n(i) and i["op"] == 4 and i["a"] == i["b"],
    "op_shl": lambda i, o: _n(i) and i["op"] == 5,
    "op_shl_amt0": lambda i, o: _n(i) and i["op"] == 5 and (i["b"] & 7) == 0,
    "op_shl_amt7": lambda i, o: _n(i) and i["op"] == 5 and (i["b"] & 7) == 7,
    "op_shl_discards_bits": lambda i, o: _n(i) and i["op"] == 5 and (i["a"] << (i["b"] & 7)) > 255,
    "op_shl_ignores_b_high_bits": lambda i, o: _n(i) and i["op"] == 5 and (i["b"] >> 3) != 0,
    "op_shr": lambda i, o: _n(i) and i["op"] == 6,
    "op_shr_amt0": lambda i, o: _n(i) and i["op"] == 6 and (i["b"] & 7) == 0,
    "op_shr_amt7": lambda i, o: _n(i) and i["op"] == 6 and (i["b"] & 7) == 7,
    "op_shr_logical_msb_set": lambda i, o: _n(i) and i["op"] == 6 and i["a"] >= 128 and (i["b"] & 7) != 0,
    "op_shr_ignores_b_high_bits": lambda i, o: _n(i) and i["op"] == 6 and (i["b"] >> 3) != 0,
    "op_reserved_zero": lambda i, o: _n(i) and i["op"] == 7 and o["y"] == 0,
    "op_reserved_nonzero_inputs": lambda i, o: _n(i) and i["op"] == 7 and i["a"] != 0 and i["b"] != 0,
    "lt_true": lambda i, o: _n(i) and o["lt"] == 1,
    "lt_false_equal": lambda i, o: _n(i) and i["a"] == i["b"] and o["lt"] == 0,
    "lt_false_greater": lambda i, o: _n(i) and i["a"] > i["b"] and o["lt"] == 0,
    "lt_unsigned_a_small_b_msb": lambda i, o: _n(i) and i["a"] < 128 <= i["b"] and o["lt"] == 1,
    "lt_unsigned_a_msb_b_small": lambda i, o: _n(i) and i["b"] < 128 <= i["a"] and o["lt"] == 0,
    "lt_a_zero_b_max": lambda i, o: _n(i) and i["a"] == 0 and i["b"] == 255 and o["lt"] == 1,
    "lt_updated_non_sub_op": lambda i, o: _n(i) and i["op"] != 1 and o["lt"] == 1,
    "lt_set_on_every_op": lambda i, o: _n(i) and i["a"] < i["b"] and i["op"] in range(8),
}
for _k in range(8):
    COVER[f"lt_true_op{_k}"] = (lambda k: lambda i, o: _n(i) and i["op"] == k and o["lt"] == 1)(_k)
    COVER[f"lt_false_op{_k}"] = (lambda k: lambda i, o: _n(i) and i["op"] == k and o["lt"] == 0)(_k)
for _s in range(8):
    COVER[f"shl_amt{_s}"] = (lambda s: lambda i, o: _n(i) and i["op"] == 5 and (i["b"] & 7) == s)(_s)
    COVER[f"shr_amt{_s}"] = (lambda s: lambda i, o: _n(i) and i["op"] == 6 and (i["b"] & 7) == s)(_s)


def _v(a, b, op, rst=0):
    return {"rst": rst, "a": a, "b": b, "op": op}


def DIRECTED():
    yield _v(0x55, 0xAA, 0, 1)            # first reset
    yield _v(0x55, 0xAA, 0, 1)
    vals = [0, 1, 2, 0x7F, 0x80, 0x81, 0xAA, 0x55, 0xFE, 0xFF]
    for op in range(8):
        for a in vals:
            for b in vals:
                yield _v(a, b, op)
    for s in range(8):                    # every shift amount, plus high b bits set
        for hi in (0, 0xF8):
            for a in (1, 0x80, 0xFF, 0xA5):
                yield _v(a, hi | s, 5)
                yield _v(a, hi | s, 6)
    for op in range(8):                   # reset mid-stream with nonzero results pending
        yield _v(0xFF, 0x01, op)
        yield _v(0xFF, 0x01, op, 1)
        yield _v(0xFF, 0x01, op, 1)
        yield _v(0x01, 0xFF, op)
    yield _v(3, 4, 0, 1)
    yield _v(3, 4, 7)
    yield _v(0, 0, 0)


def RANDOM(rng):
    r = rng.random()
    def val():
        c = rng.random()
        if c < 0.15: return rng.choice([0, 1, 0x7F, 0x80, 0xFF])
        return rng.randrange(256)
    a, b = val(), val()
    if r < 0.1:
        b = a
    return {"rst": 1 if rng.random() < 0.05 else 0, "a": a, "b": b, "op": rng.randrange(8)}


N_RANDOM = 20000
