# Test suite format (written from the spec alone)

A suite is a directory `suites/<name>/` with `suite.py` (required) and `assertions.sv` (optional).
Read `ppa_opt/verify/suite.py`'s docstring for the exact contract. Summary:

```python
TOP   = "alu_p"
PORTS = {"clk": ("input", 1), "a": ("input", 8), "y": ("output", 8), ...}   # every port in the spec
CLOCK = "clk"                 # or None for a combinational design
RESET = ("rst", False)        # (name, active_low), or None

class Model:                  # independent cycle-accurate model of the SPEC, plain Python
    def __init__(self): ...   # unknown power-up state
    def step(self, inp):      # inp: pin values applied before the edge (reset pin included, as its pin level)
        ...                   # return {output_name: int | None}; None = unspecified (e.g. before first reset)

COVER = {                     # functional cover points, one per behaviour/corner the spec names
    "op_add_wraps": lambda i, o: i["op"] == 0 and i["a"] + i["b"] > 255,
}
def DIRECTED(): yield {...}   # optional spec-driven directed vectors (dicts of pin values)
def RANDOM(rng): return {...} # optional biased random generator, rng is random.Random
N_RANDOM = 20000              # optional
```

`assertions.sv` (optional) defines `module <TOP>_sva(...)` with exactly the DUT's ports, all declared
`input logic`, containing SVA properties for rules in the spec that hold at every cycle (e.g.
"after a reset edge, y == 0"). Use only constructs Verilator supports (`assert property (@(posedge clk) ...)`,
`$past`, `|->`, `|=>`).

## Rules for the author
- Work from the spec only. Never read, ask for, or infer anything from any RTL file.
- The model must follow the spec literally, including wrap-around, reserved encodings and reset behaviour.
  Where the spec is silent, return None (don't check) and do not invent behaviour.
- Cover every row of every table, every reset case, and each boundary value the spec implies
  (zero, max, wrap, shift amounts 0 and 7, equal operands, etc.). Cover predicates take `(inp, expected_out)`.
- Every output must be checked by the model. Prefer many small cover points over a few broad ones.
