"""Suite for `counter` written from examples/counter.resolved.md only."""
TOP = "counter"
PORTS = {"clk": ("input", 1), "rst": ("input", 1), "en": ("input", 1), "count": ("output", 4)}
CLOCK = "clk"
RESET = ("rst", False)


class Model:
    def __init__(self):
        self.c = None  # unspecified until first reset

    def step(self, inp):
        if inp["rst"]:
            self.c = 0
        elif self.c is not None and inp["en"]:
            self.c = (self.c + 1) & 15
        return {"count": self.c}


def _run(i, o, rst, en, cnt):
    return i["rst"] == rst and i["en"] == en and cnt(o["count"])


COVER = {
    "reset_with_en": lambda i, o: i["rst"] == 1 and i["en"] == 1 and o["count"] == 0,
    "reset_without_en": lambda i, o: i["rst"] == 1 and i["en"] == 0 and o["count"] == 0,
    "count_inc": lambda i, o: i["rst"] == 0 and i["en"] == 1 and o["count"] is not None,
    "count_hold": lambda i, o: i["rst"] == 0 and i["en"] == 0 and o["count"] is not None,
    "inc_to_1": lambda i, o: i["rst"] == 0 and i["en"] == 1 and o["count"] == 1,
    "inc_to_8": lambda i, o: i["rst"] == 0 and i["en"] == 1 and o["count"] == 8,
    "inc_to_15": lambda i, o: i["rst"] == 0 and i["en"] == 1 and o["count"] == 15,
    "wrap_15_to_0": lambda i, o: i["rst"] == 0 and i["en"] == 1 and o["count"] == 0,
    "hold_at_0": lambda i, o: i["rst"] == 0 and i["en"] == 0 and o["count"] == 0,
    "hold_at_15": lambda i, o: i["rst"] == 0 and i["en"] == 0 and o["count"] == 15,
    "hold_at_mid": lambda i, o: i["rst"] == 0 and i["en"] == 0 and o["count"] not in (None, 0, 15),
}


def DIRECTED():
    def v(rst, en):
        return {"rst": rst, "en": en}
    yield v(1, 0)
    for _ in range(40):          # two full wraps
        yield v(0, 1)
    for _ in range(3):
        yield v(0, 0)            # hold mid-range
    yield v(1, 1)                # reset priority over en
    yield v(0, 0)                # hold at 0
    yield v(0, 0)
    for _ in range(15):          # reach 15
        yield v(0, 1)
    yield v(0, 0)                # hold at 15
    yield v(0, 1)                # wrap to 0
    for _ in range(15):
        yield v(0, 1)
    yield v(1, 1)                # reset from 15 with en
    for _ in range(5):
        yield v(0, 1)
    yield v(1, 0)                # reset from nonzero without en
    yield v(1, 1)                # consecutive resets
    yield v(0, 1)
    yield v(0, 0)
    yield v(0, 1)
    yield v(1, 0)


N_RANDOM = 5000


def RANDOM(rng):
    return {"rst": 1 if rng.random() < 0.04 else 0, "en": 1 if rng.random() < 0.7 else 0}
