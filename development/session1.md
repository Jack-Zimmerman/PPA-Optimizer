# Session 1 — SystemVerilog → area / timing / power flow

Goal: one function that takes SystemVerilog and returns area, timing and power numbers read from the tools.

## What was built

[ppa_opt/synth/openroad_flow.py](../ppa_opt/synth/openroad_flow.py)

```
SV files ──lint (slang, ~1 s)──> ORFS in Docker (Yosys → OpenROAD → route → report) ──> 6_report.json ──> metrics + provenance
              │                                                                              │
         fail fast with                                                                cache by RTL hash
         file:line errors
```

- `run_flow(rtl_files, FlowConfig(top, clock_ns, pdk, ...))` returns a `FlowResult`.
- `lint(rtl_files, top)` elaborates with slang in the container and raises `FlowError("lint", ...)` with the compiler's `file:line:col` message.
- CLI: `.venv/bin/python -m ppa_opt.synth.openroad_flow <files> --top <top> --clock-ns 5`.

### Design choices

- **SystemVerilog:** uses the slang frontend (`SYNTH_HDL_FRONTEND=slang`), which handles `logic`, `always_ff/always_comb`, enums, parameters and packages. Pass `--frontend ""` for Yosys `read_verilog -sv`.
- **PDK:** `sky130hd` by default. It is one config field, so nangate45 and asap7 work by changing `pdk`.
- **Constraints:** the SDC is generated per run. A clock port is auto-detected (`clk`, `clock`, ...). With no clock port it uses a virtual clock. Input and output delays are 20% of the period (`io_delay_frac`).
- **Provenance:** every metric carries `"<report file>:<key>"`, so each number traces to an OpenROAD report key. The LLM never supplies a number.
- **Derived values:** `min_period_ns` = clock − setup slack, `fmax_mhz`, and `timing_met`. They are computed only from the tool metrics.
- **Cache:** keyed on the hash of the RTL contents, the config and the image. A repeat run returns immediately with `cache_hit: true`.
- **Errors:** `FlowError(stage, message, log_tail)` names the failed stage and carries the log text, ready to feed back to the proposer.

### Metrics returned

| Metric | Report key |
|---|---|
| `area_um2` (std cells only) | `finish__design__instance__area__stdcell` |
| `cell_count`, `sequential_area_um2`, `sequential_count` | `finish__design__instance__*` |
| `setup_wns_ns`, `setup_tns_ns`, `hold_wns_ns` | `finish__timing__setup__ws` etc. |
| `power_total_w` (+ internal, switching, leakage) | `finish__power__*` |
| `die_area_um2`, `utilization` | `finish__design__die__area` etc. |

## Results

| Design | Area (µm²) | Cells | Setup slack (ns) | Power (mW) | Min period (ns) | Clock |
|---|---|---|---|---|---|---|
| `examples/alu_sv/alu.sv` (SV ALU, 8-bit) | 2269.68 | 338 | +1.648 | 0.361 | 3.35 | 5 ns, real |
| 16-bit combinational adder (scratch test) | 1244.94 | n/a | +2.232 | 0.106 | 5.77 | 8 ns, virtual |

- A cold run takes about 230 s, because x86 runs under emulation on Apple Silicon. A cache hit is instant.
- The earlier Verilog ALU gave 2210.9 µm², and the SV version gives 2269.7 µm². They are different sources, so they aren't a controlled comparison.

## Verified

- Real SystemVerilog flows through to metrics.
- Cache hit returns the identical result immediately.
- Syntax error: the lint fails in under a second with `bad.sv:2:17: error: expected expression`.
- A design with no clock runs with the virtual clock.

## Caveats and open items

- **Power** uses OpenROAD's default switching activity, so it is an estimate. Label it that way in reports.
- For a **combinational** design the min period includes the input and output delays (20% each), so compare it only against other runs of the same design.
- The slang diagnostics are not written to the ORFS stage log, so `lint` is the place to get readable errors. A failure after lint (in synthesis, placement or routing) shows the stage log tail.
- Warnings from slang aren't surfaced when the lint passes.
- Only one run at a time has been tested. Parallel candidates will need a CPU and memory budget for the VM (currently 4 CPUs, 8 GB).
- No automated tests yet. They should cover the cache key, lint failure and metric parsing.

