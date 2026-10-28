"""Flow wrapper tests that need no Docker: the container call is faked."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from ppa_opt.synth import openroad_flow as of

RTL = "module t(input logic clk, input logic a, output logic y);\n always_ff @(posedge clk) y <= a;\nendmodule\n"
REPORT = {
    "finish__design__instance__area__stdcell": 100.0,
    "finish__design__instance__count__stdcell": 10,
    "finish__timing__setup__ws": 1.0,
    "finish__timing__setup__tns": 0,
    "finish__power__total": 1e-4,
}


@pytest.fixture
def design(tmp_path):
    f = tmp_path / "t.sv"
    f.write_text(RTL)
    return f


def fake_docker(monkeypatch, outcomes):
    """outcomes: list of (returncode, text, write_report) consumed one per docker run."""
    calls = []
    monkeypatch.setattr(of, "_image_id", lambda: "test-image")
    real_run = subprocess.run

    def run(cmd, *a, **kw):
        if cmd[0] != "docker":
            return real_run(cmd, *a, **kw)
        rc, text, write = outcomes[len(calls)]
        calls.append(cmd)
        if write:
            mount = next(c for c in cmd if c.endswith(":/work")).split(":")[0]
            rep = of.Path(mount) / "run/logs/sky130hd/t/base/6_report.json"
            rep.parent.mkdir(parents=True, exist_ok=True)
            rep.write_text(json.dumps(REPORT))
        return SimpleNamespace(returncode=rc, stdout=text, stderr="")

    monkeypatch.setattr(of.subprocess, "run", run)
    return calls


def cfg(**kw):
    return of.FlowConfig(top="t", hdl_frontend="", **kw)   # no slang lint -> no extra docker call


def run(design, tmp_path, **kw):
    return of.run_flow([design], cfg(**kw), work_dir=tmp_path / "w", cache_dir=tmp_path / "c")


def test_crash_then_success_is_retried(monkeypatch, design, tmp_path):
    calls = fake_docker(monkeypatch, [(139, "make: Segmentation fault (core dumped)", False), (0, "ok", True)])
    res = run(design, tmp_path)
    assert len(calls) == 2 and res.attempts == 2
    assert res.metrics["area_um2"] == 100.0
    assert res.provenance["area_um2"].endswith(":finish__design__instance__area__stdcell")


def test_repeated_crash_is_tool_crash_not_design_failure(monkeypatch, design, tmp_path):
    fake_docker(monkeypatch, [(139, "Segmentation fault", False)] * 2)
    with pytest.raises(of.FlowError) as e:
        run(design, tmp_path)
    assert e.value.stage == "tool_crash"


def test_ordinary_failure_is_not_retried(monkeypatch, design, tmp_path):
    calls = fake_docker(monkeypatch, [(2, "make: *** [x] Error 1", False)])
    with pytest.raises(of.FlowError) as e:
        run(design, tmp_path)
    assert e.value.stage != "tool_crash" and len(calls) == 1


def test_cache_hit_skips_docker(monkeypatch, design, tmp_path):
    calls = fake_docker(monkeypatch, [(0, "ok", True)])
    run(design, tmp_path)
    again = run(design, tmp_path)
    assert again.cache_hit and len(calls) == 1


def test_same_rtl_under_another_name_hits_cache(monkeypatch, design, tmp_path):
    calls = fake_docker(monkeypatch, [(0, "ok", True)])
    run(design, tmp_path)
    other = tmp_path / "renamed.sv"
    other.write_text(RTL)
    assert run(other, tmp_path).cache_hit and len(calls) == 1


def test_changed_rtl_or_clock_misses_cache(monkeypatch, design, tmp_path):
    calls = fake_docker(monkeypatch, [(0, "ok", True)] * 3)
    run(design, tmp_path)
    assert not run(design, tmp_path, clock_ns=4.0).cache_hit
    design.write_text(RTL + "// edit\n")
    assert not run(design, tmp_path).cache_hit
    assert len(calls) == 3
