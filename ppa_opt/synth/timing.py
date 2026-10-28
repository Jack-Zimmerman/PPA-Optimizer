"""Read the worst setup path out of OpenROAD's `6_finish.rpt` (OpenSTA text).

Everything returned is copied from the report; nothing is estimated. The summary is what the
iteration loop shows the RTL author: which flops the path joins and which cells it passes through.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REPORT_NAME = "6_finish.rpt"
HEADER = "report_checks -path_delay max"
# Output-pin rows start with fanout, cap, slew, delay, time, edge, pin, (cell type).
_STAGE = re.compile(
    r"^\s*(\d+)\s+([\d.]+)\s+([\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+([\^v])\s+(\S+)\s+\((\S+)\)\s*$")
# Input-pin rows have no fanout/cap columns: slew, delay, time, edge, pin, (cell type).
_PIN = re.compile(r"^\s+([\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+([\^v])\s+(\S+)\s+\((\S+)\)\s*$")
_SLACK = re.compile(r"^\s*(-?[\d.]+)\s+slack \((\w+)\)")
_TIME = re.compile(r"^\s*(-?[\d.]+)\s+(data arrival time|data required time)")


def report_path(run_dir: str | Path, pdk: str, top: str) -> Path:
    return Path(run_dir) / "run" / "reports" / pdk / top / "base" / REPORT_NAME


def _short(cell: str) -> str:
    return cell.split("__", 1)[-1]


def parse_worst_setup_path(text: str) -> dict | None:
    """First `report_checks -path_delay max` block, or None if the report has none."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == f"finish {HEADER}"), None)
    if start is None:
        return None
    begin = next((i for i in range(start, len(lines)) if lines[i].startswith("Startpoint:")), None)
    if begin is None:
        return None
    startpoint = lines[begin].split()[1]
    endpoint = next((l.split()[1] for l in lines[begin:begin + 4] if l.startswith("Endpoint:")), "?")
    launch_inst = startpoint

    stages, arrival, required, slack, status = [], None, None, None, None
    in_data = False
    for line in lines[begin:]:
        if line.startswith("====") and stages:
            break
        pin_row = _PIN.match(line)
        if pin_row:
            if pin_row.group(5) == f"{launch_inst}/CLK":
                in_data = True              # everything before this is the launch clock tree
            continue
        m = _STAGE.match(line)
        if m:
            fanout, cap, slew, delay, t, edge, pin, cell = m.groups()
            if in_data and arrival is None:
                stages.append({"pin": pin, "cell": _short(cell), "fanout": int(fanout),
                               "delay_ns": float(delay), "time_ns": float(t)})
            continue
        t = _TIME.match(line)
        if t:
            if "arrival" in t.group(2) and arrival is None:
                arrival = float(t.group(1))
            elif "required" in t.group(2):
                required = float(t.group(1))
            continue
        s = _SLACK.match(line)
        if s:
            slack, status = float(s.group(1)), s.group(2)
            break
    if slack is None or not stages:
        return None
    combinational = stages[1:]              # stages[0] is the launching flop's clk->Q
    return {
        "startpoint": startpoint, "endpoint": endpoint,
        "slack_ns": slack, "status": status,
        "arrival_ns": arrival, "required_ns": required,
        "clk_to_q_ns": stages[0]["delay_ns"],
        "logic_depth": len(combinational),
        "cells": dict(Counter(s["cell"] for s in combinational).most_common()),
        "slowest_stages": sorted(combinational, key=lambda s: -s["delay_ns"])[:6],
        "stages": stages,
    }


def read_critical_path(run_dir: str | Path, pdk: str, top: str) -> dict | None:
    """Parse the run's report; adds `provenance`. None if the report is absent or unparseable."""
    path = report_path(run_dir, pdk, top)
    if not path.exists():
        return None
    out = parse_worst_setup_path(path.read_text(errors="replace"))
    if out:
        out["provenance"] = f"run/reports/{pdk}/{top}/base/{REPORT_NAME}:{HEADER}"
    return out


def describe(cp: dict) -> str:
    """Plain-text summary for an RTL author. All numbers come from the report."""
    cells = ", ".join(f"{n}x {c}" for c, n in cp["cells"].items())
    slow = "; ".join(f"{s['cell']} {s['delay_ns']:.2f} ns (fanout {s['fanout']})" for s in cp["slowest_stages"])
    return (f"Worst setup path: {cp['startpoint']} -> {cp['endpoint']}, slack {cp['slack_ns']:+.2f} ns "
            f"({cp['status']}), arrival {cp['arrival_ns']} ns vs required {cp['required_ns']} ns.\n"
            f"Clk->Q {cp['clk_to_q_ns']:.2f} ns, then {cp['logic_depth']} cells: {cells}.\n"
            f"Slowest stages: {slow}.")
