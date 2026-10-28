"""SystemVerilog -> area / timing / power, via the OpenROAD-flow-scripts Docker image.

Every number returned comes from OpenROAD's own report JSON. Each metric is
paired with the report key it was read from, so nothing is LLM-inferred.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .timing import read_critical_path

IMAGE = "openroad/orfs:latest"
PLATFORM_FLAG = "linux/amd64"
FLOW_DIR = "/OpenROAD-flow-scripts/flow"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORK = PROJECT_ROOT / "work" / "runs"
DEFAULT_CACHE = PROJECT_ROOT / "work" / "cache"

# metric name -> key in logs/<pdk>/<design>/base/6_report.json
METRIC_KEYS = {
    "area_um2": "finish__design__instance__area__stdcell",
    "cell_count": "finish__design__instance__count__stdcell",
    "sequential_area_um2": "finish__design__instance__area__class:sequential_cell",
    "sequential_count": "finish__design__instance__count__class:sequential_cell",
    "setup_wns_ns": "finish__timing__setup__ws",
    "setup_tns_ns": "finish__timing__setup__tns",
    "hold_wns_ns": "finish__timing__hold__ws",
    "power_total_w": "finish__power__total",
    "power_internal_w": "finish__power__internal__total",
    "power_switching_w": "finish__power__switching__total",
    "power_leakage_w": "finish__power__leakage__total",
    "die_area_um2": "finish__design__die__area",
    "utilization": "finish__design__instance__utilization__stdcell",
}
CLOCK_NAMES = ("clk", "clock", "CLK", "CLOCK", "clk_i", "i_clk")


class FlowError(RuntimeError):
    """The flow failed; `stage` says where and `log_tail` shows why."""

    def __init__(self, stage: str, message: str, log_tail: str = ""):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.log_tail = log_tail


@dataclass(frozen=True)
class FlowConfig:
    top: str
    clock_ns: float = 5.0
    pdk: str = "sky130hd"
    clock_port: str | None = "auto"  # "auto", a port name, or None for a virtual clock
    io_delay_frac: float = 0.2       # input/output delay as a fraction of the clock
    core_utilization: int = 30
    hdl_frontend: str = "slang"      # "slang" handles real SystemVerilog; "" = yosys read_verilog -sv
    abc_area: bool = False           # True: ABC optimizes area instead of speed
    timeout_s: int = 1800
    num_cores: int = 1               # CPUs per run; 1 lets many candidates run side by side
    crash_retries: int = 1           # re-runs after an OpenROAD/Yosys crash (e.g. segfault)
    # Timing knobs for the iteration loop. At their defaults they are left out of the cache key,
    # so results cached before these existed stay valid.
    synth_hierarchical: bool = False       # keep module hierarchy through synthesis
    place_density_addon: float = 0.20      # PLACE_DENSITY_LB_ADDON
    setup_slack_margin: float = 0.0        # ns of extra setup margin the timing repair aims for
    gpl_timing_driven: bool | None = None  # None: ORFS default


_KNOB_DEFAULTS = {"synth_hierarchical": False, "place_density_addon": 0.20,
                  "setup_slack_margin": 0.0, "gpl_timing_driven": None}


@dataclass
class FlowResult:
    config: dict
    metrics: dict
    provenance: dict       # metric name -> "<report file>:<key>"
    rtl_hash: str
    cache_hit: bool
    run_dir: str
    wall_s: float
    derived: dict = field(default_factory=dict)  # computed here from tool metrics only
    attempts: int = 1      # more than 1 means an earlier attempt hit a tool crash
    critical_path: dict | None = None   # worst setup path parsed from 6_finish.rpt (see timing.py)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


_IMAGE_ID: str | None = None


def _image_id() -> str:
    """Exact image content id, so a changed `latest` tag cannot reuse stale results."""
    global _IMAGE_ID
    if _IMAGE_ID is None:
        try:
            out = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
                                 capture_output=True, text=True, timeout=60)
            _IMAGE_ID = out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else IMAGE
        except (OSError, subprocess.TimeoutExpired):
            _IMAGE_ID = IMAGE
    return _IMAGE_ID


def _hash_inputs(rtl: dict[str, str], cfg: FlowConfig) -> str:
    h = hashlib.sha256()
    for text in sorted(rtl.values()):   # contents only: the same RTL under another name is a hit
        h.update(hashlib.sha256(text.encode()).digest())
    # Threads do not change OpenROAD's results, so they stay out of the cache key.
    key_cfg = {k: v for k, v in asdict(cfg).items()
               if k not in ("num_cores", "crash_retries") and not (k in _KNOB_DEFAULTS and v == _KNOB_DEFAULTS[k])}
    h.update(json.dumps(key_cfg, sort_keys=True).encode())
    h.update(_image_id().encode())
    return h.hexdigest()[:20]


def _detect_clock(rtl: dict[str, str], top: str) -> str | None:
    """Find a clock port on the top module's header; None means combinational."""
    text = "\n".join(rtl.values())
    m = re.search(rf"\bmodule\s+{re.escape(top)}\b(.*?)\);", text, re.S)
    header = m.group(1) if m else text
    for name in CLOCK_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", header):
            return name
    return None


