"""cocotb test body: drive the DUT, check every output against the suite's reference model
each cycle, and record which spec cover points the stimulus hit. Loaded by cocotb inside the
simulator process; configuration comes from the JSON file named in PPA_VERIFY_CFG and the
verdict is written to the path in that file (never inferred from cocotb's pass/fail alone).
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

import cocotb
from cocotb.triggers import Timer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ppa_opt.verify.latency import LatencyTracker  # noqa: E402
from ppa_opt.verify.stimulus import vectors  # noqa: E402
from ppa_opt.verify.suite import load_module  # noqa: E402

MAX_MISMATCHES_KEPT = 1


@cocotb.test()
async def check_against_model(dut):
    cfg = json.loads(Path(os.environ["PPA_VERIFY_CFG"]).read_text())
    out_path = Path(cfg["result_path"])
    result = {"status": "error", "cycles": 0, "mismatch": None, "cover": {}, "detail": ""}
    try:
        suite = load_module(Path(cfg["suite_dir"]))
        model = suite.Model()
        widths = {n: w for n, w in cfg["data_inputs"].items()}
        outputs = cfg["outputs"]
        clock, reset = cfg["clock"], cfg["reset"]
        cover = {name: 0 for name in suite.COVER}
        directed = suite.DIRECTED() if hasattr(suite, "DIRECTED") else ()
        gen = getattr(suite, "RANDOM", None)
        n_random = cfg["n_random"] if cfg["n_random"] is not None else getattr(suite, "N_RANDOM", 20000)

        max_extra = cfg.get("max_extra_latency", 0)
        tracker = LatencyTracker(max_extra, outputs) if max_extra > 0 else None
        if clock:
            getattr(dut, clock).value = 0
        cycle = 0
        deadline = time.time() + cfg.get("wall_budget_s", 600)
        for pins in vectors(widths, n_random, cfg["seed"], tuple(reset) if reset else None,
                            directed, gen):
            if time.time() > deadline:
                raise TimeoutError(f"simulation exceeded {cfg.get('wall_budget_s', 600)} s wall budget at cycle {cycle}")
            for name, val in pins.items():
                getattr(dut, name).value = val
            await Timer(1, "ns")
            if clock:
                getattr(dut, clock).value = 1
                await Timer(1, "ns")
            model_in = dict(pins)
            expected = model.step(model_in)
            bad = None
            got = {name: int(getattr(dut, name).value) for name in outputs}
            if tracker:
                rname = reset[0] if reset else None
                asserted = bool(rname) and pins[rname] == (0 if reset[1] else 1)
                tracker.update(cycle, asserted, pins, expected, got)
            else:
                for name in outputs:
                    want = expected.get(name)
                    if want is None:
                        continue
                    if got[name] != want & ((1 << outputs[name]) - 1):
                        bad = {"cycle": cycle, "signal": name, "expected": want, "got": got[name],
                               "inputs": dict(pins)}
                        break
            for cname, fn in suite.COVER.items():
                try:
                    if fn(pins, expected):
                        cover[cname] += 1
                except Exception:  # noqa: BLE001 - a broken cover predicate must be visible
                    result["detail"] += f"cover point '{cname}' raised: {traceback.format_exc(limit=1)}\n"
            if clock:
                getattr(dut, clock).value = 0
                await Timer(1, "ns")
            cycle += 1
            result["cycles"] = cycle
            result["cover"] = cover
            if bad:
                result["status"], result["mismatch"] = "fail", bad
                break
            if tracker and tracker.all_failed:
                result["status"], result["mismatch"] = "fail", tracker.best_mismatch()
                result["detail"] = "first mismatch per latency tried: " + tracker.summary()
                break
        else:
            result["status"] = "pass"
            if tracker:
                result["latency"] = tracker.latency
                if tracker.latency is None:
                    result["status"], result["mismatch"] = "fail", tracker.best_mismatch()
                    result["detail"] = "first mismatch per latency tried: " + tracker.summary()
    except Exception:
        result["status"], result["detail"] = "error", traceback.format_exc()
    finally:
        out_path.write_text(json.dumps(result))
