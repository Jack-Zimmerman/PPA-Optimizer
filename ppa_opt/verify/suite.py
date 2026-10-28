"""The test-suite contract. A suite is a directory written from the spec alone:

    suites/<name>/suite.py        required   PORTS, CLOCK, RESET, Model, COVER (+ optional hooks)
    suites/<name>/assertions.sv   optional   module `<top>_sva` with the DUT's ports, all as inputs

suite.py must define (see ppa_opt/verify/SUITE_FORMAT.md for the full contract):

    TOP      str                     top module name from the spec
    PORTS    {name: ("input"|"output", width)}   every port of the top module, clock and reset included
    CLOCK    str | None              clock port; None for a purely combinational design
    RESET    (name, active_low) | None
    class Model:                     cycle-accurate reference model
        def step(self, inp: dict) -> dict      # see below
    COVER    {point_name: fn(inp, out) -> bool}  functional cover points from the spec

    optional: DIRECTED() -> iterable of input dicts, RANDOM(rng) -> input dict, N_RANDOM: int

`Model.step(inp)` is called once per clock cycle with the pin values applied before the edge
(every input except the clock, reset pin included). It returns the value each output must
have right after that edge (before the edge for a design with no clock). A value of None
means "unspecified, don't check", which is how a model expresses power-up state before reset.
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


class SuiteError(ValueError):
    """The suite itself is malformed (a suite defect, never a candidate defect)."""


@dataclass
class Suite:
    path: Path
    module: ModuleType

    @property
    def top(self) -> str:
        return self.module.TOP

    @property
    def ports(self) -> dict[str, tuple[str, int]]:
        return dict(self.module.PORTS)

    @property
    def clock(self) -> str | None:
        return self.module.CLOCK

    @property
    def reset(self) -> tuple[str, bool] | None:
        return self.module.RESET

    @property
    def sva(self) -> Path | None:
        p = self.path / "assertions.sv"
        return p if p.exists() else None

    @property
    def data_inputs(self) -> dict[str, int]:
        skip = {self.clock, self.reset[0] if self.reset else None}
        return {n: w for n, (d, w) in self.ports.items() if d == "input" and n not in skip}

    @property
    def outputs(self) -> dict[str, int]:
        return {n: w for n, (d, w) in self.ports.items() if d == "output"}


def load_module(path: Path, name: str = "ppa_suite") -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path / "suite.py")
    if spec is None or spec.loader is None:
        raise SuiteError(f"{path}/suite.py not found")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_suite(path: str | Path) -> Suite:
    path = Path(path).resolve()
    try:
        mod = load_module(path)
    except SuiteError:
        raise
    except Exception as e:  # noqa: BLE001 - any import-time failure is a suite defect
        raise SuiteError(f"suite.py failed to import: {e!r}") from e
    problems = []
    for attr in ("TOP", "PORTS", "CLOCK", "RESET", "Model", "COVER"):
        if not hasattr(mod, attr):
            problems.append(f"missing {attr}")
    if problems:
        raise SuiteError("; ".join(problems))
    for n, v in mod.PORTS.items():
        if (not isinstance(v, (tuple, list)) or len(v) != 2 or v[0] not in ("input", "output")
                or not isinstance(v[1], int) or v[1] < 1):
            raise SuiteError(f"PORTS['{n}'] must be ('input'|'output', width>=1), got {v!r}")
    if mod.CLOCK is not None and mod.PORTS.get(mod.CLOCK, ("", 0))[0] != "input":
        raise SuiteError(f"CLOCK '{mod.CLOCK}' is not an input port")
    if mod.RESET is not None and mod.PORTS.get(mod.RESET[0], ("", 0))[0] != "input":
        raise SuiteError(f"RESET '{mod.RESET[0]}' is not an input port")
    if not mod.COVER:
        raise SuiteError("COVER is empty: the spec must yield functional cover points")
    return Suite(path, mod)


def check_ports(suite: Suite, rtl_ports: list) -> list[str]:
    """Differences between the spec's port list and the RTL's; empty means they agree."""
    want = {n: (d, w) for n, (d, w) in suite.ports.items()}
    have = {p.name: (p.direction, p.width) for p in rtl_ports}
    out = []
    for n in sorted(want.keys() - have.keys()):
        out.append(f"missing port '{n}' ({want[n][0]}, {want[n][1]} bit)")
    for n in sorted(have.keys() - want.keys()):
        out.append(f"unexpected port '{n}'")
    for n in sorted(want.keys() & have.keys()):
        if want[n] != have[n]:
            out.append(f"port '{n}': spec {want[n]}, RTL {have[n]}")
    return out
