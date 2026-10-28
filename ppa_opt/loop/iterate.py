"""Iteration loop: drive setup slack to >= 0 at the target clock, then minimize area.

Each round asks the RTL author for N rewrites of the current best design (shown the worst path
from the tool report), adds a few flow-knob variants of the same RTL, gates the rewrites against
the qualified suite, measures the survivors with the OpenROAD flow, and keeps the best.

All numbers come from tool reports. Rewrites must stay cycle-exact (the suite enforces it), so
only combinational restructuring is possible; when that cannot reach the target the loop stops
and says so instead of changing the spec.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..llm.devagents import AgentError, find_spec, run_agent
from ..synth.openroad_flow import PROJECT_ROOT, FlowConfig, FlowResult
from ..synth.parallel import Candidate, Outcome, default_workers, run_many
from ..synth.timing import describe, read_critical_path
from ..verify.gate import GateResult, verify_many
from ..verify.suite import Suite, load_suite

ITER_ROOT = PROJECT_ROOT / "work" / "iterations"
AREA_GAIN_MIN = 0.02          # a round that improves area by less than this counts as stalled
PATIENCE = 2                  # stalled rounds in a row before stopping
SLOW_GAIN_FRACTION = 0.25     # a timing round that closes less than this share of the remaining violation counts as slow
AUTO_ROUND_CAP = 30           # safety net for iters=None ("until converged"); the stop rules normally end a run long before

TIMING_HINTS = [
    "Shorten the critical path by balancing long chains of adders/OR/AND into trees.",
    "Replace ripple-style carry chains on the path with a prefix (Kogge-Stone/Brent-Kung) or carry-select structure.",
    "Flatten priority/mux chains on the path into a one-hot select with a parallel AND-OR.",
    "Precompute values that depend only on early-arriving inputs so less logic sits after the late signal.",
    "Duplicate high-fanout nets or logic on the path so each copy drives fewer loads.",
    "Reduce logic depth by moving decode and special-case detection off the critical path (compute them in parallel and select at the end).",
]
PIPELINE_HINTS = [
    "Cut the critical path into balanced halves with {L} pipeline stage(s) of registers placed across the middle of the long chain.",
    "Register the operands/early decode up front and finish the long arithmetic in the following stage(s); add {L} stage(s) in total.",
    "Split the long arithmetic into two or more phases and register the partial result between them; add {L} stage(s) in total.",
]
AREA_HINTS = [
    "Share operators that are never needed in the same cycle (one adder/shifter feeding several uses).",
    "Remove redundant logic: narrow intermediate widths to what the spec needs and drop unused bits.",
    "Replace wide parallel structures with smaller equivalents wherever the path has slack to spare.",
    "Simplify special-case handling using the spec's don't-care cases, without changing any specified output.",
]
KNOBS_TIMING = [("slack_margin_0.2", {"setup_slack_margin": 0.2}),
                ("timing_driven_place", {"gpl_timing_driven": True}),
                ("place_density_0.1", {"place_density_addon": 0.10})]
KNOBS_AREA = [("abc_area", {"abc_area": True}),
              ("place_density_0.3", {"place_density_addon": 0.30})]


_BLOCK = re.compile(r"^\s*(always_comb|always_ff|always\b|assign\b|generate\b|function\b)")
_HEADER_KEYS = ("strategy", "what changed", "why it helps", "equivalence", "latency", "tradeoffs")


_DECL = re.compile(r"^\s*(logic|wire|reg|bit|int|integer|localparam|parameter|typedef|enum|struct)\b")


def _documented_above(lines: list[str], i: int) -> bool:
    """Is the block at lines[i] covered by a comment above it? The comment may sit above a group of
    signal declarations or `assign` lines that belong to the same section, so those are skipped."""
    j = i - 1
    while j >= 0:
        t = lines[j].strip()
        if t == "" or _DECL.match(lines[j]) or t.startswith("assign"):
            j -= 1
            continue
        return t.startswith("//") or t.endswith("*/")
    return False


def comment_check(text: str) -> list[str]:
    """Documentation problems in a candidate (empty = documented). Informational: a poorly commented
    design is still a correct design, so this never rejects a candidate."""
    lines = text.splitlines()
    head = []
    for line in lines:
        if line.strip() == "" and not head:
            continue
        if line.strip().startswith("//"):
            head.append(line.strip().lower())
        else:
            break
    out = []
    if len(head) < 4:
        out.append("no header comment block at the top of the file")
    else:
        missing = [k for k in _HEADER_KEYS if not any(k in h for h in head)]
        if missing:
            out.append("header lacks: " + ", ".join(missing))
    blocks = bare = 0
    for i, line in enumerate(lines):
        if not _BLOCK.match(line):
            continue
        blocks += 1
        if "//" not in line and not _documented_above(lines, i):
            bare += 1
    if bare:
        out.append(f"{bare} of {blocks} logic blocks have no comment")
    return out


@dataclass
class Entry:
    name: str
    rtl: Path
    source: str                   # baseline | rewrite | knob
    strategy: str
    overrides: dict = field(default_factory=dict)
    latency_extra: int = 0        # extra output cycles vs the spec (only possible when MAXLAT > 0)
    gate: GateResult | None = None
    result: FlowResult | None = None
    error: str | None = None
    doc_notes: list = field(default_factory=list)     # documentation problems (informational)
    author_failed: bool = False       # the agent failed and wrote no file (not a verdict on any design)
    author_error: str = ""            # the agent's own error text, kept even when its file was still used

    @property
    def measured(self) -> bool:
        return self.result is not None


def _metrics(e: Entry) -> tuple[bool, float, float]:
    r = e.result
    return (bool(r.derived.get("timing_met")), r.metrics["setup_wns_ns"], r.metrics["area_um2"])


def better(a: Entry, b: Entry | None) -> bool:
    """Timing met beats not met. Both met: less added latency, then smaller area.
    Both violating: larger (less negative) slack."""
    if b is None:
        return True
    (am, aw, aa), (bm, bw, ba) = _metrics(a), _metrics(b)
    if am != bm:
        return am
    if am:
        if a.latency_extra != b.latency_extra:
            return a.latency_extra < b.latency_extra
        return aa < ba - 1e-9
    return aw > bw + 1e-9


def critical_path_of(e: Entry) -> dict | None:
    r = e.result
    if r.critical_path:
        return r.critical_path
    if Path(r.run_dir).exists():
        return read_critical_path(r.run_dir, r.config["pdk"], r.config["top"])
    return None


def _prompt(e: Entry, out: Path, spec: Path, top: str, hint: str, phase: str, clock_ns: float,
            pipeline: int = 0, keep_latency: int = 0) -> str:
    m = e.result.metrics
    cp = critical_path_of(e)
    goal = (f"Setup timing is VIOLATED at the {clock_ns} ns target (worst slack {m['setup_wns_ns']:+.3f} ns). "
            "Goal: make the design meet timing at that clock."
            if phase == "timing" else
            f"Timing is met at {clock_ns} ns (slack {m['setup_wns_ns']:+.3f} ns). Goal: reduce area "
            f"(currently {m['area_um2']:.1f} um^2) without letting slack go below 0.")
    return (f"Rewrite mode. Spec: {spec}. Current RTL (read it, do not edit it): {e.rtl}. Write your new RTL to "
            f"{out} (top module `{top}`, identical ports).\n{goal}\n"
            + (f"Tool report: {describe(cp)}\n" if cp else "")
            + f"Strategy for this attempt: {hint}\n"
            + (f"PIPELINE MODE: you may add exactly {pipeline} pipeline stage(s). The outputs must equal the "
               f"spec's outputs delayed by exactly {pipeline} cycle(s) (same stream, later), the ports stay "
               "identical, and the function must not change. Datapath stage registers need no reset, but the "
               "output registers must keep the spec's reset behaviour.\n" if pipeline else
               (f"The current design already has {keep_latency} extra pipeline stage(s) versus the spec. Keep exactly "
                f"that output latency ({keep_latency} cycle(s) later than the spec): do not add or remove stages.\n"
                if keep_latency else
                "Stay cycle-exact: same latency and reset behaviour, no added or removed registers.\n"))
            + "Documentation is required: start the file with the header comment block described in your "
              "instructions (Strategy, What changed, Why it helps, Equivalence, Latency, Tradeoffs), comment every "
              "always/assign/generate/function/case block, and mark changed logic with `// OPT:`.\n"
            + "Do not read suites/.")


Author = Callable[[str], str]
Gate = Callable[..., list[GateResult]]       # (suite, candidates, max_extra_latency=0) -> results
Measure = Callable[[list[Candidate], FlowConfig], list[Outcome]]


def _default_gate(suite: Suite, cands: list[Candidate], max_extra_latency: int = 0) -> list[GateResult]:
    return verify_many(suite, cands, workers=4, max_extra_latency=max_extra_latency)


def _load_prior(out: Path) -> tuple[list[dict], dict] | None:
    """The previous run saved in `out` (history + best.json), or None when the folder has none."""
    hist_file, best_sv = out / "history.json", out / "best.sv"
    if not (hist_file.exists() and best_sv.exists()):
        return None
    try:
        hist = json.loads(hist_file.read_text())
        bj = out / "best.json"
        best = json.loads(bj.read_text()) if bj.exists() else {}
    except ValueError:
        return None
    return (hist, best) if isinstance(hist, list) and hist else None


def iterate(spec: Path, suite_dir: Path, rtl: Path | None, *, clock_ns: float, iters: int | None = 5, n: int = 4,
            n_knob: int = 2, pdk: str = "sky130hd", name: str | None = None, workers: int | None = None,
            budget_min: float | None = None, max_extra_latency: int = 0, pipeline_now: bool = False,
            fresh: bool = False, out_root: Path = ITER_ROOT, author: Author | None = None,
            gate: Gate | None = None, measure: Measure | None = None, log: Callable[[str], None] = print,
            ) -> tuple[Entry, list[dict], str]:
    """Returns (best entry, per-round history, stop reason).

    `iters` is the number of rounds to run; `None` means "until converged": no fixed count, only the stop
    rules below (plus `budget_min` and a safety cap of AUTO_ROUND_CAP rounds).

    The run folder `out_root/<name>` is the memory of the run. If it already holds a history, this
    continues from that run's best design, numbers new rounds after the last saved one and appends to
    the same history; `rtl` is only needed for a new run. `fresh=True` moves the old folder aside
    (never deletes it) and starts over from `rtl`."""
    spec = Path(spec)
    suite = load_suite(suite_dir)
    top = suite.top
    name = name or top
    out = out_root / name
    prior = _load_prior(out)
    if prior and fresh:
        aside = out.with_name(f"{name}.old-{time.strftime('%Y%m%d-%H%M%S')}")
        out.rename(aside)
        log(f"moved the previous run to {aside}")
        prior = None
    if prior is None and rtl is None:
        raise AgentError(f"{out} has no previous run to continue, so the starting RTL is required (RTL=...)")
    if prior:
        prev_clock = prior[1].get("clock_ns")
        if prev_clock is not None and abs(prev_clock - clock_ns) > 1e-9:
            raise AgentError(f"{out} was run at a {prev_clock} ns clock but this run asks for {clock_ns} ns; "
                             f"use CLOCK={prev_clock} to continue it, or FRESH=1 to start over (the old run is kept)")
        if rtl is not None:
            log(f"note: continuing from the best design saved in {out}; the RTL you passed is ignored "
                "(pass FRESH=1 to start over from it)")
    out.mkdir(parents=True, exist_ok=True)
    author = author or (lambda prompt: run_agent("rtl-author", prompt, deny_read=("./suites/**",)))
    gate = gate or _default_gate
    workers = workers or default_workers()
    cfg = FlowConfig(top=top, clock_ns=clock_ns, pdk=pdk)
    measure = measure or (lambda cands, c: run_many(cands, c, workers=workers))
    deadline = time.time() + budget_min * 60 if budget_min else None

    # ---- start: the design to improve must pass the suite, then it is measured ------------------
    if prior:
        history = prior[0]
        k0 = max(r["round"] for r in history)
        best_name = history[-1]["best"]
        prev_lat = prior[1].get("latency_extra")
        if prev_lat is None:    # runs saved before best.json existed
            prev_lat = next((e.get("latency_extra", 0) for r in history for e in r["entries"]
                             if e["name"] == best_name), 0)
        max_extra_latency = max(max_extra_latency, prev_lat)
        base_dir = out / f"iter_{k0 + 1}"
        base_dir.mkdir(exist_ok=True)
        base_rtl = base_dir / f"{top}_start.sv"
        shutil.copyfile(out / "best.sv", base_rtl)
        first_round = k0 + 1
        g = gate(suite, [Candidate("baseline", [base_rtl], top)], max_extra_latency=prev_lat)[0]
        label, how = best_name, "continued from the previous best"
    else:
        history = []
        base_dir = out / "iter_0"
        base_dir.mkdir(exist_ok=True)
        base_rtl = base_dir / f"{top}.sv"
        shutil.copyfile(rtl, base_rtl)
        first_round = 1
        g = gate(suite, [Candidate("baseline", [base_rtl], top)])[0]
        label, how = "baseline", "starting design"
    if not g.ok:
        raise AgentError(f"the starting RTL fails the suite, fix it before iterating: {g.summary()}")
    best = Entry(label, base_rtl, "baseline", how, gate=g, latency_extra=g.latency_extra)
    o = measure([Candidate("baseline", [base_rtl], top)], cfg)[0]
    if not o.ok:
        raise AgentError(f"starting design did not complete the flow: {o.error}")
    best.result = o.result
    seen = {(base_rtl.read_text(), json.dumps({}, sort_keys=True))}
    for r in history:           # designs already tried in earlier rounds are not worth measuring again
        for e in r["entries"]:
            f = Path(e.get("rtl", ""))
            if e["source"] == "rewrite" and f.is_file():
                seen.add((f.read_text(), "{}"))
    if prior:
        log(f"continuing {name}: round {k0} ended with best {best_name}; starting at round {first_round}")
    else:
        history.append(_round_record(0, [best], best, "baseline", False))
    log(_round_table(0, [best], best))
    if critical_path_of(best) is None:
        log("note: no critical-path report available for the starting design (old cache entry whose run "
            "directory is gone); the author will only see metrics. Re-run with `make clean-cache` for details.")

    stalled = rejected_rounds = 0
    # extra pipeline stages rewrites may add; normally rises only when cycle-exact rewrites stall
    allowance = max(best.latency_extra, max_extra_latency if pipeline_now else 0)
    n_rounds = AUTO_ROUND_CAP if iters is None else iters
    reason = (f"reached the safety cap of {AUTO_ROUND_CAP} rounds without converging" if iters is None
              else f"reached {iters} iterations")
    for k in range(first_round, first_round + n_rounds):
        phase = "area" if best.result.derived.get("timing_met") else "timing"
        if deadline and time.time() > deadline:
            reason = "wall-clock budget used up"
            break
        it_dir = out / f"iter_{k}"
        it_dir.mkdir(exist_ok=True)
        hints = TIMING_HINTS if phase == "timing" else AREA_HINTS
        # Once timing is met the latency is fixed at the best design's; before that it is the allowance.
        allowed = best.latency_extra if phase == "area" else allowance
        n_pipe = max(1, n // 2) if (phase == "timing" and allowance > 0) else 0
        log(f"\n== round {k}: phase '{phase}', best so far slack {best.result.metrics['setup_wns_ns']:+.3f} ns, "
            f"area {best.result.metrics['area_um2']:.1f} um^2"
            + (f", extra latency {best.latency_extra}" if best.latency_extra else "")
            + (f", pipelining up to +{allowance} stage(s) allowed" if phase == "timing" and allowance else ""))

        # ---- rewrites from the RTL author (parallel) --------------------------------------------
        entries: list[Entry] = []

        def write(i: int) -> Entry:
            piped = i < n_pipe
            hint = (PIPELINE_HINTS[((k - 1) * n_pipe + i) % len(PIPELINE_HINTS)].format(L=allowance) if piped
                    else hints[((k - 1) * n + i) % len(hints)])
            path = it_dir / f"cand{i}.sv"
            e = Entry(f"r{k}c{i}", path, "rewrite", hint)
            path.unlink(missing_ok=True)       # never measure a stale file left by an interrupted earlier round
            try:
                author(_prompt(best, path, spec, top, hint, phase, clock_ns,
                               pipeline=allowance if piped else 0,
                               keep_latency=best.latency_extra if (phase == "area" or not piped) else 0))
            except AgentError as exc:
                e.author_error = str(exc)[:1500]
            if path.exists():
                # Even if the agent exited with an error, a file it wrote is judged by the suite like any other.
                e.doc_notes = comment_check(path.read_text())
            elif e.author_error:
                e.author_failed = True
                e.error = "author failed: " + e.author_error.splitlines()[0][:160]
            else:
                e.error = "author wrote no file"
            return e

        with ThreadPoolExecutor(max_workers=max(1, n)) as pool:
            entries += list(pool.map(write, range(n)))
        for e in entries:
            if e.author_error and not e.author_failed:
                log(f"note: the author exited with an error for {e.name} but wrote its file; the suite will judge it "
                    f"({e.author_error.splitlines()[0][:120]})")
        if entries and all(e.author_failed for e in entries):
            reason = ("the rtl-author agent failed on every call, so no designs were produced (a usage or rate limit, "
                      "or a network problem, is the usual cause). Nothing was lost: run the same command again to "
                      "continue from this folder. Agent error: " + entries[0].author_error.splitlines()[0][:200])
            log(f"\n{reason}")
            break

        # ---- flow-knob variants of the best RTL (already verified, no re-gating) -----------------
        for kname, over in (KNOBS_TIMING if phase == "timing" else KNOBS_AREA)[:n_knob]:
            key = (best.rtl.read_text(), json.dumps({**best.overrides, **over}, sort_keys=True))
            if key not in seen:
                entries.append(Entry(f"r{k}-{kname}", best.rtl, "knob", kname, {**best.overrides, **over},
                                     latency_extra=best.latency_extra))
                seen.add(key)

        for e in entries:
            if e.doc_notes:
                log(f"note: {e.name} is under-documented: " + "; ".join(e.doc_notes))

        # ---- gate the rewrites -------------------------------------------------------------------
        rw = [e for e in entries if e.source == "rewrite" and e.error is None]
        verdicts = gate(suite, [Candidate(e.name, [e.rtl], top) for e in rw], max_extra_latency=allowed) if rw else []
        for e, g in zip(rw, verdicts):
            e.gate = g
            e.latency_extra = g.latency_extra
            if g.ok and g.latency_extra > allowed:
                g.status, g.failure_class = "fail", "mismatch"
            if not g.ok:
                e.error = f"gate: {g.summary()}"
            else:
                key = (e.rtl.read_text(), "{}")
                if key in seen:
                    e.error = "duplicate of an earlier design"
                seen.add(key)

        # ---- measure what is left ----------------------------------------------------------------
        todo = [e for e in entries if e.error is None]
        if todo:
            # overrides are applied on top of the baseline cfg for every entry
            outcomes = measure([Candidate(e.name, [e.rtl], top, e.overrides or None) for e in todo], cfg)
            for e, oc in zip(todo, outcomes):
                if oc.ok:
                    e.result = oc.result
                else:
                    e.error = f"flow: [{oc.error.stage}] {oc.error}"

        # ---- choose (better() is a total order, so scan order does not matter) -------------------
        before = best
        for e in entries:
            if e.measured and better(e, best):
                best = e
        improved = best is not before
        slack_before, slack_after = _metrics(before)[1], _metrics(best)[1]
        remaining = max(0.0, -slack_after)          # how far timing is still violated
        slow = improved and remaining > 0 and (slack_after - slack_before) < SLOW_GAIN_FRACTION * remaining
        if phase == "timing" and remaining > 0 and allowance < max_extra_latency and (not improved or slow):
            allowance += 1             # progress stalled or too slow to close the gap: allow one more pipeline stage
            stalled = 0
            why = "no timing improvement" if not improved else (
                f"the round closed only {slack_after - slack_before:.2f} of the {remaining + slack_after - slack_before:.2f} ns "
                f"violation (under {SLOW_GAIN_FRACTION:.0%} of what remained)")
            log(f"{why}; allowing up to +{allowance} pipeline stage(s) next round")
        elif phase == "timing":
            # entering the area phase counts as progress; otherwise a round must raise the slack
            stalled = 0 if improved else stalled + 1
        else:
            gain = (_metrics(before)[2] - _metrics(best)[2]) / _metrics(before)[2]
            stalled = 0 if gain >= AREA_GAIN_MIN else stalled + 1
        rewrites = [e for e in entries if e.source == "rewrite" and not e.author_failed]
        rejected_rounds = rejected_rounds + 1 if rewrites and all(e.error for e in rewrites) else 0
        history.append(_round_record(k, entries, best, phase, improved))
        log(_round_table(k, entries, best))
        (out / "history.json").write_text(json.dumps(history, indent=2, default=str))

        if rejected_rounds >= PATIENCE:
            reason = "every rewrite was rejected (gate, flow or author) two rounds in a row"
            break
        if stalled >= PATIENCE:
            reason = ("timing met and area stopped improving" if best.result.derived.get("timing_met")
                      else f"no timing improvement for {PATIENCE} rounds")
            break

    shutil.copyfile(best.rtl, out / "best.sv")
    (out / "best.json").write_text(json.dumps({
        "name": best.name, "source": best.source, "latency_extra": best.latency_extra,
        "setup_wns_ns": best.result.metrics["setup_wns_ns"], "area_um2": best.result.metrics["area_um2"],
        "timing_met": bool(best.result.derived.get("timing_met")), "clock_ns": clock_ns,
        "provenance": best.result.provenance}, indent=2))
    (out / "history.json").write_text(json.dumps(history, indent=2, default=str))
    return best, history, reason


def _round_record(k: int, entries: list[Entry], best: Entry, phase: str, improved: bool) -> dict:
    return {
        "round": k, "phase": phase, "improved": improved,
        "best": best.name,
        "entries": [{
            "name": e.name, "source": e.source, "strategy": e.strategy, "overrides": e.overrides,
            "latency_extra": e.latency_extra, "doc_notes": e.doc_notes, "author_error": e.author_error,
            "rtl": str(e.rtl), "error": e.error,
            "gate": (e.gate.status if e.gate else None),
            "metrics": e.result.metrics if e.result else None,
            "derived": e.result.derived if e.result else None,
            "provenance": e.result.provenance if e.result else None,
        } for e in entries],
    }


def _round_table(k: int, entries: list[Entry], best: Entry) -> str:
    rows = [("candidate", "source", "+lat", "slack ns", "area um^2", "status")]
    for e in entries:
        if e.measured:
            m = e.result.metrics
            tag = ("met" if e.result.derived.get("timing_met") else "VIOLATED") + ("  <- best" if e is best else "")
            rows.append((e.name, e.source, str(e.latency_extra), f"{m['setup_wns_ns']:+.3f}",
                         f"{m['area_um2']:.1f}", tag))
        else:
            rows.append((e.name, e.source, "-", "-", "-", (e.error or "not measured")[:70]))
    w = [max(len(r[i]) for r in rows) for i in range(6)]
    return "\n".join("  ".join(c.ljust(w[i]) for i, c in enumerate(r)) for r in rows)


def final_report(best: Entry, history: list[dict], reason: str, clock_ns: float) -> str:
    m = best.result.metrics
    met = best.result.derived.get("timing_met")
    lines = [f"\nstopped: {reason}",
             f"best: {best.name} ({best.source}: {best.strategy})",
             f"  setup slack {m['setup_wns_ns']:+.3f} ns at {clock_ns} ns ({'MET' if met else 'VIOLATED'}), "
             f"area {m['area_um2']:.1f} um^2   [power is an estimate]"]
    if best.latency_extra:
        lines.append(f"WARNING: this design has {best.latency_extra} extra cycle(s) of output latency versus the "
                     "spec. It is not a drop-in replacement: whatever consumes the outputs must expect them later.")
    if not met:
        cp = critical_path_of(best)
        lines.append("target not reached by cycle-exact rewrites. Options: relax the clock, accept the best "
                     "slack above, or change the spec to allow extra latency (needs a new suite).")
        if cp:
            lines.append(describe(cp))
    return "\n".join(lines)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Iterate on RTL until timing is met, then reduce area")
    ap.add_argument("spec")
    ap.add_argument("--suite", required=True)
    ap.add_argument("--rtl", help="starting RTL; only needed for a new run, since a run folder that already "
                    "has a history is continued from its best design")
    ap.add_argument("--clock-ns", type=float, required=True)
    ap.add_argument("--iters", default="5", help="rounds to run, or 'auto' to keep going until converged "
                    "(stops on the usual rules: timing met and area gains under 2%% for two rounds, no slack "
                    "improvement for two rounds, every rewrite rejected, or --budget-min)")
    ap.add_argument("--n", type=int, default=4, help="rewrites per round")
    ap.add_argument("--knobs", type=int, default=2, help="flow-knob variants per round")
    ap.add_argument("--pdk", default="sky130hd")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--budget-min", type=float, help="stop starting new rounds after this many minutes")
    ap.add_argument("--pipeline-now", action="store_true",
                    help="allow the full --max-extra-latency from the first round instead of waiting for a stall")
    ap.add_argument("--max-extra-latency", type=int, default=0,
                    help="allow rewrites to add up to this many pipeline stages (changes output latency "
                         "versus the spec); tried only after cycle-exact rewrites stall")
    ap.add_argument("--name", help="run folder name under work/iterations/ (default: the top module)")
    ap.add_argument("--fresh", action="store_true",
                    help="start over from --rtl: the existing run folder is moved aside, not deleted")
    a = ap.parse_args()
    if str(a.iters).lower() in ("auto", "0"):
        iters = None
    else:
        try:
            iters = int(a.iters)
        except ValueError:
            raise SystemExit(f"error: --iters must be a number or 'auto', got '{a.iters}'")
        if iters < 1:
            raise SystemExit("error: --iters must be at least 1 (or 'auto')")
    try:
        best, hist, reason = iterate(find_spec(a.spec), Path(a.suite), Path(a.rtl) if a.rtl else None,
                                     clock_ns=a.clock_ns,
                                     iters=iters, n=a.n, n_knob=a.knobs, pdk=a.pdk, name=a.name,
                                     workers=a.workers, budget_min=a.budget_min,
                                     max_extra_latency=a.max_extra_latency, pipeline_now=a.pipeline_now,
                                     fresh=a.fresh)
    except AgentError as e:
        raise SystemExit(f"error: {e}")
    print(final_report(best, hist, reason, a.clock_ns))
    print(f"best RTL: {ITER_ROOT / (a.name or load_suite(a.suite).top) / 'best.sv'}")
    raise SystemExit(0 if best.result.derived.get("timing_met") else 1)


if __name__ == "__main__":
    main()
