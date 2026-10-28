"""Loop logic with a fake author, gate and flow (no agents, no Docker, no simulator)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ppa_opt.llm.devagents import AgentError
from ppa_opt.loop import iterate as it
from ppa_opt.synth.openroad_flow import FlowResult
from ppa_opt.synth.parallel import Outcome
from ppa_opt.verify.gate import GateResult

SUITE = Path(__file__).resolve().parents[1] / "suites" / "alu_p"
SPEC = Path(__file__).resolve().parents[1] / "examples" / "alu_spec" / "spec.md"


def result(wns: float, area: float) -> FlowResult:
    return FlowResult(config={"pdk": "sky130hd", "top": "alu_p"},
                      metrics={"setup_wns_ns": wns, "area_um2": area, "power_total_w": 1e-4},
                      provenance={}, rtl_hash="k", cache_hit=False, run_dir="/nonexistent", wall_s=1.0,
                      derived={"timing_met": wns >= 0}, critical_path=None)


def run_loop(tmp_path, plan, **kw):
    """plan: list per round of rewrite specs [(wns, area) | 'bad' | 'gatefail']; knob variants are neutral."""
    base = tmp_path / "base.sv"
    base.write_text("// wns=-1.0 area=1000\n")
    calls = {"round": 0}
    counter = {"i": 0}

    def author(prompt: str) -> str:
        path = Path(re.search(r"Write your new RTL to (\S+)", prompt).group(1))
        rnd = int(re.search(r"iter_(\d+)", str(path)).group(1))
        idx = int(re.search(r"cand(\d+)", str(path)).group(1))
        spec = plan[min(rnd, len(plan)) - 1][idx] if idx < len(plan[min(rnd, len(plan)) - 1]) else "bad"
        counter["i"] += 1
        if spec == "bad":
            raise AgentError("author failed")
        w, a = (-9, 9999) if spec == "gatefail" else spec
        path.write_text(f"// wns={w} area={a} uid={counter['i']}\n")
        return "ok"

    def gate(suite, cands, **kw):
        return [GateResult(c.name, "fail" if "wns=-9" in c.files[0].read_text() else "pass",
                           "mismatch" if "wns=-9" in c.files[0].read_text() else None) for c in cands]

    def measure(cands, cfg):
        out = []
        for c in cands:
            m = re.search(r"wns=(\S+) area=(\S+)", c.files[0].read_text())
            w, a = float(m.group(1)), float(m.group(2))
            if c.cfg:                      # knob variants change nothing in this fake
                pass
            out.append(Outcome(c.name, result(w, a)))
        return out

    return it.iterate(SPEC, SUITE, base, clock_ns=5.0, author=author, gate=gate, measure=measure,
                      out_root=tmp_path / "out", log=lambda s: None, **kw)


def test_violating_then_area_phase(tmp_path):
    best, hist, reason = run_loop(tmp_path, [[(-0.5, 1100), (-1.2, 900)],     # round 1: -0.5 wins
                                             [(0.1, 1200), (-0.2, 1000)],     # round 2: first design that meets timing
                                             [(0.0, 1000), (0.2, 1300)],      # round 3: area drops 1200 -> 1000, ~1% gains after that
                                             [(0.0, 990)], [(0.0, 985)]],     # tiny gains: area stalls
                                  iters=6, n=2)
    assert best.result.derived["timing_met"] and best.result.metrics["area_um2"] == 985
    assert [h["phase"] for h in hist][:4] == ["baseline", "timing", "timing", "area"]
    assert "area stopped improving" in reason


def test_never_accepts_a_timing_regression(tmp_path):
    best, hist, reason = run_loop(tmp_path, [[(0.3, 800), (0.1, 700)],        # becomes met
                                             [(-0.1, 100), (-0.5, 50)]],      # much smaller but violates
                                  iters=2, n=2)
    assert best.result.derived["timing_met"] and best.result.metrics["area_um2"] == 700


def test_gate_rejects_wrong_designs_and_they_are_not_measured(tmp_path):
    best, hist, reason = run_loop(tmp_path, [["gatefail", (-0.4, 1000)]], iters=1, n=2)
    rejected = [e for e in hist[1]["entries"] if e["name"].endswith("c0")][0]
    assert rejected["metrics"] is None and "gate" in rejected["error"]
    assert best.result.metrics["setup_wns_ns"] == -0.4


def test_stops_when_all_rewrites_rejected_twice(tmp_path):
    best, hist, reason = run_loop(tmp_path, [["gatefail", "gatefail"]], iters=5, n=2)
    assert "rejected" in reason and len(hist) == 3          # baseline + 2 rounds


def test_stalled_timing_reports_unreachable(tmp_path):
    best, hist, reason = run_loop(tmp_path, [[(-2.0, 1000), (-3.0, 900)]], iters=6, n=2)
    assert not best.result.derived["timing_met"] and "no timing improvement" in reason
    assert "not reached" in it.final_report(best, hist, reason, 5.0)


def test_starting_rtl_must_pass_suite(tmp_path):
    base = tmp_path / "base.sv"
    base.write_text("// wns=-1.0 area=1000\n")
    with pytest.raises(AgentError, match="starting RTL fails"):
        it.iterate(SPEC, SUITE, base, clock_ns=5.0, out_root=tmp_path / "o", log=lambda s: None,
                   author=lambda p: "", measure=lambda c, f: [],
                   gate=lambda s, c, **kw: [GateResult("baseline", "fail", "mismatch")])


def test_better_orders_met_before_violating():
    def entry(w, a):
        e = it.Entry("x", Path("x"), "rewrite", "")
        e.result = result(w, a)
        return e
    assert it.better(entry(0.0, 5000), entry(-0.1, 10))        # met beats smaller-but-violating
    assert it.better(entry(0.0, 90), entry(0.5, 100))          # both met: smaller area
    assert it.better(entry(-0.2, 500), entry(-0.9, 100))       # both violating: higher slack
    assert not it.better(entry(0.0, 100), entry(0.0, 100))


def test_pipelining_is_tried_only_after_cycle_exact_stalls(tmp_path):
    """Round 1 cycle-exact rewrites fail to help -> allowance rises to 1 -> a +1 latency design meets
    timing; later rounds keep that latency and shrink area."""
    base = tmp_path / "base.sv"
    base.write_text("// wns=-1.0 area=1000 lat=0\n")
    prompts = []

    def author(prompt):
        prompts.append(prompt)
        path = Path(re.search(r"Write your new RTL to (\S+)", prompt).group(1))
        rnd = int(re.search(r"iter_(\d+)", str(path)).group(1))
        idx = int(re.search(r"cand(\d+)", str(path)).group(1))
        piped = "PIPELINE MODE" in prompt
        if rnd == 1:
            text = f"// wns=-1.5 area={1000 + idx} lat=0"                       # no help
        elif piped:
            text = f"// wns=0.3 area={1500 - 100 * (rnd - 2) - idx} lat=1"      # pipelined, meets timing
        else:
            text = f"// wns=-0.5 area={900 - idx} lat=0"
        path.write_text(text + "\n")
        return "ok"

    seen_allowed = []

    def gate(suite, cands, max_extra_latency=0, **kw):
        seen_allowed.append(max_extra_latency)
        out = []
        for c in cands:
            lat = int(re.search(r"lat=(\d+)", c.files[0].read_text()).group(1))
            g = GateResult(c.name, "pass" if lat <= max_extra_latency else "fail")
            g.latency_extra = lat if lat <= max_extra_latency else 0
            out.append(g)
        return out

    def measure(cands, cfg):
        res = []
        for c in cands:
            m = re.search(r"wns=(\S+) area=(\S+)", c.files[0].read_text())
            res.append(Outcome(c.name, result(float(m.group(1)), float(m.group(2)))))
        return res

    best, hist, reason = it.iterate(SPEC, SUITE, base, clock_ns=5.0, author=author, gate=gate, measure=measure,
                                    out_root=tmp_path / "o", log=lambda s: None, iters=4, n=2, n_knob=0,
                                    max_extra_latency=1)
    assert seen_allowed[:2] == [0, 0]                      # baseline, then round 1 strictly cycle-exact
    assert not any("PIPELINE MODE" in p for p in prompts[:2])      # round 1 prompts: no pipelining offered
    assert any("PIPELINE MODE" in p for p in prompts[2:])          # round 2: offered
    assert best.result.derived["timing_met"] and best.latency_extra == 1
    area_rounds = [h for h in hist if h["phase"] == "area"]
    assert area_rounds and all("Keep exactly" in p for p in prompts[-2:])   # latency is frozen once timing is met
    assert (tmp_path / "o" / "alu_p" / "best.json").exists()
    assert "WARNING" in it.final_report(best, hist, reason, 5.0)


def test_lower_latency_beats_smaller_area_when_timing_met():
    def entry(w, a, lat):
        e = it.Entry("x", Path("x"), "rewrite", "", latency_extra=lat)
        e.result = result(w, a)
        return e
    assert it.better(entry(0.1, 5000, 0), entry(0.1, 100, 1))
    assert it.better(entry(0.1, 90, 1), entry(0.1, 100, 1))
    assert it.better(entry(0.1, 5000, 1), entry(-0.5, 100, 0))      # meeting timing still beats everything


def test_pipeline_now_offers_pipelining_in_the_first_round(tmp_path):
    base = tmp_path / "base.sv"
    base.write_text("// wns=-1.0 area=1000 lat=0\n")
    prompts = []

    def author(prompt):
        prompts.append(prompt)
        path = Path(re.search(r"Write your new RTL to (\S+)", prompt).group(1))
        path.write_text(f"// wns=-0.5 area={900 + len(prompts)} lat=0\n")
        return "ok"

    gate = lambda suite, cands, **kw: [GateResult(c.name, "pass") for c in cands]
    measure = lambda cands, cfg: [Outcome(c.name, result(*map(float, re.search(r"wns=(\S+) area=(\S+)", c.files[0].read_text()).groups()))) for c in cands]
    it.iterate(SPEC, SUITE, base, clock_ns=5.0, author=author, gate=gate, measure=measure, out_root=tmp_path / "o",
               log=lambda s: None, iters=1, n=2, n_knob=0, max_extra_latency=1, pipeline_now=True)
    assert any("PIPELINE MODE" in p for p in prompts)
    prompts.clear()
    it.iterate(SPEC, SUITE, base, clock_ns=5.0, author=author, gate=gate, measure=measure, out_root=tmp_path / "o2",
               log=lambda s: None, iters=1, n=2, n_knob=0, max_extra_latency=1)
    assert not any("PIPELINE MODE" in p for p in prompts)       # default: only after a stall


GOOD = """// ============================================================================
// alu_p -- registered ALU with a shared adder
// Strategy: share one adder for add and subtract
// What changed: merged the two adders in the datapath block
// Why it helps: removes one 8-bit adder from the critical path (expected)
// Equivalence: same ports, same reset, same registered outputs
// Latency: cycle-exact
// Tradeoffs: one extra XOR level on operand b
// ============================================================================
module alu_p(input logic clk);
  // select the result for the current op
  always_comb begin
  end
  // register the result
  always_ff @(posedge clk) begin
  end
