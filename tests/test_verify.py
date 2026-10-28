"""Verification gate tests. Simulation tests run real Verilator + cocotb against the
spec-derived suite in suites/alu_p (no Docker needed: they pass run_lint=False); the lint test
is skipped when Docker is not reachable."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ppa_opt.synth.parallel import Candidate
from ppa_opt.verify import gate, mutants
from ppa_opt.verify.bench import BenchError
from ppa_opt.verify.interface import extract_ports
from ppa_opt.verify.stimulus import vectors
from ppa_opt.verify.suite import SuiteError, check_ports, load_suite

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "parallel_demo"
SUITE_DIR = ROOT / "suites" / "alu_p"
needs_verilator = pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")


def cand(name: str, path: Path) -> Candidate:
    return Candidate(name, [path], "alu_p")


def demo(name: str) -> Path:
    return next(DEMO.glob(f"{name}*.sv"))


def variant_suite(tmp_path: Path, extra: str, keep_sva: bool = True) -> Path:
    """Copy of the alu suite with extra python appended (e.g. to break the model)."""
    d = tmp_path / "suite"
    shutil.copytree(SUITE_DIR, d)
    if not keep_sva:
        (d / "assertions.sv").unlink()
    with open(d / "suite.py", "a") as f:
        f.write("\n" + extra)
    return d


# ---- pure-python pieces -----------------------------------------------------------------

def test_stimulus_is_reproducible_and_exhaustive_when_small():
    kw = dict(widths={"x": 4, "y": 4}, n_random=50, seed=7, reset=("rst", False))
    a = list(vectors(**kw))
    assert a == list(vectors(**kw))
    assert len({(v["x"], v["y"]) for v in a if not v["rst"]}) == 256
    assert a[0]["rst"] == 1                       # starts in reset


def test_stimulus_honours_active_low_reset_and_directed_vectors():
    a = list(vectors({"x": 4}, 0, 1, ("rst_n", True), directed=[{"x": 9}]))
    assert a[0]["rst_n"] == 0 and a[3] == {"x": 9, "rst_n": 1}


def test_mutants_change_one_thing_and_skip_comments():
    src = "assign y = a + b; // a + b stays\nassign z = a & b;\n"
    ms = mutants.generate(src, limit=50)
    assert ms and all(m.text != src for m in ms)
    assert all("// a - b" not in m.text and "// a + b stays" in m.text for m in ms)


def test_suite_loads_and_rejects_malformed(tmp_path):
    s = load_suite(SUITE_DIR)
    assert s.top == "alu_p" and s.clock == "clk" and s.reset == ("rst", False) and s.sva
    assert set(s.data_inputs) == {"a", "b", "op"} and set(s.outputs) == {"y", "lt"}
    (tmp_path / "suite.py").write_text("TOP = 'x'\n")
    with pytest.raises(SuiteError, match="missing"):
        load_suite(tmp_path)


@needs_verilator
def test_port_check_against_spec(tmp_path):
    s = load_suite(SUITE_DIR)
    assert check_ports(s, extract_ports([demo("v0")], "alu_p")) == []
    bad = tmp_path / "alu_p.sv"
    bad.write_text(demo("v0").read_text().replace("[7:0] y", "[8:0] y"))
    assert any("port 'y'" in p for p in check_ports(s, extract_ports([bad], "alu_p")))


# ---- gate against the spec-derived suite ------------------------------------------------

@needs_verilator
@pytest.mark.parametrize("name", ["v0", "v1", "v2", "v3"])
def test_correct_designs_pass(tmp_path, name):
    r = gate.verify(load_suite(SUITE_DIR), cand(name, demo(name)), work_dir=tmp_path,
                    run_lint=False, n_random=3000)
    assert r.ok, r.summary()
    assert r.code_cov["line"] > 0.9


@needs_verilator
def test_v4_all_shared_is_rejected_with_replayable_mismatch(tmp_path):
    s = load_suite(SUITE_DIR)
    r = gate.verify(s, cand("v4", demo("v4")), work_dir=tmp_path, run_lint=False, n_random=3000)
    assert r.status == "fail" and r.failure_class in ("mismatch", "assertion") and r.counts_against_candidate
    if r.mismatch:
        assert r.mismatch["signal"] == "lt"
        again = gate.verify(s, cand("v4", demo("v4")), work_dir=tmp_path / "2", run_lint=False, n_random=3000)
        assert again.mismatch == r.mismatch


@needs_verilator
def test_assertions_catch_a_bug_even_with_a_blind_model(tmp_path):
    blind = variant_suite(tmp_path, "class _B:\n    def step(self, inp): return {'y': None, 'lt': None}\nModel = _B\n")
    s = load_suite(blind)
    assert gate.verify(s, cand("v0", demo("v0")), work_dir=tmp_path / "a", run_lint=False, n_random=500).ok
    r = gate.verify(s, cand("v4", demo("v4")), work_dir=tmp_path / "b", run_lint=False, n_random=500)
    assert r.failure_class == "assertion"


@needs_verilator
def test_port_change_is_an_interface_failure(tmp_path):
    f = tmp_path / "alu_p.sv"
    f.write_text(demo("v0").read_text().replace("output logic lt", "output logic lt, output logic extra")
                 .replace("lt <= 0;", "lt <= 0; extra <= 0;").replace("lt <= a < b;", "lt <= a < b; extra <= 0;"))
    r = gate.verify(load_suite(SUITE_DIR), cand("ported", f), work_dir=tmp_path / "w", run_lint=False)
    assert r.failure_class == "interface" and r.counts_against_candidate


@needs_verilator
def test_unbuildable_rtl_is_not_a_pass(tmp_path):
    r = gate.verify(load_suite(SUITE_DIR), cand("v5", demo("v5")), work_dir=tmp_path, run_lint=False)
    assert not r.ok and r.failure_class in ("build", "interface")


def test_tool_crash_does_not_count_against_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "run_suite", lambda *a, **k: (_ for _ in ()).throw(BenchError("tool_crash", "segfault")))
    r = gate.verify(load_suite(SUITE_DIR), cand("v1", demo("v1")), work_dir=tmp_path, run_lint=False)
    assert r.status == "error" and r.failure_class == "tool_crash" and not r.counts_against_candidate


@needs_verilator
def test_verify_many_dedups_and_keeps_order(tmp_path):
    cs = [cand("a", demo("v1")), cand("b", demo("v4")), cand("c", demo("v1"))]
    rs = gate.verify_many(load_suite(SUITE_DIR), cs, workers=2, work_dir=tmp_path, run_lint=False, n_random=2000)
    assert [r.name for r in rs] == ["a", "b", "c"]
    assert [r.ok for r in rs] == [True, False, True]
    assert [c.name for c in gate.passing(cs, rs)] == ["a", "c"]


# ---- qualifying the suite itself ----------------------------------------------------------

@needs_verilator
def test_good_suite_qualifies(tmp_path):
    q = gate.qualify(load_suite(SUITE_DIR), cand("v0", demo("v0")), work_dir=tmp_path,
                     workers=4, n_mutants=6, run_lint=False)
    assert q.ok, q.report()
    assert q.mutants_total >= 1 and q.mutation_score >= 0.8


@needs_verilator
def test_suite_that_disagrees_with_rtl_is_flagged_not_blamed_on_rtl(tmp_path):
    wrong = variant_suite(tmp_path, "_orig = Model\nclass _W(_orig):\n"
                          "    def step(self, inp):\n        o = super().step(inp)\n"
                          "        if o['y'] is not None and inp['op'] == 2: o['y'] ^= 1\n        return o\nModel = _W\n",
                          keep_sva=False)
    q = gate.qualify(load_suite(wrong), cand("v0", demo("v0")), work_dir=tmp_path / "w", run_lint=False, n_mutants=0)
    assert not q.ok and q.verdict == "rtl_suite_disagree"


@needs_verilator
def test_weak_suite_is_flagged(tmp_path):
    weak = variant_suite(tmp_path, "class _B:\n    def step(self, inp): return {'y': None, 'lt': None}\n"
                         "Model = _B\n", keep_sva=False)
    q = gate.qualify(load_suite(weak), cand("v0", demo("v0")), work_dir=tmp_path / "w",
                     run_lint=False, n_mutants=6)
    assert q.verdict == "suite_weak" and q.mutation_score == 0


def _docker_up() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _docker_up(), reason="docker not reachable")
def test_syntax_error_fails_at_lint(tmp_path):
    r = gate.verify(load_suite(SUITE_DIR), cand("v5", demo("v5")), work_dir=tmp_path)
    assert r.failure_class == "lint" and r.counts_against_candidate


# ---- resolved specs -----------------------------------------------------------------------

def test_parse_spec_reads_port_table():
    from ppa_opt.verify.spec import parse_spec
    top, ports = parse_spec((ROOT / "examples" / "alu_spec" / "spec.md").read_text())
    assert top == "alu_p" and {(p.name, p.direction, p.width) for p in ports} >= {("a", "input", 8), ("lt", "output", 1)}


def test_parse_spec_rejects_missing_table():
    from ppa_opt.verify.spec import SpecError, parse_spec
    with pytest.raises(SpecError, match="port table"):
        parse_spec("Top module: `x`\nA counter.")


def test_suite_checked_against_spec():
    from ppa_opt.verify.spec import check_suite_against_spec
    assert check_suite_against_spec(load_suite(SUITE_DIR), ROOT / "examples" / "alu_spec" / "spec.md") == []


# ---- latency-tolerant gate ------------------------------------------------------------------

PIPE1 = ROOT / "tests" / "data" / "alu_p_pipe1.sv"          # inputs registered: +1 cycle of latency
PIPE1_BUG = ROOT / "tests" / "data" / "alu_p_pipe1_bug.sv"  # same, with a wrong lt


@needs_verilator
def test_pipelined_design_fails_strict_check_by_default(tmp_path):
    r = gate.verify(load_suite(SUITE_DIR), cand("pipe", PIPE1), work_dir=tmp_path, run_lint=False, n_random=2000)
    assert not r.ok and r.failure_class == "mismatch"


@needs_verilator
def test_pipelined_design_accepted_with_latency_allowance(tmp_path):
    s = load_suite(SUITE_DIR)
    r = gate.verify(s, cand("pipe", PIPE1), work_dir=tmp_path, run_lint=False, n_random=2000, max_extra_latency=2)
    assert r.ok and r.latency_extra == 1 and "+1" in r.summary()      # smallest matching latency, not the limit
    assert gate.verify(s, cand("pipe", PIPE1), work_dir=tmp_path / "b", run_lint=False, n_random=2000,
                       max_extra_latency=1).latency_extra == 1


@needs_verilator
def test_cycle_exact_design_stays_at_zero_latency_when_allowance_given(tmp_path):
    r = gate.verify(load_suite(SUITE_DIR), cand("v0", demo("v0")), work_dir=tmp_path, run_lint=False,
                    n_random=2000, max_extra_latency=2)
    assert r.ok and r.latency_extra == 0


@needs_verilator
def test_wrong_pipelined_design_is_still_rejected(tmp_path):
    r = gate.verify(load_suite(SUITE_DIR), cand("bug", PIPE1_BUG), work_dir=tmp_path, run_lint=False,
                    n_random=2000, max_extra_latency=2)
    assert not r.ok and r.counts_against_candidate
    assert "+1: 'lt'" in r.message          # at the real latency the wrong flag is what differs


@needs_verilator
def test_function_arguments_and_return_values_are_not_reported_as_ports(tmp_path):
    f = tmp_path / "m.sv"
    f.write_text("""module m (input logic clk, input logic [7:0] a, output logic [7:0] y);
  function automatic logic [7:0] twice(input logic [7:0] x, input logic [7:0] unused);
    return x + x;
  endfunction
  task automatic t(input logic [3:0] q, output logic [3:0] r);
    r = q;
  endtask
  always_ff @(posedge clk) y <= twice(a, 8'd0);
endmodule
""")
    from ppa_opt.verify.interface import extract_ports
    assert [(p.name, p.direction, p.width) for p in extract_ports([f], "m")] == [
        ("clk", "input", 1), ("a", "input", 8), ("y", "output", 8)]
