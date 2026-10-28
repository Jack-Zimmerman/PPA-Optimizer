# Spec: 8-bit registered ALU (`alu_p`)

Top module: `alu_p`. Single clock `clk` (rising edge), synchronous active-high reset `rst`.

## Ports
| Port | Dir | Width | Meaning |
|---|---|---|---|
| clk | in | 1 | clock |
| rst | in | 1 | synchronous reset, active high |
| a | in | 8 | operand A (unsigned) |
| b | in | 8 | operand B (unsigned) |
| op | in | 3 | operation select |
| y | out | 8 | registered result |
| lt | out | 1 | registered "a < b" flag |

## Behaviour
On each rising edge of `clk`:
- If `rst` is 1: `y` and `lt` become 0.
- Otherwise `y` takes the result of the operation selected by `op`, computed from the `a`, `b` and `op` values present before the edge, and `lt` takes 1 if `a < b` (unsigned comparison) else 0. `lt` is updated for every `op`, not only subtract.

| op | operation | result (8 bit, wraps modulo 256) |
|---|---|---|
| 0 | add | a + b |
| 1 | subtract | a - b |
| 2 | and | a & b |
| 3 | or | a \| b |
| 4 | xor | a ^ b |
| 5 | shift left | a << b[2:0] |
| 6 | shift right (logical) | a >> b[2:0] |
| 7 | reserved | 0 |

Outputs are registered: they change only on a clock edge, one cycle after the inputs are presented. Outputs are unspecified before the first reset.
