# Environment Setup

Steps used to build the PPA Optimizer environment on macOS (Apple Silicon, arm64). Metrics come from Yosys and OpenROAD running the sky130hd PDK.

## Tools installed

| Tool | Version | Where it runs | Purpose |
|---|---|---|---|
| Yosys | 0.69+post | native (`/opt/homebrew/bin/yosys`), already installed | quick synthesis |
| Verilator | 5.052 | native (brew) | simulator for cocotb benches |
| cocotb | 2.1.0 | Python venv | testbenches |
| Python | 3.9.6 (system) | native | tool code |
| Colima + Docker | latest (brew) | native | Linux VM for OpenROAD |
| OpenROAD-flow-scripts | `openroad/orfs:latest` image | Docker, linux/amd64 | Yosys + OpenROAD flow, sky130hd PDK |

OpenROAD has no brew formula and no macOS build, so it runs in the ORFS Docker image.

## Steps

### 1. Simulator and cocotb

```sh
brew install verilator
cd "<project root>"
python3 -m venv .venv
.venv/bin/pip install cocotb pytest anthropic
verilator --version                       # Verilator 5.052
.venv/bin/python -c "import cocotb; print(cocotb.__version__)"   # 2.1.0
```

`anthropic` is for the production LLM backend (API mode).

### 2. Linux VM for OpenROAD

```sh
brew install colima docker
colima start --cpu 4 --memory 8 --vm-type vz --vz-rosetta
docker version --format '{{.Server.Os}}/{{.Server.Arch}}'   # linux/arm64
```

- `colima start --arch x86_64` failed because it needs QEMU (`qemu-img not found`).
- The fix was an arm64 VM with the `vz` driver and Rosetta, which runs amd64 containers.
- Restart the VM after a reboot with `colima start`.

### 3. OpenROAD image

```sh
docker pull --platform linux/amd64 openroad/orfs:latest
```

- The image only publishes amd64, so every `docker run` needs `--platform linux/amd64`.
- The binaries are not on `PATH`. They live under `/OpenROAD-flow-scripts/tools/install/` (`OpenROAD/bin/openroad`, `yosys/bin/yosys`).
- Platforms are in `/OpenROAD-flow-scripts/flow/platforms/`: nangate45, sky130hd, sky130hs, asap7, gf180, ihp-sg13g2 and others.

### 4. PDK choice

The PDK is sky130hd (SkyWater 130 nm). Its liberty file is `sky130_fd_sc_hd__tt_025C_1v80.lib` in `platforms/sky130hd/lib/`. nangate45 and asap7 are available as alternatives by changing `PLATFORM` in the design `config.mk`.

### 5. Project scaffold

```
PPA Optimizer/
  .venv/              Python env
  development/        this documentation
  examples/alu/alu.v  example design (8-bit ALU)
  ppa_opt/            package: llm/ synth/ annotate/ verify/
  skills/  tests/  work/
  work/alu/           ORFS inputs and outputs for the example
```

### 6. Test run of the full flow (sky130hd, ALU)

Inputs in `work/alu/`:

`config.mk`
```make
export DESIGN_NAME = alu
export PLATFORM = sky130hd
export VERILOG_FILES = /work/alu.v
export SDC_FILE = /work/constraint.sdc
export CORE_UTILIZATION = 30
export PLACE_DENSITY_LB_ADDON = 0.20
export WORK_HOME = /work/run
```

`constraint.sdc`
```tcl
create_clock -name clk -period 5.0 [get_ports clk]
set_input_delay 1.0 -clock clk [all_inputs -no_clocks]
set_output_delay 1.0 -clock clk [all_outputs]
```

Run (about 2 minutes under emulation):

```sh
cd "<project root>"
cp examples/alu/alu.v work/alu/alu.v
docker run --rm --platform linux/amd64 -v "$PWD/work/alu:/work" openroad/orfs:latest \
  bash -lc 'cd /OpenROAD-flow-scripts/flow && make DESIGN_CONFIG=/work/config.mk'
```

### 7. Where the metrics are

Final metrics are in `work/alu/run/logs/sky130hd/alu/base/6_report.json`:

| Metric | Key | ALU baseline |
|---|---|---|
| Area (std cells) | `finish__design__instance__area__stdcell` | 2210.9 µm² (331 cells) |
| Setup slack | `finish__timing__setup__ws` | +1.565 ns at 5 ns clock |
| Hold slack | `finish__timing__hold__ws` | +1.414 ns |
| Power | `finish__power__total` | 0.352 mW |

Notes:
- `finish__design__instance__area` also counts fill and tap cells, so use the `__stdcell` key for area.
- Power uses OpenROAD's default switching activity, so it is an estimate.

## Next steps

1. `synth/openroad_flow.py`: wrap step 6, with RTL file, clock period and PDK as inputs, a metrics JSON as output, and caching by RTL hash.
2. `verify/`: cocotb benches run with Verilator, as the gate before any candidate is measured.
3. `annotate/`: liberty-based area annotation per RTL line.
4. `llm/`: subagent (dev) and Anthropic API (prod) proposers.
5. `loop.py` and `cli.py`.
