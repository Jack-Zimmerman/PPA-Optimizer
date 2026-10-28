"""Resolved-spec parsing. A resolved spec is markdown with a `Top module: \\`name\\`` line and a
port table (`| Port | Dir | Width | ... |`). Loose specs are expanded into this form once by the
`spec-resolver` agent, so the RTL and the suite are written from the same, explicit port list."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .suite import Suite


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class SpecPort:
    name: str
    direction: str
    width: int


def parse_spec(text: str) -> tuple[str, list[SpecPort]]:
    top = re.search(r"top module:?\**\s*`?(\w+)`?", text, re.I)
    if not top:
        raise SpecError("no 'Top module: `name`' line")
    ports: list[SpecPort] = []
    in_table = False
    for line in text.splitlines():
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if line.strip().startswith("|") and len(cells) >= 3 and cells[0].lower() == "port":
            in_table = True
            continue
        if in_table:
            if not line.strip().startswith("|"):
                break
            if set(cells[0]) <= set("-: "):
                continue
            d = cells[1].lower()
            d = {"in": "input", "out": "output"}.get(d, d)
            if d not in ("input", "output") or not cells[2].isdigit() or int(cells[2]) < 1:
                raise SpecError(f"bad port row: {line.strip()}")
            ports.append(SpecPort(cells[0], d, int(cells[2])))
    if not ports:
        raise SpecError("no port table (| Port | Dir | Width | ... |)")
    names = [p.name for p in ports]
    if len(set(names)) != len(names):
        raise SpecError("duplicate port names in the table")
    return top.group(1), ports


def check_suite_against_spec(suite: Suite, spec_path: Path) -> list[str]:
    """Differences between the spec's port table and the suite's PORTS (empty = agree)."""
    top, ports = parse_spec(Path(spec_path).read_text())
    out = [] if top == suite.top else [f"top: spec '{top}', suite '{suite.top}'"]
    want = {p.name: (p.direction, p.width) for p in ports}
    have = suite.ports
    out += [f"suite lacks port '{n}'" for n in sorted(want.keys() - have.keys())]
    out += [f"suite has port '{n}' not in spec" for n in sorted(have.keys() - want.keys())]
    out += [f"port '{n}': spec {want[n]}, suite {have[n]}" for n in sorted(want.keys() & have.keys())
            if want[n] != tuple(have[n])]
    return out


def main() -> None:
    import argparse
    from .suite import load_suite

    ap = argparse.ArgumentParser(description="Check a resolved spec (and optionally a suite against it)")
    ap.add_argument("spec")
    ap.add_argument("--suite")
    a = ap.parse_args()
    try:
        top, ports = parse_spec(Path(a.spec).read_text())
    except SpecError as e:
        raise SystemExit(f"spec invalid: {e}")
    print(f"spec ok: {top}, {len(ports)} ports")
    if a.suite:
        problems = check_suite_against_spec(load_suite(a.suite), Path(a.spec))
        for p in problems:
            print("  -", p)
        raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
