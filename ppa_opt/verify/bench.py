"""Build and run one design under the suite: RTL (+ the suite's SVA) in Verilator, cocotb driving
it, outputs checked against the reference model, code coverage collected from the RTL."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .interface import VERILATOR_FLAGS
from .suite import Suite

HERE = Path(__file__).resolve().parent
COV_FLAGS = ["--coverage-line", "--coverage-toggle"]


class BenchError(RuntimeError):
    """`kind` is "build" (Verilator rejected the RTL/SVA), "suite" (the suite itself raised)
    or "tool_crash" (the simulator died)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class SimResult:
    status: str                       # "pass" | "fail"
    failure: str | None               # "mismatch" | "assertion" | None
    cycles: int
    mismatch: dict | None = None
    cover: dict[str, int] = field(default_factory=dict)
    code_cov: dict[str, float] = field(default_factory=dict)   # {"line": 0..1, "toggle": 0..1}
    seed: int = 0
    message: str = ""
    latency: int | None = None        # extra output latency found (tolerant mode only)


def wrapper_source(suite: Suite, with_sva: bool = True) -> str:
    """tb_top: the DUT plus the suite's SVA checker on the same nets."""
    decl, conn = [], []
    for n, (d, w) in suite.ports.items():
        rng = f"[{w - 1}:0] " if w > 1 else ""
        decl.append(f"  {d:6s} logic {rng}{n}")
        conn.append(f".{n}({n})")
    body = f"  {suite.top} u_dut ({', '.join(conn)});\n"
    if suite.sva and with_sva:
        body += f"  {suite.top}_sva u_sva ({', '.join(conn)});\n"
    return "module tb_top (\n" + ",\n".join(decl) + "\n);\n" + body + "endmodule\n"


_link_lock = threading.Lock()


def _use_space_free_cocotb() -> None:
    """GNU make cannot handle spaces, and cocotb's lib/share dirs live inside the project
    venv (whose path has one). Point cocotb at a symlink to the same directory."""
    import cocotb_tools.config as cfg

    with _link_lock:
        if " " not in str(cfg.libs_dir):
            return
        link = Path(tempfile.gettempdir()) / "ppa_opt_cocotb"
        target = cfg.libs_dir.parent
        if link.is_symlink() and link.resolve() != target:
            link.unlink()
        if not link.exists():
            link.symlink_to(target)
        cfg.share_dir, cfg.libs_dir = link / "share", link / "libs"


def _tail(path: Path, n: int = 1500) -> str:
    return path.read_text(errors="replace")[-n:] if path.exists() else ""


def parse_coverage(dat: Path, rtl_names: set[str]) -> dict[str, float]:
    """Line and toggle coverage of the RTL files only (the bench and SVA are excluded)."""
    totals: dict[str, list[int]] = {"line": [0, 0], "toggle": [0, 0]}
    if not dat.exists():
        return {}
    for raw in dat.read_text(errors="replace").splitlines():
        if not raw.startswith("C '"):
            continue
        body, _, count = raw[3:].rpartition("' ")
        fields = dict(f.split("\x02", 1) for f in body.split("\x01") if "\x02" in f)
        if Path(fields.get("f", "")).name not in rtl_names:
            continue
        page = fields.get("page", "")
        kind = "toggle" if page.startswith("v_toggle") else "line" if page.startswith("v_line") else None
        if kind:
            totals[kind][1] += 1
            totals[kind][0] += int(int(count.strip() or 0) > 0)
    return {k: (hit / n if n else 1.0) for k, (hit, n) in totals.items()}


def run_suite(rtl_files: list[Path], suite: Suite, build_dir: Path, *, n_random: int | None = None,
              seed: int = 1, retries: int = 1, coverage: bool = True,
              max_extra_latency: int = 0) -> SimResult:
    """Raises BenchError. Simulator crashes are retried `retries` times before being reported."""
    from cocotb_tools.runner import get_runner

    _use_space_free_cocotb()
    root = str(HERE.parents[1])      # the simulator process must be able to import ppa_opt
    if root not in sys.path:     # the cocotb runner builds the simulator's PYTHONPATH from sys.path
        sys.path.insert(0, root)
    if root not in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (root, os.environ.get("PYTHONPATH", "")) if p)
    if build_dir.exists():
        shutil.rmtree(build_dir)
    src = build_dir / "src"
    src.mkdir(parents=True)
    sources, rtl_names = [], set()
    for i, f in enumerate(rtl_files):
        out = src / f"{i}_{Path(f).name}"      # copy: the build never touches user files
        shutil.copyfile(f, out)
        sources.append(out)
        rtl_names.add(out.name)
    # The suite's SVA assumes the original latency, so it is left out of latency-tolerant runs.
    with_sva = bool(suite.sva) and max_extra_latency == 0
    if with_sva:
        shutil.copyfile(suite.sva, src / "assertions.sv")
        sources.append(src / "assertions.sv")
    (src / "tb_top.sv").write_text(wrapper_source(suite, with_sva))
    sources.append(src / "tb_top.sv")

    result_path, cfg_path = build_dir / "result.json", build_dir / "cfg.json"
    cfg_path.write_text(json.dumps({
        "suite_dir": str(suite.path), "data_inputs": suite.data_inputs, "outputs": suite.outputs,
        "clock": suite.clock, "reset": list(suite.reset) if suite.reset else None,
        "n_random": n_random, "seed": seed, "result_path": str(result_path),
        "max_extra_latency": max_extra_latency,
    }))
    build_args = VERILATOR_FLAGS + ["--top-module", "tb_top", "--assert"] + (COV_FLAGS if coverage else [])
    sim_log, build_log = build_dir / "sim.log", build_dir / "build.log"

    last = ""
    for _ in range(retries + 1):
        result_path.unlink(missing_ok=True)
        try:
            runner = get_runner("verilator")
            runner.build(sources=sources, hdl_toplevel="tb_top", build_dir=build_dir / "sim",
                         build_args=build_args, timescale=("1ns", "1ps"), always=True,
                         log_file=build_log)
        except BaseException as exc:      # noqa: BLE001 - runner raises SystemExit/CalledProcessError
            log = _tail(build_log)
            if re.search(r"%Error|error:", log):
                raise BenchError("build", log or str(exc)) from exc
            last = f"build crashed: {exc}\n{log}"
            continue
        try:
            runner.test(hdl_toplevel="tb_top", test_module="ppa_opt.verify.cocotb_test",
                        build_dir=build_dir / "sim", test_dir=build_dir, log_file=sim_log,
                        extra_env={"PPA_VERIFY_CFG": str(cfg_path)})
        except BaseException as exc:      # noqa: BLE001
            last = str(exc)
        log = _tail(sim_log, 4000)
        assertion = re.search(r"(%Error: .*Assertion failed.*)", log)
        if assertion:      # Verilator aborts the run on a failing assertion
            return SimResult("fail", "assertion", 0, seed=seed, message=assertion.group(1)[:500])
        if result_path.exists():
            res = json.loads(result_path.read_text())
            if res["status"] in ("pass", "fail"):
                cov = parse_coverage(build_dir / "coverage.dat", rtl_names) if coverage else {}
                return SimResult(res["status"], "mismatch" if res["status"] == "fail" else None,
                                 res["cycles"], res["mismatch"], res["cover"], cov, seed, res["detail"],
                                 res.get("latency"))
            raise BenchError("suite", res["detail"] or "suite raised an error")
        last = last or "simulator exited without a result"
    raise BenchError("tool_crash", f"simulator failed after {retries + 1} attempt(s): {last}\n"
                     + _tail(sim_log))
