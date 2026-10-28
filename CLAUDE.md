# PPA Optimizer

An AI-assisted RTL PPA exploration tool. An LLM proposes SystemVerilog rewrites, deterministic EDA tools (Yosys + OpenROAD, sky130hd) measure power, performance and area, and a loop converges on the best design. For history, setup steps and results, see the `development/` folder.

## Rules that must hold
- **Every metric comes from a tool report.** Numbers are read from OpenROAD's `6_report.json` and carry a `provenance` entry (report file and key). The LLM never supplies or estimates a number.
- **Verification gate before measurement.** A candidate must pass the cocotb + Verilator suite before it is measured, ranked or reported. There is no golden RTL: the suite is written from the spec alone (never from RTL) by a separate agent, and must itself be *qualified* first. See `ppa_opt/verify/` and `ppa_opt/verify/SUITE_FORMAT.md`.
- **Tool crashes are not design failures.** Failure classes are `lint`, a flow stage name, or `tool_crash`. Only lint and flow-stage failures count against a candidate.
- Power is an estimate (OpenROAD default switching activity). Label it that way.

## How the flow works
Code: [ppa_opt/synth/openroad_flow.py](ppa_opt/synth/openroad_flow.py) and [ppa_opt/synth/parallel.py](ppa_opt/synth/parallel.py).

```
SV files -> lint (slang, ~1 s) -> cache lookup -> ORFS in Docker -> 6_report.json -> metrics + provenance
```

1. **Lint** (`lint()`): elaborates with slang in the container. Bad RTL fails in about a second with `file:line:col` errors, before any long run.
2. **Cache** (`run_flow()`): key = hash of RTL contents (not file names) + `FlowConfig` + exact Docker image id. A hit returns immediately with `cache_hit: true`. Failures are not cached. Cache lives in `work/cache/`, run directories in `work/runs/<key>/`.
3. **Flow**: generates `config.mk` and the SDC (clock port auto-detected, virtual clock if none, I/O delay 20% of the period), then runs the OpenROAD-flow-scripts `make` in the `openroad/orfs` image: Yosys synthesis, floorplan, placement, CTS, global and detailed route, report.
4. **Metrics** (`METRIC_KEYS`): `area_um2` (std cells only), `setup_wns_ns`, `hold_wns_ns`, `power_total_w` (+ internal, switching, leakage), cell counts, die area. Derived from those only: `min_period_ns`, `fmax_mhz`, `timing_met`.
5. **Crash handling**: exit 134/137/139 or a "Segmentation fault"-style banner is a tool crash. The partial run is wiped and retried once (`crash_retries`); a repeat raises `FlowError("tool_crash")`.
6. **Parallel evaluation** (`run_many()`): the core of the tool. Each candidate runs on one CPU (`num_cores=1`), so the Docker VM's CPUs (8) are 8 slots. Identical RTL runs once, results keep input order, and one failure never stops the others.

## Commands
```
make <name>.analysis              # area/timing/power for <name>.sv or .v (searches ., examples/, examples/*/)
make <name>.lint                  # fast elaboration check only
make compare FILES="a.sv b.sv"    # parallel comparison table
make rtl SPEC=x.md                # agents: resolve a loose spec if needed, then write examples/<top>.sv
make suite SPEC=x.md              # test-author writes suites/<top>/ from the spec (never sees RTL)
make suite-improve SPEC=x.md SUITE=suites/x RTL=x.sv   # qualify; feed coverage/mutation gaps back to test-author
make qualify SUITE=suites/x RTL=x.sv   # is the suite trustworthy? (passes RTL, spec cover points hit, code coverage, mutants killed)
make verify SUITE=suites/x FILES="a.sv b.sv" [MAXLAT=N]   # gate candidates; MAXLAT lets them have up to N extra output cycles
make compare FILES=... SUITE=suites/x  # same gate, then measure only the passers
make ppa DESIGN=x SUITE=suites/x  # verify, then synthesis + PPA flow
make iterate SPEC=x.md SUITE=suites/x [RTL=x.sv] CLOCK=5 [ITERS=<n>|auto N= KNOBS= BUDGET= MAXLAT= PIPENOW= NAME= FRESH=1]   # fix slack, then cut area; a run folder with a history is continued from its best (RTL only for a new run)
make viz [DESIGN=run]             # HTML report (work/report.html) of iteration runs: all candidates, area/power/delay/latency per round
make clean-cache                  # drop work/cache and work/runs
# options: TOP=<module> CLOCK=<ns> PDK=<platform> JSON=1 EXTRA='--abc-area'
.venv/bin/python -m pytest tests -q
```
Top module defaults to the file's basename. Full JSON for each run is written to `work/results/`.

## Environment
- macOS arm64. Python venv at `.venv` (cocotb, pytest, anthropic). Verilator via brew.
- OpenROAD runs in Docker via Colima: `colima start --cpu 8 --memory 16 --vm-type vz --vz-rosetta`. The image is amd64-only, so every `docker run` uses `--platform linux/amd64`. Start the VM after a reboot.
- PDK is `sky130hd` (one config field; nangate45 and asap7 are also in the image).
- Details and install steps: [development/enviroment_setup.md](development/enviroment_setup.md).
- A native linux/arm64 flow image `ppa-orfs:arm64` is built (`docker/arm64/`, see its README): same area/cell counts as the x86 image, slack within about 10 ps, roughly 1.3-1.4x faster per flow. The flow code still uses the x86 image; switching is the image name plus `--platform linux/arm64` (and the cache key changes). Run directories must be under `$HOME` to be visible in the VM.