endmodule
"""


def test_comment_check_accepts_documented_rtl():
    assert it.comment_check(GOOD) == []


def test_comment_check_reports_missing_header_and_blocks():
    bare = "module m(input logic clk);\n  always_ff @(posedge clk) begin\n  end\n  assign y = 1;\nendmodule\n"
    notes = it.comment_check(bare)
    assert any("no header" in n for n in notes) and any("2 of 2" in n for n in notes)
    partial = "\n".join(GOOD.splitlines()[:5]) + "\nmodule m;\nendmodule\n"
    assert any("header lacks" in n and "tradeoffs" in n for n in it.comment_check(partial))


def test_prompt_requires_documentation(tmp_path):
    e = it.Entry("baseline", Path("x.sv"), "baseline", "")
    e.result = result(-1.0, 100)
    assert "Documentation is required" in it._prompt(e, Path("o.sv"), SPEC, "alu_p", "hint", "timing", 5.0)


def test_comment_check_accepts_a_section_comment_above_declarations():
    ok = GOOD.split("module")[0] + """module m(input logic clk);
  // Stage 1: input registers
  logic [15:0] a1, b1;
  logic        v1;
  always_ff @(posedge clk) begin
    a1 <= 1;
  end

  // Output decode
  assign y = a1;
  assign z = b1;