## Next

1. Verification gate: cocotb + Verilator, run before any candidate is measured.
2. Parallel candidate evaluation and a speed option, such as a post-synthesis-only mode for quick screening.
3. Liberty-based per-line area annotator.
4. LLM proposers, then the loop, CLI and report.

---

## Addendum — parallel candidate evaluation, Makefile, crash handling

### Added
- **Makefile**: `make <name>.analysis` (finds `<name>.sv`/`.v` in `.`, `examples/`, `examples/*/`; top defaults to `<name>`), `.lint`, `compare FILES="a.sv b.sv ..."`, `clean-cache`, `help`. Options: `TOP`, `CLOCK`, `PDK`, `JSON=1`, `EXTRA`.
- **VM resized** to 8 CPUs / 16 GB (`colima start --cpu 8 --memory 16 --vm-type vz --vz-rosetta`). Each run is pinned to one core (`FlowConfig.num_cores=1`, `docker --cpus 1`, `NUM_CORES=1`) because detailed routing is mostly single-threaded; N candidates use about N cores.
- **[ppa_opt/synth/parallel.py](../ppa_opt/synth/parallel.py)**: `run_many()` runs candidates side by side, dedupes identical RTL, isolates failures, and prints a comparison table.
- **Crash handling**: failures are classified (`lint`, a flow stage, or `tool_crash`). A tool crash (exit 134/137/139 or a "Segmentation fault" banner) wipes the run's partial state and retries once (`crash_retries`). A repeated crash is `tool_crash`, never a verdict on the design. `FlowResult.attempts` records retries.
- **Cache key** now hashes RTL contents only (so the same RTL under another name hits) plus the exact Docker image id instead of the `latest` tag. `num_cores` and `crash_retries` are not part of the key.
- **Tests**: `tests/test_flow_unit.py` (6 tests, Docker faked): crash then success retries, repeated crash becomes `tool_crash`, ordinary failure is not retried, cache hit skips Docker, rename hits the cache, edited RTL or clock misses.

### Parallel run: six 8-bit ALU variants (`examples/parallel_demo/`), sky130hd, 5 ns
Wall time 336 s for all six, against about 4-5 min each run alone.

| Candidate | Area (µm²) | Setup slack (ns) | Power (mW) | Status |
|---|---|---|---|---|
| v0_baseline | 2269.7 | +1.648 | 0.3611 | met |
| v2_reverse_shifter | 2172.1 | +1.113 | 0.3609 | met |
| v3_lt_from_sub | 2359.8 | +1.551 | 0.3770 | met |
| v4_all_shared | 2000.7 | +1.184 | 0.3455 | met, but **functionally wrong on purpose** |
| v1_shared_addsub | 2168.3 | +1.388 | n/a | met (after the crash below) |
| v5_broken | n/a | n/a | n/a | failed at lint in 1 s |


- **v4 is the smallest only because its `lt` output is wrong.** The flow cannot tell, since the RTL is valid. This is exactly what the verification gate must reject, and it is the first acceptance test for it.
- **v1 crashed in the parallel run** (OpenROAD segfault after CTS). Run alone, twice with the cache off, it completed both times with identical results (2168.3 µm², +1.388 ns). So the crash was a flake under load, not a design problem, and the reruns also show the flow is deterministic. This is why crash handling and the retry were added.

### Speed notes
- 8-bit ALU: 3-6 min per run. 16-bit ALU: about 15 min, of which about 13.5 min is detailed routing (`5_2_route`), under x86 emulation. Parallelism raises throughput but not per-candidate latency.
- A fast screening mode (stop after global route) is planned.

### Still not done
Verification gate (cocotb + Verilator), fast screening mode, liberty-based area annotator, LLM proposers, loop/CLI/report, Windows (WSL2) notes.