## Status and known limits
- **Built:** lint, cached flow, metrics with provenance, crash retry, parallel runner, Makefile, iteration loop, verification gate (`ppa_opt/verify/`: spec-derived suite = Python reference model + SVA + functional cover points; Verilator code coverage; mutation check of the suite; `test-author` agent in `.claude/agents/` writes suites from the spec only), 54 tests; `ppa_opt/viz/` renders iteration history as a standalone HTML report.
- **Not built:** automated spec-to-suite step in the loop (today the agent is invoked by hand), suite repair when the RTL and suite disagree, formal checks, fast screening mode, liberty-based area annotator, LLM proposers (subagent for dev, Anthropic API for production), CLI, report. The iteration loop exists but is dev-agent driven. It is cycle-exact unless `MAXLAT>0` is given; then it may add pipeline stages (latency-tolerant gate check, SVA skipped for that check) and reports the added latency.
- **Speed:** a cold run is 3-6 min for an 8-bit design and about 15 min for 16-bit, mostly detailed routing under x86 emulation. Parallelism raises throughput, not per-run latency.
- **Demo caveat:** `examples/parallel_demo/v4_all_shared.sv` is deliberately wrong. The flow cannot detect it; the gate rejects it (`lt` mismatch / SVA failure).
- **Gate notes:** `qualify` verdicts: `qualified`, `rtl_suite_disagree` (RTL and suite read the spec differently; adjudicate against the spec, blame neither automatically), `suite_weak` (uncovered cover points, low coverage or surviving mutants), `suite_invalid`. Demo suite: `suites/alu_p` from `examples/alu_spec/spec.md`; qualify takes ~3 min (mutant builds). Verilator builds run under the system temp dir because GNU make fails on the space in the project path. Build failures and simulator crashes are `build`/`tool_crash`: the candidate is not measured but it is not a design verdict. Top-module ports must be plain logic vectors.

## Dev workflow (agents now, Anthropic API later)
1. Spec as MD (see `examples/alu_spec/spec.md`). A loose description with no port table goes through the `spec-resolver` agent first, which writes `<name>.resolved.md` with inferred decisions listed; check it with `python -m ppa_opt.verify.spec <file> [--suite dir]`. Both authors then read the resolved spec. `make rtl SPEC=<spec.md>` and `make suite SPEC=<spec.md>` automate resolver + rtl-author / test-author headless (`make suite-improve` loops qualify -> gaps -> test-author) (`ppa_opt/llm/devagents.py`; needs the `claude` CLI, found on PATH, `CLAUDE_BIN`, or the VS Code extension bundle).
2. Agent `test-author` (spec only) writes `suites/<name>/`; agent `rtl-author` (spec only) writes the first RTL. Run them as separate subagents so neither sees the other's output.
3. `make qualify SUITE=suites/<name> RTL=<first rtl>`; on `suite_weak` send the report back to `test-author`, on `rtl_suite_disagree` check the spec by hand.
4. Rewrites go through `rtl-author` with the goal; `make compare FILES=... SUITE=suites/<name>` gates then measures. `make iterate SPEC=.. SUITE=.. RTL=.. CLOCK=<ns>` automates this (`ppa_opt/loop/iterate.py`): N cycle-exact rewrites + flow-knob variants per round, gate, measure, keep the best; violating setup slack is fixed first, then area is minimized with timing kept met. History and best RTL land in `work/iterations/<name>/`; re-running into a folder that has a history continues it (starts from its best.sv, appends rounds, keeps its latency; `FRESH=1` moves the old folder aside). `MAXLAT=N` lets it add up to N pipeline stages when cycle-exact rewrites stall or close under 25% of the remaining violation in a round; it changes output latency versus the spec, so use it only when that is acceptable.
Every RTL file the agent writes carries a header comment (Strategy, What changed, Why it helps, Equivalence, Latency, Tradeoffs) and per-block comments with `// OPT:` markers on changed logic; the loop notes under-documented candidates (`doc_notes`) but does not reject them. Agents live in `.claude/agents/`. Production swaps them for `ppa_opt/llm/` API calls, with the verifier in a sandbox container (not built). Today the suite's Python runs unsandboxed on the host; the simulation has a 600 s wall budget but no isolation.

## Where to look
- [development/enviroment_setup.md](development/enviroment_setup.md): install steps, Docker/Colima setup, where each metric lives.
- [development/session0.md](development/session0.md): initial status.
- [development/session1.md](development/session1.md): the flow, parallel runs, crash handling, results and caveats.
- [development/session2.md](development/session2.md): verification gate, dev agents, iteration loop (incl. pipelining), real-run results.
- [README.md](README.md): step-by-step usage.
- Plan: `~/.claude/plans/pasted-content-id-f266-in-professional-rippling-wren.md`.
