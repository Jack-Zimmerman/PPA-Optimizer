from __future__ import annotations

from pathlib import Path

from ppa_opt.synth.timing import describe, parse_worst_setup_path, read_critical_path

DATA = Path(__file__).parent / "data" / "finish_violated.rpt"   # trimmed real 6_finish.rpt (fp16_multiply, 5 ns)


def test_parses_worst_setup_path_from_real_report():
    cp = parse_worst_setup_path(DATA.read_text())
    assert cp["startpoint"] == "b1[12]$_DFF_P_" and cp["endpoint"] == "pn2[19]$_DFF_P_"
    assert cp["slack_ns"] == -1.67 and cp["status"] == "VIOLATED"
    assert cp["arrival_ns"] == 6.90 and cp["required_ns"] == 5.23
    assert cp["clk_to_q_ns"] == 0.32 and cp["logic_depth"] == 25
    assert cp["cells"]["fa_1"] == 5 and "dfxtp_2" not in cp["cells"]     # launch flop is not counted as logic
    assert cp["slowest_stages"][0]["delay_ns"] == 0.54
    assert abs(sum(s["delay_ns"] for s in cp["stages"]) - (6.90 - 0.36)) < 0.05   # data path sums to arrival minus launch clock


def test_describe_mentions_slack_and_cells():
    text = describe(parse_worst_setup_path(DATA.read_text()))
    assert "-1.67" in text and "5x fa_1" in text and "pn2[19]" in text


def test_missing_or_empty_report_gives_none(tmp_path):
    assert parse_worst_setup_path("no paths here") is None
    assert read_critical_path(tmp_path, "sky130hd", "x") is None


def test_read_critical_path_adds_provenance(tmp_path):
    d = tmp_path / "run" / "reports" / "sky130hd" / "x" / "base"
    d.mkdir(parents=True)
    (d / "6_finish.rpt").write_text(DATA.read_text())
    cp = read_critical_path(tmp_path, "sky130hd", "x")
    assert cp["provenance"].endswith("6_finish.rpt:report_checks -path_delay max")