endmodule
"""
    assert it.comment_check(ok) == []                      # declarations and sibling assigns do not break the link
    bad = ok.replace("  // Output decode\n", "")
    assert any("2 of 3" in n for n in it.comment_check(bad))   # both assigns lose their comment; the always_ff keeps its own


def test_comment_check_on_the_real_fp16_candidates_is_quiet():
    """Candidates the agents wrote for the fp16_multiply run (section comments above declarations) must not be flagged."""
    files = sorted((Path(__file__).resolve().parents[1] / "tests" / "data").glob("documented_*.sv"))
    assert files
    for f in files:
        assert it.comment_check(f.read_text()) == [], f.name


# ---- continuing a run from its folder ---------------------------------------------------------

def _fakes(plan, gate_calls=None):
    """plan: {round: (wns, area, lat)}; every rewrite in that round gets that design (area + index)."""
    def author(prompt):
        path = Path(re.search(r"Write your new RTL to (\S+)", prompt).group(1))
        rnd = int(re.search(r"iter_(\d+)", str(path)).group(1))
        idx = int(re.search(r"cand(\d+)", str(path)).group(1))
        w, a, lat = plan[rnd]
        path.write_text(f"// wns={w} area={a + idx} lat={lat}\n")
        return "ok"

    def gate(suite, cands, max_extra_latency=0, **kw):
        if gate_calls is not None:
            gate_calls.append(max_extra_latency)
        out = []
        for c in cands:
            lat = int(re.search(r"lat=(\d+)", c.files[0].read_text()).group(1))
            g = GateResult(c.name, "pass" if lat <= max_extra_latency else "fail")
            g.latency_extra = lat if g.ok else 0
            out.append(g)
        return out

    def measure(cands, cfg):
        res = []
        for c in cands:
            w, a = re.search(r"wns=(\S+) area=(\S+)", c.files[0].read_text()).groups()
            res.append(Outcome(c.name, result(float(w), float(a))))
        return res
    return author, gate, measure


def _run(tmp_path, plan, rtl=None, gate_calls=None, **kw):
    author, gate, measure = _fakes(plan, gate_calls)
    args = dict(clock_ns=5.0, author=author, gate=gate, measure=measure, out_root=tmp_path / "o",
                log=lambda s: None, iters=1, n=1, n_knob=0)
    args.update(kw)
    return it.iterate(SPEC, SUITE, rtl, **args)


def _base(tmp_path):
    f = tmp_path / "base.sv"
    f.write_text("// wns=-1.0 area=1000 lat=0\n")
    return f


def test_a_folder_with_a_history_is_continued_not_overwritten(tmp_path):
    plan = {1: (-0.5, 900, 0), 2: (0.2, 800, 0)}
    _run(tmp_path, plan, rtl=_base(tmp_path))
    run_dir = tmp_path / "o" / "alu_p"
    first = json.loads((run_dir / "history.json").read_text())
    assert [r["round"] for r in first] == [0, 1]

    best, hist, _ = _run(tmp_path, plan)                      # no RTL given: the folder supplies it
    assert [r["round"] for r in hist] == [0, 1, 2]            # appended, numbered after the last round
    assert hist[:2] == json.loads(json.dumps(first))          # earlier rounds untouched
    assert best.name == "r2c0" and best.result.derived["timing_met"]
    assert hist[2]["entries"][0]["name"] == "r2c0"
    assert (run_dir / "iter_2" / "alu_p_start.sv").read_text() == "// wns=-0.5 area=900 lat=0\n"   # started from round 1's best
    assert (run_dir / "best.sv").read_text().startswith("// wns=0.2")


def test_new_folder_without_rtl_is_an_error(tmp_path):
    with pytest.raises(AgentError, match="starting RTL is required"):
        _run(tmp_path, {})


def test_continuing_at_a_different_clock_is_refused(tmp_path):
    plan = {1: (-0.5, 900, 0)}
    _run(tmp_path, plan, rtl=_base(tmp_path))
    with pytest.raises(AgentError, match="5.0 ns clock"):
        _run(tmp_path, plan, clock_ns=4.0)


def test_fresh_moves_the_old_run_aside_instead_of_deleting_it(tmp_path):
    plan = {1: (-0.5, 900, 0)}
    base = _base(tmp_path)
    _run(tmp_path, plan, rtl=base)
    _, hist, _ = _run(tmp_path, plan, rtl=base, fresh=True)
    assert [r["round"] for r in hist] == [0, 1]               # started over
    aside = [p for p in (tmp_path / "o").iterdir() if ".old-" in p.name]
    assert len(aside) == 1 and (aside[0] / "history.json").exists() and (aside[0] / "best.sv").exists()


def test_continuing_carries_the_best_designs_added_latency(tmp_path):
    plan = {1: (0.3, 1500, 1), 2: (0.3, 1400, 1)}
    calls = []
    best1, _, _ = _run(tmp_path, plan, rtl=_base(tmp_path), max_extra_latency=1, pipeline_now=True)
    assert best1.latency_extra == 1
    best2, hist, _ = _run(tmp_path, plan, gate_calls=calls)   # no MAXLAT flag: the folder knows the latency
    assert calls[0] == 1                                      # the start design was gated with its own latency allowed
    assert best2.latency_extra == 1 and best2.result.metrics["area_um2"] == 1400
    assert json.loads((tmp_path / "o" / "alu_p" / "best.json").read_text())["latency_extra"] == 1


def test_auto_runs_until_converged_not_for_a_fixed_count(tmp_path):
    plan = {1: (-0.5, 900, 0), 2: (0.2, 800, 0), 3: (0.2, 799, 0), 4: (0.2, 798, 0), 5: (0.2, 797, 0),
            6: (0.2, 796, 0), 7: (0.2, 795, 0), 8: (0.2, 794, 0)}
    best, hist, reason = _run(tmp_path, plan, rtl=_base(tmp_path), iters=None)
    assert "area stopped improving" in reason                 # ended by the convergence rule, not a count
    assert [r["round"] for r in hist] == [0, 1, 2, 3, 4]      # timing met in round 2, then two tiny-gain rounds
    assert best.result.metrics["area_um2"] == 798


def test_auto_has_a_safety_cap(tmp_path):
    # area keeps dropping by >2% every round, so only the cap can end the run
    plan = {k: (0.2, 1000 * (0.95 ** k), 0) for k in range(1, it.AUTO_ROUND_CAP + 2)}
    best, hist, reason = _run(tmp_path, plan, rtl=_base(tmp_path), iters=None)
    assert len(hist) == it.AUTO_ROUND_CAP + 1 and "safety cap" in reason


# ---- escalation by slow progress, and author failures ------------------------------------------

def _run_capturing(tmp_path, plan, *, writer_error=None, **kw):
    """Like _run, but records the prompts; writer_error(rnd, idx) -> None | 'after' (write then fail) | 'before'."""
    author0, gate, measure = _fakes(plan)
    prompts = []

    def author(prompt):
        path = Path(re.search(r"Write your new RTL to (\S+)", prompt).group(1))
        rnd = int(re.search(r"iter_(\d+)", str(path)).group(1))
        idx = int(re.search(r"cand(\d+)", str(path)).group(1))
        prompts.append((rnd, "PIPELINE MODE" in prompt))
        mode = writer_error(rnd, idx) if writer_error else None
        if mode == "before":
            raise AgentError("rtl-author failed (exit 1): the claude CLI printed nothing")
        author0(prompt)
        if mode == "after":
            raise AgentError("rtl-author failed (exit 1): killed after writing")
        return "ok"

    rtl = kw.pop("rtl", None) or _base(tmp_path)
    args = dict(clock_ns=5.0, author=author, gate=gate, measure=measure, out_root=tmp_path / "o",
                log=lambda s: None, iters=2, n=2, n_knob=0)
    args.update(kw)
    return it.iterate(SPEC, SUITE, rtl, **args) + (prompts,)


def test_slow_progress_escalates_to_pipelining_even_though_slack_improved(tmp_path):
    # -1.0 -> -0.9 closes 0.1 of a 0.9 ns violation (11%, under 25%): too slow, so round 2 may pipeline
    plan = {1: (-0.9, 900, 0), 2: (0.3, 1500, 1)}
    best, hist, reason, prompts = _run_capturing(tmp_path, plan, max_extra_latency=2)
    assert not any(p for r, p in prompts if r == 1)
    assert any(p for r, p in prompts if r == 2)
    assert best.latency_extra == 1 and best.result.derived["timing_met"]


def test_fast_progress_does_not_escalate(tmp_path):
    # -1.0 -> -0.4 closes 0.6 of the 1.0 violation... and what remains (0.4) is within reach: no pipelining yet
    plan = {1: (-0.4, 900, 0), 2: (0.2, 880, 0)}
    best, hist, reason, prompts = _run_capturing(tmp_path, plan, max_extra_latency=2)
    assert not any(p for r, p in prompts)
    assert best.latency_extra == 0 and best.result.derived["timing_met"]


def test_a_file_written_before_the_agent_errored_is_still_judged_by_the_suite(tmp_path):
    plan = {1: (-0.5, 900, 0)}
    best, hist, reason, prompts = _run_capturing(tmp_path, plan, iters=1, n=1,
                                                 writer_error=lambda r, i: "after")
    assert best.name == "r1c0" and best.result.metrics["setup_wns_ns"] == -0.5      # measured, not discarded
    entry = hist[-1]["entries"][0]
    assert entry["error"] is None and "killed after writing" in entry["author_error"]


def test_author_down_stops_cleanly_and_is_not_counted_as_rejected_designs(tmp_path):
    plan = {1: (-0.5, 900, 0), 2: (0.1, 800, 0)}
    base = _base(tmp_path)
    # round 1 fine, round 2: the author fails on every call and writes nothing
    best, hist, reason, prompts = _run_capturing(
        tmp_path, plan, rtl=base, iters=5, n=3, writer_error=lambda r, i: "before" if r == 2 else None)
    assert "rtl-author agent failed on every call" in reason and "run the same command again" in reason
    assert [r["round"] for r in hist] == [0, 1]                 # the empty round was not recorded
    assert best.name == "r1c0"
    # and the run can simply be continued afterwards
    best2, hist2, _, _ = _run_capturing(tmp_path, plan, iters=1, n=1)
    assert [r["round"] for r in hist2] == [0, 1, 2] and best2.name == "r2c0"


def test_a_stale_file_from_an_interrupted_round_is_never_measured(tmp_path):
    base = _base(tmp_path)
    stale = tmp_path / "o" / "alu_p" / "iter_1"
    stale.mkdir(parents=True)
    (stale / "cand0.sv").write_text("// wns=9.9 area=1 lat=0\n")           # looks like a great design
    best, hist, reason, _ = _run_capturing(tmp_path, {1: (-0.5, 900, 0)}, rtl=base, iters=1, n=1,
                                           writer_error=lambda r, i: "before")
    assert best.name == "baseline" and "rtl-author agent failed" in reason
