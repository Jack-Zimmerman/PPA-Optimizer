"""Read the top-level ports of RTL (via Verilator's JSON dump, no C++ build)."""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

VERILATOR_FLAGS = ["-Wno-fatal", "-Wno-lint", "-Wno-style", "-Wno-DECLFILENAME"]


class InterfaceError(RuntimeError):
    """The design's ports could not be read."""


@dataclass(frozen=True)
class Port:
    name: str
    direction: str   # "input" | "output"
    width: int


def _range_width(rng: str | None) -> int:
    if not rng:
        return 1
    hi, lo = (int(x) for x in rng.split(":"))
    return abs(hi - lo) + 1


def extract_ports(files: list[Path], top: str) -> list[Port]:
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            ["verilator", "--json-only", "--top-module", top, "--Mdir", tmp, *VERILATOR_FLAGS,
             *map(str, files)],
            capture_output=True, text=True)
        trees = sorted(Path(tmp).glob("*.tree.json"))
        if proc.returncode != 0 or not trees:
            raise InterfaceError(f"verilator could not elaborate {top}:\n{proc.stderr[-1500:]}")
        tree = json.loads(trees[0].read_text())

    nodes: dict[str, dict] = {}

    def index(n) -> None:
        if isinstance(n, dict):
            if "addr" in n and "type" in n:
                nodes[n["addr"]] = n
            for v in n.values():
                index(v)
        elif isinstance(n, list):
            for v in n:
                index(v)

    index(tree)
    mod = next((n for n in nodes.values() if n["type"] == "MODULE" and n.get("name") == top), None)
    if mod is None:
        raise InterfaceError(f"top module '{top}' not found")

    # Only the module's own port declarations: the direct children of the module. Walking deeper would pick
    # up the arguments and return values of functions/tasks (they are also INPUT/OUTPUT variables) and
    # wrongly report them as ports.
    ports: list[Port] = []
    for n in mod.get("stmtsp", []):
        if isinstance(n, dict) and n.get("type") == "VAR" and n.get("direction") in ("INPUT", "OUTPUT"):
            dt = nodes.get(n.get("dtypep", ""))
            if dt is None or dt["type"] != "BASICDTYPE":
                raise InterfaceError(
                    f"port '{n['name']}' has an unsupported type ({dt['type'] if dt else '?'}); "
                    "use plain logic/wire vectors on the top module")
            ports.append(Port(n["name"], n["direction"].lower(), _range_width(dt.get("range"))))
    if not ports:
        raise InterfaceError(f"top module '{top}' has no ports")
    return ports
