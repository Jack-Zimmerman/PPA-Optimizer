"""Evaluate many candidate designs at once.

Each candidate is one full flow run pinned to `num_cores` CPUs, so N candidates
use about N CPUs of the Docker VM. Identical RTL is run once and shared, cached
results return immediately, and one candidate failing never stops the others.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .openroad_flow import (
    DEFAULT_CACHE, DEFAULT_WORK, FlowConfig, FlowError, FlowResult,
    _hash_inputs, format_summary, lint, run_flow,
)


@dataclass
class Candidate:
    name: str
    files: list[Path]
    top: str | None = None     # overrides the batch default
    cfg: dict | None = None    # FlowConfig field overrides for this candidate (e.g. flow knobs)


@dataclass
class Outcome:
    name: str
    result: FlowResult | None = None
    error: FlowError | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None


def default_workers() -> int:
    """Use the Docker VM's CPU count, as seen from inside a container."""
    import subprocess
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", "--platform", "linux/amd64", "openroad/orfs:latest", "nproc"],
            capture_output=True, text=True, timeout=120,
        ).stdout.strip()
        return max(1, int(out))
    except Exception:
        return 4


def run_many(
    candidates: list[Candidate],
    cfg: FlowConfig,
    *,
    workers: int | None = None,
    use_cache: bool = True,
    work_dir: Path = DEFAULT_WORK,
    cache_dir: Path = DEFAULT_CACHE,
    on_done: Callable[[Outcome], None] | None = None,
) -> list[Outcome]:
    """Run all candidates, up to `workers / num_cores` at a time. Results keep input order."""
    workers = workers or default_workers()
    slots = max(1, workers // max(1, cfg.num_cores))

    jobs: dict[str, tuple[Candidate, FlowConfig]] = {}   # cache key -> first candidate with that RTL
    key_of: dict[str, str] = {}
    for c in candidates:
        ccfg = replace(cfg, top=c.top or cfg.top, **(c.cfg or {}))
        rtl = {Path(f).name: Path(f).read_text() for f in c.files}
        key = _hash_inputs(rtl, ccfg)
        key_of[c.name] = key
        jobs.setdefault(key, (c, ccfg))

    outcomes: dict[str, Outcome] = {}

    def work(key: str) -> tuple[str, FlowResult | FlowError]:
        cand, ccfg = jobs[key]
        try:
            return key, run_flow(cand.files, ccfg, work_dir=work_dir,
                                 cache_dir=cache_dir, use_cache=use_cache)
        except FlowError as e:
            return key, e

    done: dict[str, FlowResult | FlowError] = {}
    with ThreadPoolExecutor(max_workers=slots) as pool:
        futures = [pool.submit(work, k) for k in jobs]
        for fut in as_completed(futures):
            key, res = fut.result()
            done[key] = res
            if on_done:
                for c in candidates:
                    if key_of[c.name] == key:
                        on_done(Outcome(c.name, res if isinstance(res, FlowResult) else None,
                                        res if isinstance(res, FlowError) else None))

    return [
        Outcome(c.name,
                done[key_of[c.name]] if isinstance(done[key_of[c.name]], FlowResult) else None,
                done[key_of[c.name]] if isinstance(done[key_of[c.name]], FlowError) else None)
        for c in candidates
    ]


def comparison_table(outcomes: list[Outcome]) -> str:
    rows = [("candidate", "area um^2", "slack ns", "min period ns", "power mW", "status")]
    ok = [o for o in outcomes if o.ok]
    best = min((o.result.metrics["area_um2"] for o in ok), default=None)
    for o in outcomes:
        if o.ok:
            m, d = o.result.metrics, o.result.derived
            tag = "met" if d["timing_met"] else "VIOLATED"
            if m["area_um2"] == best:
                tag += "  <- smallest"
            rows.append((o.name, f"{m['area_um2']:.1f}", f"{m['setup_wns_ns']:+.3f}",
                         f"{d.get('min_period_ns', float('nan'))}", f"{m['power_total_w'] * 1e3:.4f}", tag))
        else:
            rows.append((o.name, "-", "-", "-", "-", f"FAILED at {o.error.stage}"))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows)


def main() -> None:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Evaluate several single-file candidates in parallel")
    ap.add_argument("files", nargs="+", help="one RTL file per candidate")
    ap.add_argument("--top", help="top module for every candidate (default: each file's basename)")
    ap.add_argument("--clock-ns", type=float, default=5.0)
    ap.add_argument("--pdk", default="sky130hd")
    ap.add_argument("--workers", type=int, help="total CPUs to use (default: all in the Docker VM)")
    ap.add_argument("--num-cores", type=int, default=1, help="CPUs per run")
    ap.add_argument("--abc-area", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", help="write all results as JSON to this path")
    ap.add_argument("--verify-suite", help="spec-derived suite directory; candidates must pass it "
                    "before they are measured. Rejected ones are reported, not run.")
    a = ap.parse_args()

    cands = [Candidate(Path(f).stem, [Path(f)], a.top or Path(f).stem) for f in a.files]
    rejected: list = []
    if a.verify_suite:
        from ..verify.gate import passing, verify_many
        from ..verify.suite import load_suite
        suite = load_suite(a.verify_suite)
        cands = [Candidate(c.name, c.files, suite.top) for c in cands]
        print(f"verifying {len(cands)} candidate(s) against suite {a.verify_suite}")
        gate = verify_many(suite, cands)
        for r in gate:
            print(f"  {r.name}: {r.summary()}")
        rejected = [r for r in gate if not r.ok]
        cands = passing(cands, gate)
        if not cands:
            raise SystemExit("no candidate passed verification; nothing to measure")
    cfg = FlowConfig(top=a.top or cands[0].top, clock_ns=a.clock_ns, pdk=a.pdk,
                     num_cores=a.num_cores, abc_area=a.abc_area)
    workers = a.workers or default_workers()
    print(f"evaluating {len(cands)} candidate(s), {max(1, workers // a.num_cores)} at a time")

    t0 = time.time()

    def progress(o: Outcome) -> None:
        state = ("cache hit" if o.result.cache_hit else f"{o.result.wall_s} s") if o.ok \
            else f"FAILED at {o.error.stage}"
        print(f"  [{time.time() - t0:6.0f}s] {o.name}: {state}", flush=True)

    outcomes = run_many(cands, cfg, workers=workers, use_cache=not a.no_cache, on_done=progress)
    print()
    print(comparison_table(outcomes))
    for o in outcomes:
        if not o.ok:
            print(f"\n{o.name}: {o.error}\n{o.error.log_tail}")
    for r in rejected:
        note = "" if r.counts_against_candidate else " (unverified, not a design verdict)"
        print(f"\n{r.name}: NOT MEASURED, rejected by verification gate [{r.failure_class}]{note}")
    print(f"\nwall time {time.time() - t0:.0f} s")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(
            {**{o.name: (json.loads(o.result.to_json()) if o.ok else {"error": str(o.error)})
                for o in outcomes},
             **{r.name: {"rejected_by_gate": json.loads(r.to_json())} for r in rejected}}, indent=2))


if __name__ == "__main__":
    main()
