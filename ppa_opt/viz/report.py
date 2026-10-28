"""Self-contained HTML report of iteration runs: every candidate per round, and how the best
design's area, power, critical-path delay and added latency change over the rounds.

Reads work/iterations/<design>/history.json (written by ppa_opt.loop.iterate). All numbers
shown are copied from those files, which in turn hold tool-report values; nothing is computed
here except percentage changes and the target clock (min period + setup slack).
"""
from __future__ import annotations

import json
import webbrowser
from pathlib import Path

from ..synth.openroad_flow import PROJECT_ROOT

ITER_ROOT = PROJECT_ROOT / "work" / "iterations"
TEMPLATE = Path(__file__).with_name("template.html")


def _row(rnd: int, phase: str, e: dict) -> dict:
    m, d = e.get("metrics"), e.get("derived") or {}
    row = {
        "round": rnd, "phase": phase, "name": e["name"], "source": e["source"],
        "strategy": e.get("strategy", ""), "overrides": e.get("overrides") or {},
        "latency": e.get("latency_extra", 0) or 0,
        "measured": m is not None, "error": e.get("error"), "gate": e.get("gate"),
    }
    if m:
        slack = m["setup_wns_ns"]
        period = d.get("min_period_ns")
        row.update({
            "area": m["area_um2"], "power_mw": m["power_total_w"] * 1e3, "slack": slack,
            "period": period, "met": bool(d.get("timing_met")),
            "target": round(period + slack, 3) if period is not None else None,
        })
    return row


def build_run(name: str, history: list[dict], best: dict | None = None) -> dict:
    """Normalize one run: candidate rows, and the incumbent (best so far) after each round."""
    rows: list[dict] = []
    by_name: dict[str, dict] = {}
    for r in history:
        for e in r["entries"]:
            row = _row(r["round"], r["phase"], e)
            rows.append(row)
            if row["measured"]:
                by_name[row["name"]] = row
    for row in rows:
        row["incumbent"] = False
    rounds, incumbents = [], []
    for r in history:
        inc = by_name.get(r["best"])
        if inc is not None:
            inc["incumbent"] = True
        incumbents.append({"round": r["round"], "name": r["best"], **{
            k: inc[k] for k in ("area", "power_mw", "slack", "period", "met", "latency", "target", "source", "strategy")
        }} if inc else {"round": r["round"], "name": r["best"]})
        here = [x for x in rows if x["round"] == r["round"]]
        rounds.append({"round": r["round"], "phase": r["phase"], "best": r["best"],
                       "tested": sum(x["measured"] for x in here),
                       "rejected": sum(1 for x in here if not x["measured"])})
    targets = [x["target"] for x in rows if x.get("target") is not None]
    return {"name": name, "rows": rows, "incumbents": incumbents, "rounds": rounds,
            "target_ns": targets[0] if targets else None, "best": best or {}}


def load_runs(root: Path = ITER_ROOT) -> list[dict]:
    runs = []
    for h in sorted(Path(root).glob("*/history.json"), key=lambda p: p.stat().st_mtime):
        try:
            history = json.loads(h.read_text())
            bj = h.with_name("best.json")
            runs.append(build_run(h.parent.name, history, json.loads(bj.read_text()) if bj.exists() else None))
        except (OSError, ValueError, KeyError):
            continue            # a half-written or foreign file must not break the report
    return runs


def render(runs: list[dict], selected: str | None = None) -> str:
    data = json.dumps({"runs": runs, "selected": selected or (runs[-1]["name"] if runs else None)})
    data = data.replace("<", "\\u003c")          # keep the JSON from closing its own <script>
    return TEMPLATE.read_text().replace("__DATA__", data)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Build an HTML report of iteration runs")
    ap.add_argument("--design", help="run to show first (default: the most recent)")
    ap.add_argument("--root", default=str(ITER_ROOT))
    ap.add_argument("--out", default=str(PROJECT_ROOT / "work" / "report.html"))
    ap.add_argument("--open", action="store_true", help="open the report in the default browser")
    a = ap.parse_args()
    runs = load_runs(Path(a.root))
    if not runs:
        raise SystemExit(f"no runs found under {a.root} (run `make iterate` first)")
    if a.design and a.design not in [r["name"] for r in runs]:
        raise SystemExit(f"no run named '{a.design}'; found: {', '.join(r['name'] for r in runs)}")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(runs, a.design))
    print(f"wrote {out}  ({len(runs)} run(s): {', '.join(r['name'] for r in runs)})")
    if a.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