def _sdc(cfg: FlowConfig, clock_port: str | None) -> str:
    delay = round(cfg.clock_ns * cfg.io_delay_frac, 4)
    if clock_port:
        clk = f"create_clock -name clk -period {cfg.clock_ns} [get_ports {clock_port}]\n"
        ins = f"set_input_delay {delay} -clock clk [all_inputs -no_clocks]\n"
    else:
        clk = f"create_clock -name clk -period {cfg.clock_ns}\n"
        ins = f"set_input_delay {delay} -clock clk [all_inputs]\n"
    outs = f"set_output_delay {delay} -clock clk [all_outputs]\n"
    return clk + ins + outs


def _config_mk(cfg: FlowConfig, files: list[str]) -> str:
    lines = [
        f"export DESIGN_NAME = {cfg.top}",
        f"export PLATFORM = {cfg.pdk}",
        "export VERILOG_FILES = " + " ".join(f"/work/{f}" for f in files),
        "export SDC_FILE = /work/constraint.sdc",
        f"export CORE_UTILIZATION = {cfg.core_utilization}",
        f"export PLACE_DENSITY_LB_ADDON = {cfg.place_density_addon}",
        "export WORK_HOME = /work/run",
        f"export NUM_CORES = {cfg.num_cores}",
    ]
    if cfg.hdl_frontend:
        lines.append(f"export SYNTH_HDL_FRONTEND = {cfg.hdl_frontend}")
    if cfg.abc_area:
        lines.append("export ABC_AREA = 1")
    if cfg.synth_hierarchical:
        lines.append("export SYNTH_HIERARCHICAL = 1")
    if cfg.setup_slack_margin:
        lines.append(f"export SETUP_SLACK_MARGIN = {cfg.setup_slack_margin}")
    if cfg.gpl_timing_driven is not None:
        lines.append(f"export GPL_TIMING_DRIVEN = {int(cfg.gpl_timing_driven)}")
    return "\n".join(lines) + "\n"


def _tail(path: Path, n: int = 40) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _is_tool_crash(returncode: int, output: str) -> bool:
    """Signals (139 segfault, 134 abort, 137 killed) or a crash banner in the tool output."""
    markers = ("Segmentation fault", "core dumped", "Aborted", "Killed", "terminate called")
    return returncode in (134, 137, 139) or any(m in output for m in markers)


def _failed_stage(run_dir: Path, output: str) -> str:
    logs = sorted((run_dir / "run" / "logs").rglob("*.log"), key=lambda p: p.stat().st_mtime)
    if logs:
        return logs[-1].stem
    return "docker" if "docker" in output.lower() else "flow"


