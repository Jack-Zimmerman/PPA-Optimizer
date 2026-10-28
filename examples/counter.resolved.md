# Spec: 4-bit up counter with enable (`counter`)

Top module: `counter`. Single clock `clk` (rising edge), synchronous active-high reset `rst` (inferred).

## Ports
| Port | Dir | Width | Meaning |
|---|---|---|---|
| clk | in | 1 | clock |
| rst | in | 1 | synchronous reset, active high |
| en | in | 1 | count enable |
| count | out | 4 | registered count value (unsigned) |

## Behaviour
On each rising edge of `clk`:
- If `rst` is 1: `count` becomes 0. Reset takes priority over `en`.
- Otherwise, if `en` is 1: `count` becomes `count + 1`, wrapping modulo 16 (15 goes to 0).
- Otherwise (`en` is 0): `count` holds its value.

`count` is registered: it changes only on a clock edge, and an `en` value presented before an edge takes effect at that edge. `count` is unspecified before the first reset. There are no reserved or illegal encodings.

## Inferred decisions
- Top module name `counter` (inferred), from the file name.
- Clock is rising edge on a port named `clk` (inferred).
- Reset is synchronous, active high, named `rst` (inferred); the description mentions no reset.
- Reset value is 0 (inferred).
- Reset has priority over `en` (inferred).
- Output port is named `count`, 4 bits, unsigned (inferred).
- Output is registered directly from the state register (inferred).
- Wraps from 15 to 0 with no carry-out port (inferred).
- Output unspecified before first reset (inferred).
