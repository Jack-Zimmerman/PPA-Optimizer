# Spec: pipelined IEEE-754 binary16 multiplier (`fp16_multiply`)

Top module: `fp16_multiply`. Single clock `clk` (rising edge), synchronous active-high reset `rst`.

## Ports
| Port | Dir | Width | Meaning |
|---|---|---|---|
| clk | in | 1 | clock |
| rst | in | 1 | synchronous reset, active high |
| a | in | 16 | operand A, IEEE-754 binary16 bit pattern (inferred) |
| b | in | 16 | operand B, IEEE-754 binary16 bit pattern (inferred) |
| en | in | 1 | enable: 1 means `a` and `b` are a valid operation this cycle |
| y | out | 16 | registered product, binary16 bit pattern |
| valid | out | 1 | registered; 1 means `y` holds the result of an accepted operation |

## Behaviour
Format: bit 15 sign, bits 14:10 biased exponent (bias 15), bits 9:0 fraction.

The multiplier is fully pipelined with a fixed latency of 3 cycles (inferred) and a throughput of one operation per cycle: a new `en` operation may be presented on every cycle with no stalls.

On each rising edge of `clk`:
- If `rst` is 1: `valid` becomes 0 and every operation still in the pipeline is discarded (it never appears on `valid`). `y` is unspecified while `valid` is 0.
- Otherwise, if `en` is 1 when edge N samples the inputs, then `valid` is 1 and `y` holds the product of those `a` and `b` values immediately after edge N+2 (stable until edge N+3). That is 3 register stages: edges N, N+1, N+2.
- If `en` was 0 at the sampling edge, `valid` is 0 at the corresponding output cycle. `a` and `b` are ignored then.
- `valid` is high for exactly the cycles corresponding to `en`-high input cycles, delayed by the latency. `y` is unspecified when `valid` is 0.
- Reset takes priority over `en`: an `en` sampled in a cycle where `rst` is 1 is dropped.

### Arithmetic
`y` = `a` × `b` correctly rounded per IEEE-754 binary16 multiplication, round-to-nearest, ties-to-even (inferred), with these details (inferred, IEEE-conformant):
- Sign of result = sign(a) XOR sign(b), including for zero, infinity and overflow results.
- Subnormal inputs (exponent 0, nonzero fraction) are treated with their true value (no flush-to-zero); subnormal results are produced by gradual underflow.
- Zero × finite = signed zero. Underflow below half the smallest subnormal rounds to signed zero.
- Overflow (rounded result magnitude > 65504) gives signed infinity (0x7C00 / 0xFC00).
- Infinity × nonzero (finite or infinity) = signed infinity.
- Infinity × zero = NaN. Any NaN input gives NaN.
- Every NaN result is the canonical quiet NaN `0x7E00` (inferred; sign and payload of input NaNs are not propagated).
- No exception flags are output (inferred).

### Unspecified
- `y` when `valid` is 0.
- All outputs before the first reset.

## Inferred decisions
- Format is IEEE-754 binary16 (1-5-10), not bfloat16 or a custom format.
- Operands are named `a`, `b`; result `y`; flags `en`, `valid`; sync active-high `rst` added as a conventional reset (the description did not mention one).
- Latency is a fixed 3 cycles, fully pipelined, one result per cycle, no backpressure/stall. (The description only said "more than 1 cycle" is acceptable.)
- `valid` tracks `en` through the pipeline; `en=0` bubbles pass through as `valid=0`.
- Reset clears `valid` and discards in-flight operations; `y` is unspecified when `valid=0`.
- Round-to-nearest-even; full subnormal support; canonical NaN `0x7E00`; no exception flags.
- Outputs are registered.
