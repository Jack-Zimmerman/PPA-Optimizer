from __future__ import annotations

import json

from ppa_opt.viz import report


def entry(name, source, area=None, slack=0.1, latency=0, err=None, strategy="s"):
    m = None if area is None else {"area_um2": area, "setup_wns_ns": slack, "power_total_w": 2e-4}
    d = None if area is None else {"min_period_ns": round(5.0 - slack, 3), "timing_met": slack >= 0}
    return {"name": name, "source": source, "strategy": strategy, "overrides": {}, "latency_extra": latency,
            "error": err, "gate": "pass" if m else "fail", "metrics": m, "derived": d, "provenance": {}}


HIST = [
    {"round": 0, "phase": "baseline", "best": "baseline", "improved": False, "entries": [entry("baseline", "baseline", 1000, -1.0)]},
    {"round": 1, "phase": "timing", "best": "r1c1", "improved": True, "entries": [
        entry("r1c0", "rewrite", 1100, -1.5), entry("r1c1", "rewrite", 1050, -0.5),
        entry("r1c2", "rewrite", err="gate: FAIL mismatch"), entry("r1-abc", "knob", 1000, -1.0)]},
    {"round": 2, "phase": "timing", "best": "r2c0", "improved": True, "entries": [
        entry("r2c0", "rewrite", 1300, 0.2, latency=1)]},
]


def test_build_run_tracks_the_best_design_over_rounds():
    run = report.build_run("d", HIST)
    assert [i["name"] for i in run["incumbents"]] == ["baseline", "r1c1", "r2c0"]
    assert [i["area"] for i in run["incumbents"]] == [1000, 1050, 1300]
    assert run["incumbents"][2]["latency"] == 1 and run["incumbents"][2]["met"] is True
    assert [(r["tested"], r["rejected"]) for r in run["rounds"]] == [(1, 0), (3, 1), (1, 0)]
    assert run["target_ns"] == 5.0                                     # min period + setup slack
    assert [r["incumbent"] for r in run["rows"]].count(True) == 3
    rej = next(r for r in run["rows"] if r["name"] == "r1c2")
    assert not rej["measured"] and "mismatch" in rej["error"]


def test_power_is_converted_from_report_watts_to_milliwatts():
    run = report.build_run("d", HIST)
    assert abs(run["rows"][0]["power_mw"] - 0.2) < 1e-9


def test_render_embeds_data_safely():
    hist = [dict(HIST[0], entries=[entry("baseline", "baseline", 1000, -1.0, strategy="</script><b>x</b>")])]
    html = report.render([report.build_run("d", hist)], "d")
    body = html.split('<script id="data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(body)["runs"][0]["rows"][0]["strategy"] == "</script><b>x</b>"   # survives the round trip
    assert html.count("</script>") == 2                                              # data block + page script


def test_load_runs_skips_unreadable_files(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "history.json").write_text(json.dumps(HIST))
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "history.json").write_text("{not json")
    assert [r["name"] for r in report.load_runs(tmp_path)] == ["good"]
