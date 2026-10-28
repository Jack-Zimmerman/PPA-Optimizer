"""Verification gate. There is no golden RTL: a suite built from the spec is the oracle.

    qualify(suite, rtl):   is the suite itself good enough to trust?
        suite passes the first RTL, every spec cover point is hit, code coverage of the RTL
        meets a floor, and the suite kills most mutants of the RTL.
    verify(suite, cand):   lint -> port check against the spec -> run the suite

Only `status == "pass"` candidates may go on to `run_many()`. Failure classes that count
against a candidate: lint, interface, mismatch, assertion. `build`, `suite` and `tool_crash`
leave the candidate unverified (so it is not measured) but are not design verdicts.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..synth.openroad_flow import FlowError, lint
from ..synth.parallel import Candidate
from . import mutants as mut
from .bench import BenchError, SimResult, run_suite
from .interface import InterfaceError, extract_ports
from .suite import Suite, SuiteError, check_ports, load_suite

# GNU make (used by Verilator) cannot build in a path containing spaces, and the project
# folder has one, so verification builds live under the system temp dir.
DEFAULT_VERIFY_DIR = Path(tempfile.gettempdir()) / "ppa_opt_verify"
COUNTS_AGAINST = {"lint", "interface", "mismatch", "assertion"}
MIN_LINE_COV, MIN_TOGGLE_COV, MIN_MUTATION_SCORE = 0.90, 0.80, 0.80


@dataclass
class GateResult:
    name: str
    status: str                       # "pass" | "fail" | "error"
    failure_class: str | None = None  # lint|interface|mismatch|assertion|build|suite|tool_crash
    message: str = ""
    mismatch: dict | None = None
    cycles: int = 0
    seed: int = 0
    code_cov: dict = field(default_factory=dict)
    wall_s: float = 0.0
    latency_extra: int = 0            # extra output cycles vs the spec (only > 0 when allowed)

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    @property
    def counts_against_candidate(self) -> bool:
        return self.failure_class in COUNTS_AGAINST

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def summary(self) -> str:
        if self.ok:
            cov = ", ".join(f"{k} {v:.0%}" for k, v in self.code_cov.items())
            lat = f", +{self.latency_extra} cycle(s) latency" if self.latency_extra else ""
            return f"PASS ({self.cycles} cycles, seed {self.seed}{lat}" + (f", code coverage: {cov})" if cov else ")")
        m = self.mismatch
        if m:
            return (f"FAIL mismatch on '{m['signal']}' at cycle {m['cycle']}: expected {m['expected']}, "
                    f"got {m['got']}, inputs {m['inputs']}")
        first = self.message.strip().splitlines()[0] if self.message.strip() else ""
        return f"{self.status.upper()} [{self.failure_class}] {first}"


def _rtl_key(files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda p: p.name):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def _suite_key(suite: Suite) -> str:
    h = hashlib.sha256()
    for f in sorted(suite.path.glob("*")):
        if f.is_file() and f.suffix in (".py", ".sv"):
            h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:12]


def _verify_strict(suite: Suite, cand: Candidate, *, work_dir: Path = DEFAULT_VERIFY_DIR,
           n_random: int | None = None, seed: int = 1, run_lint: bool = True) -> GateResult:
    """Cycle-exact check of one candidate. Never raises for a design problem."""
    t0 = time.time()

    def done(**kw) -> GateResult:
        return GateResult(name=cand.name, wall_s=round(time.time() - t0, 2), **kw)

    top = cand.top or suite.top
    if top != suite.top:
        return done(status="fail", failure_class="interface",
                    message=f"top module is '{top}', the spec says '{suite.top}'")
    if run_lint:
        try:
            lint(cand.files, top)
        except FlowError as e:
            if e.stage == "lint":
                return done(status="fail", failure_class="lint", message=f"{e}\n{e.log_tail}")
            return done(status="error", failure_class="tool_crash", message=str(e))
    try:
        problems = check_ports(suite, extract_ports(cand.files, top))
    except InterfaceError as e:
        return done(status="error", failure_class="build", message=str(e))
    if problems:
        return done(status="fail", failure_class="interface", message="; ".join(problems))

    build_dir = work_dir / f"{_suite_key(suite)}_{_rtl_key(cand.files)}_{seed}"
    try:
        sim = run_suite(cand.files, suite, build_dir, n_random=n_random, seed=seed)
    except BenchError as e:
        return done(status="error", failure_class=e.kind, message=str(e), seed=seed)
    if sim.status == "fail":
        return done(status="fail", failure_class=sim.failure, mismatch=sim.mismatch,
                    message=sim.message, cycles=sim.cycles, seed=seed)
    return done(status="pass", cycles=sim.cycles, seed=seed, code_cov=sim.code_cov)


def verify(suite: Suite, cand: Candidate, *, work_dir: Path = DEFAULT_VERIFY_DIR,
           n_random: int | None = None, seed: int = 1, run_lint: bool = True,
           max_extra_latency: int = 0) -> GateResult:
    """Check one candidate against the suite.

    With max_extra_latency > 0, a design that fails the strict (cycle-exact) check on outputs gets a
    second, latency-tolerant run: it passes if its outputs equal the model's delayed by k cycles,
    k <= the limit. The strict check always runs first, so a cycle-exact design is never reported
    as delayed. Lint, port and build failures are final."""
    strict = _verify_strict(suite, cand, work_dir=work_dir, n_random=n_random, seed=seed, run_lint=run_lint)
    if strict.ok or max_extra_latency <= 0 or strict.failure_class not in ("mismatch", "assertion"):
        return strict
    return _verify_with_latency(suite, cand, strict, work_dir, n_random, seed, max_extra_latency)


def _verify_with_latency(suite: Suite, cand: Candidate, strict: GateResult, work_dir: Path,
                         n_random: int | None, seed: int, max_extra: int) -> GateResult:
    build_dir = work_dir / f"{_suite_key(suite)}_{_rtl_key(cand.files)}_{seed}_L{max_extra}"
    try:
        sim = run_suite(cand.files, suite, build_dir, n_random=n_random, seed=seed, coverage=False,
                        max_extra_latency=max_extra)
    except BenchError as e:
        return GateResult(cand.name, "error", e.kind, str(e), seed=seed)
    if sim.status == "pass" and sim.latency is not None:
        return GateResult(cand.name, "pass", cycles=sim.cycles, seed=seed, latency_extra=sim.latency,
                          wall_s=strict.wall_s)
    if sim.mismatch:      # the mismatch at the latency that survived longest says more than the strict one
        strict.mismatch = sim.mismatch
    strict.message = (strict.message + f" (also no match with up to {max_extra} extra cycle(s) of latency; "
                      + (sim.message or "") + ")").strip()
    return strict


def verify_many(suite: Suite, candidates: list[Candidate], *, workers: int = 4,
                **kw) -> list[GateResult]:
    """Gate all candidates; identical RTL is verified once; results keep input order."""
    first: dict[str, Candidate] = {}
    key_of: dict[str, str] = {}
    for c in candidates:
        key = f"{_rtl_key(c.files)}:{c.top}"
        key_of[c.name] = key
        first.setdefault(key, c)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {k: pool.submit(verify, suite, c, **kw) for k, c in first.items()}
    done = {k: f.result() for k, f in futs.items()}
    return [GateResult(**{**asdict(done[key_of[c.name]]), "name": c.name}) for c in candidates]


def passing(candidates: list[Candidate], results: list[GateResult]) -> list[Candidate]:
    return [c for c, r in zip(candidates, results) if r.ok]


# ---------------------------------------------------------------------------------------
# Suite qualification
# ---------------------------------------------------------------------------------------

@dataclass
class Qualification:
    ok: bool
    verdict: str                      # qualified | rtl_suite_disagree | suite_weak | suite_invalid | error
    reasons: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    code_cov: dict = field(default_factory=dict)
    mutants_total: int = 0
    mutants_killed: int = 0
    survivors: list[dict] = field(default_factory=list)
    first_run: GateResult | None = None

    @property
    def mutation_score(self) -> float:
        return self.mutants_killed / self.mutants_total if self.mutants_total else 1.0

    def report(self) -> str:
        lines = [f"suite {self.verdict.upper()}"] + [f"  - {r}" for r in self.reasons]
        if self.code_cov:
            lines.append("  code coverage: " + ", ".join(f"{k} {v:.0%}" for k, v in self.code_cov.items()))
        if self.mutants_total:
            lines.append(f"  mutants killed: {self.mutants_killed}/{self.mutants_total} "
                         f"({self.mutation_score:.0%})")
        for s in self.survivors:
            lines.append(f"    survived: line {s['line']} {s['label']}")
        return "\n".join(lines)


def qualify(suite: Suite, rtl: Candidate, *, work_dir: Path = DEFAULT_VERIFY_DIR, workers: int = 4,
            n_mutants: int = 24, seed: int = 1, run_lint: bool = True,
            min_line: float = MIN_LINE_COV, min_toggle: float = MIN_TOGGLE_COV,
            min_mutation: float = MIN_MUTATION_SCORE) -> Qualification:
    """Decide whether `suite` is trustworthy, using the first RTL generated from the same spec.

    A failure here is not a verdict on any candidate: `rtl_suite_disagree` means the RTL and the
    suite read the spec differently (one of them is wrong, a person or an agent must adjudicate
    against the spec); `suite_weak` means the suite needs more tests.
    """
    top = rtl.top or suite.top
    try:
        problems = check_ports(suite, extract_ports(rtl.files, top))
    except InterfaceError as e:
        return Qualification(False, "error", [str(e).splitlines()[0]])
    if problems:
        return Qualification(False, "rtl_suite_disagree", ["ports differ from the spec: " + "; ".join(problems)])
    if run_lint:
        try:
            lint(rtl.files, top)
        except FlowError as e:
            return Qualification(False, "error", [f"RTL does not lint: {e}"])

    try:
        sim = run_suite(rtl.files, suite, work_dir / f"qual_{_suite_key(suite)}_{_rtl_key(rtl.files)}",
                        seed=seed)
    except BenchError as e:
        kind = "suite_invalid" if e.kind in ("suite", "build") else "error"
        return Qualification(False, kind, [f"{e.kind}: {str(e).strip().splitlines()[-1] if str(e).strip() else ''}"])
    first = GateResult(rtl.name, sim.status, sim.failure, sim.message, sim.mismatch, sim.cycles, seed, sim.code_cov)
    if sim.status != "pass":
        return Qualification(False, "rtl_suite_disagree", [first.summary()], first_run=first,
                             code_cov=sim.code_cov)

    q = Qualification(True, "qualified", code_cov=sim.code_cov, first_run=first)
    q.uncovered = sorted(n for n, hits in sim.cover.items() if hits == 0)
    if q.uncovered:
        q.reasons.append("spec cover points never hit: " + ", ".join(q.uncovered))
    if sim.code_cov.get("line", 1.0) < min_line:
        q.reasons.append(f"line coverage {sim.code_cov['line']:.0%} < {min_line:.0%}")
    if sim.code_cov.get("toggle", 1.0) < min_toggle:
        q.reasons.append(f"toggle coverage {sim.code_cov['toggle']:.0%} < {min_toggle:.0%}")

    # mutation check: the suite must fail on broken variants of the RTL it passes
    if len(rtl.files) == 1 and n_mutants > 0:
        text = Path(rtl.files[0]).read_text()
        muts = mut.generate(text, n_mutants, seed)

        def run_mutant(m: mut.Mutant) -> tuple[mut.Mutant, str]:
            d = work_dir / f"mut_{_suite_key(suite)}_{_rtl_key(rtl.files)}" / f"m{m.index}"
            d.mkdir(parents=True, exist_ok=True)
            f = d / Path(rtl.files[0]).name
            f.write_text(m.text)
            try:
                r = run_suite([f], suite, d / "run", n_random=3000, seed=seed, coverage=False, retries=0)
            except BenchError as e:
                return m, "invalid" if e.kind == "build" else "crash"
            return m, "killed" if r.status == "fail" else "survived"

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            outcomes = list(pool.map(run_mutant, muts))
        valid = [(m, s) for m, s in outcomes if s in ("killed", "survived")]
        q.mutants_total = len(valid)
        q.mutants_killed = sum(1 for _, s in valid if s == "killed")
        q.survivors = [{"line": m.line, "label": m.label} for m, s in valid if s == "survived"]
        if q.mutants_total and q.mutation_score < min_mutation:
            q.reasons.append(f"mutation score {q.mutation_score:.0%} < {min_mutation:.0%}")

    if q.reasons:
        q.ok, q.verdict = False, "suite_weak"
    return q


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Verify RTL candidates against a spec-derived test suite")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("qualify", help="check the suite itself against the first RTL")
    q.add_argument("--suite", required=True)
    q.add_argument("rtl")
    q.add_argument("--mutants", type=int, default=24)
    v = sub.add_parser("verify", help="gate candidates (suite should be qualified first)")
    v.add_argument("--suite", required=True)
    v.add_argument("files", nargs="+", help="one candidate per file")
    v.add_argument("--cycles", type=int, help="random cycles (default: the suite's N_RANDOM)")
    v.add_argument("--max-latency", type=int, default=0,
                   help="accept designs with up to this many extra output cycles of latency")
    for p in (q, v):
        p.add_argument("--seed", type=int, default=1)
        p.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    try:
        suite = load_suite(a.suite)
    except SuiteError as e:
        raise SystemExit(f"invalid suite: {e}")
    if a.cmd == "qualify":
        res = qualify(suite, Candidate(Path(a.rtl).stem, [Path(a.rtl)], suite.top),
                      workers=a.workers, n_mutants=a.mutants, seed=a.seed)
        print(res.report())
        raise SystemExit(0 if res.ok else 1)
    cands = [Candidate(Path(f).stem, [Path(f)], suite.top) for f in a.files]
    results = verify_many(suite, cands, workers=a.workers, n_random=a.cycles, seed=a.seed,
                          max_extra_latency=a.max_latency)
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.name.ljust(width)}  {r.summary()}")
    raise SystemExit(0 if all(r.ok for r in results) else 1)


if __name__ == "__main__":
    main()