def lint(rtl_files: list[Path | str], top: str, timeout_s: int = 120) -> None:
    """Elaborate with slang (about a second) so bad RTL fails fast with real diagnostics."""
    files = [Path(f).resolve() for f in rtl_files]
    mounts = {f.parent for f in files}
    cmd = ["docker", "run", "--rm", "--platform", PLATFORM_FLAG]
    for i, d in enumerate(sorted(mounts)):
        cmd += ["-v", f"{d.as_posix()}:/src{i}:ro"]
    ordered = sorted(mounts)
    paths = " ".join(f"/src{ordered.index(f.parent)}/{f.name}" for f in files)
    ys = f"read_slang --top {top} {paths}; hierarchy -check -top {top}"
    cmd += [IMAGE, "/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys", "-p", ys]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        raise FlowError("docker", "docker not found; is it installed and on PATH?")
    except subprocess.TimeoutExpired:
        raise FlowError("lint", f"lint exceeded {timeout_s}s")
    if proc.returncode != 0:
        out = (proc.stdout + proc.stderr).splitlines()
        diag = [l for l in out if re.search(r"error|warning|\^|\d+ \|", l, re.I)]
        raise FlowError("lint", "elaboration failed", "\n".join(diag or out[-30:]))


def run_flow(
    rtl_files: list[Path | str],
    cfg: FlowConfig,
    *,
    work_dir: Path = DEFAULT_WORK,
    cache_dir: Path = DEFAULT_CACHE,
    use_cache: bool = True,
) -> FlowResult:
    rtl = {Path(f).name: Path(f).read_text() for f in rtl_files}
    if len(rtl) != len(rtl_files):
        raise FlowError("input", "RTL files must have unique basenames")
    key = _hash_inputs(rtl, cfg)

    cache_file = cache_dir / f"{key}.json"
    if use_cache and cache_file.exists():
        data = json.loads(cache_file.read_text())
        data["cache_hit"] = True
        return FlowResult(**data)

    if cfg.hdl_frontend == "slang":
        lint(rtl_files, cfg.top)

    clock_port = _detect_clock(rtl, cfg.top) if cfg.clock_port == "auto" else cfg.clock_port
    run_dir = (work_dir / key).resolve()
    if run_dir.exists():
        subprocess.run(["rm", "-rf", str(run_dir)], check=True)
    run_dir.mkdir(parents=True)
    for name, text in rtl.items():
        (run_dir / name).write_text(text)
    (run_dir / "constraint.sdc").write_text(_sdc(cfg, clock_port))
    (run_dir / "config.mk").write_text(_config_mk(cfg, list(rtl)))

    cmd = [
        "docker", "run", "--rm", "--platform", PLATFORM_FLAG, "--cpus", str(cfg.num_cores),
        "-v", f"{run_dir.as_posix()}:/work", IMAGE,
        "bash", "-lc", f"cd {FLOW_DIR} && make DESIGN_CONFIG=/work/config.mk",
    ]
    report = run_dir / "run" / "logs" / cfg.pdk / cfg.top / "base" / "6_report.json"
    t0 = time.time()
    attempts = 0
    while True:
        attempts += 1
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.timeout_s)
        except subprocess.TimeoutExpired:
            raise FlowError("timeout", f"flow exceeded {cfg.timeout_s}s")
        except FileNotFoundError:
            raise FlowError("docker", "docker not found; is it installed and on PATH?")
        output = proc.stdout + proc.stderr
        (run_dir / "flow.log").write_text(output)
        if proc.returncode == 0 and report.exists():
            break
        if _is_tool_crash(proc.returncode, output):
            if attempts <= cfg.crash_retries:
                # A crashed stage can leave a half-written database; start the run clean.
                subprocess.run(["rm", "-rf", str(run_dir / "run")], check=True)
                continue
            raise FlowError("tool_crash", f"EDA tool crashed on {attempts} attempt(s); "
                            "this is a tool failure, not a verdict on the design",
                            "\n".join(output.splitlines()[-30:]))
        stage = _failed_stage(run_dir, output)
        stage_logs = list((run_dir / "run" / "logs").rglob(f"{stage}.log"))
        detail = _tail(stage_logs[0], 60) if stage_logs else "\n".join(output.splitlines()[-40:])
        raise FlowError(stage, "flow did not complete", detail)
    wall = time.time() - t0

    raw = json.loads(report.read_text())
    metrics, prov = {}, {}
    for name, rkey in METRIC_KEYS.items():
        if rkey in raw:
            metrics[name] = raw[rkey]
            prov[name] = f"{report.relative_to(run_dir).as_posix()}:{rkey}"
    missing = [n for n in ("area_um2", "setup_wns_ns", "power_total_w") if n not in metrics]
    if missing:
        raise FlowError("report", f"report is missing metrics: {missing}")

    # Derived only from the tool metrics above (no outside data).
    derived = {}
    slack = metrics["setup_wns_ns"]
    achieved = cfg.clock_ns - slack
    if achieved > 0:
        derived["min_period_ns"] = round(achieved, 4)
        derived["fmax_mhz"] = round(1000.0 / achieved, 2)
    derived["timing_met"] = slack >= 0 and metrics.get("setup_tns_ns", 0) == 0
    derived["clock_port"] = clock_port or "(virtual)"

    result = FlowResult(
        config=asdict(cfg), metrics=metrics, provenance=prov, rtl_hash=key,
        cache_hit=False, run_dir=str(run_dir), wall_s=round(wall, 1), derived=derived,
        attempts=attempts, critical_path=read_critical_path(run_dir, cfg.pdk, cfg.top),
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(result.to_json())
    return result


def format_summary(res: FlowResult) -> str:
    m, d, c = res.metrics, res.derived, res.config
    met = "MET" if d.get("timing_met") else "VIOLATED"
    rows = [
        ("Design", f"{c['top']}  ({c['pdk']}, {c['clock_ns']} ns clock on {d['clock_port']})"),
        ("Area", f"{m['area_um2']:.1f} um^2  ({m.get('cell_count', '?')} cells, "
                 f"{m.get('sequential_count', 0)} sequential)"),
        ("Timing", f"setup slack {m['setup_wns_ns']:+.3f} ns ({met}), hold slack "
                   f"{m.get('hold_wns_ns', float('nan')):+.3f} ns"),
        ("Speed", f"min period {d.get('min_period_ns', float('nan'))} ns  "
                  f"(~{d.get('fmax_mhz', float('nan'))} MHz)"),
        ("Power", f"{m['power_total_w'] * 1e3:.4f} mW  (internal {m.get('power_internal_w', 0) * 1e3:.4f}, "
                  f"switching {m.get('power_switching_w', 0) * 1e3:.4f}, "
                  f"leakage {m.get('power_leakage_w', 0) * 1e3:.6f})  [estimate]"),
        ("Run", f"{'cache hit' if res.cache_hit else f'{res.wall_s} s'}  ->  {res.run_dir}"),
    ]
    return "\n".join(f"  {k:<7}{v}" for k, v in rows)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="SystemVerilog -> area/timing/power (sky130hd by default)")
    ap.add_argument("rtl", nargs="+", help="SystemVerilog/Verilog source files")
    ap.add_argument("--top", required=True)
    ap.add_argument("--clock-ns", type=float, default=5.0)
    ap.add_argument("--pdk", default="sky130hd")
    ap.add_argument("--clock-port", default="auto", help="port name, 'auto', or 'none'")
    ap.add_argument("--utilization", type=int, default=30)
    ap.add_argument("--frontend", default="slang", help="'slang' or '' for yosys read_verilog -sv")
    ap.add_argument("--abc-area", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--json", action="store_true", help="print full JSON instead of a summary")
    ap.add_argument("--out", help="also write the full JSON result to this path")
    a = ap.parse_args()

    cfg = FlowConfig(
        top=a.top, clock_ns=a.clock_ns, pdk=a.pdk,
        clock_port=None if a.clock_port == "none" else a.clock_port,
        core_utilization=a.utilization, hdl_frontend=a.frontend, abc_area=a.abc_area,
    )
    try:
        res = run_flow(a.rtl, cfg, use_cache=not a.no_cache)
    except FlowError as e:
        print(f"FAILED {e}\n{e.log_tail}")
        raise SystemExit(1)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(res.to_json())
    print(res.to_json() if a.json else format_summary(res))


if __name__ == "__main__":
    main()
