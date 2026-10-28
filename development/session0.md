# Session 0 — Status

Environment only; no tool code yet.

## Done

- **Planning:** wrote and got approval for the plan. Decisions: subagents in dev and the Anthropic API in production, Yosys + OpenROAD for metrics, Verilator for simulation, cocotb only (no UVM).
- **Dropped Assignment 1:** removed the copied annotator skills and example designs. The area annotator will be built fresh.
- **Installed:**
  - Verilator 5.052 (brew)
  - Python venv (`.venv`) with cocotb 2.1.0, pytest and anthropic
  - Colima + Docker (an arm64 VM with Rosetta; the x86_64 QEMU route failed)
  - the `openroad/orfs` image, with OpenROAD and Yosys
- **PDK:** chose sky130hd.
- **Proved the flow:** ran the full Yosys → OpenROAD flow on my own 8-bit ALU (`examples/alu/alu.v`). The baseline is 2210.9 µm² of std-cell area, +1.565 ns setup slack at a 5 ns clock, and 0.352 mW of estimated power. The numbers come from OpenROAD's `6_report.json`.
- **Docs:** wrote `development/enviroment_setup.md` with the install steps and where to find each metric.
- **Scaffold:** created the empty `ppa_opt/` package folders (`llm`, `synth`, `annotate`, `verify`), plus `skills/`, `tests/` and `work/`.

## Not started

- `synth/openroad_flow.py`, the wrapper with caching by RTL hash
- the Verilator + cocotb verification gate
- the liberty-based area annotator
- the LLM proposers (subagent and API)
- the optimization loop, the CLI and the report
- a Windows (WSL2) section for the setup doc (asked about, not confirmed)
