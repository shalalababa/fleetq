#!/usr/bin/env python3
"""Offline integration check for wave expansion, artifacts, completion and advice."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLEET = ROOT / "fleet"
SPEC = ROOT / "examples/wave_hello/spec.json"
sys.path.insert(0, str(FLEET))
import durations
import packing
import triage
import wavecheck
import wavespec


def run(*args, env=None, expect=0):
    p = subprocess.run(args, text=True, capture_output=True, env=env)
    assert p.returncode == expect, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    generated = tmp / "generated"
    run(sys.executable, str(FLEET / "wavegen.py"), str(SPEC),
        "--lanes", "0,1", "--out", str(generated), "--quiet")
    spec = wavespec.load(SPEC)
    table = json.loads((generated / "runs.json").read_text())
    ids = {r.run_id for r in spec.runs()}
    assert ids == {r["run_id"] for r in table["runs"]}
    assert len(ids) == 6
    approval = (generated / "RUN_IDS.txt").read_text()
    assert {line.strip() for line in approval.splitlines() if line.strip() in ids} == ids
    assert "TOTAL RUNS: 6" in approval
    assert table["stages"][1]["needs"] == "train"
    lanes = list(generated.glob("lane_*.cmds"))
    assert {p.name for p in lanes} == {
        "lane_train_0.cmds", "lane_train_1.cmds",
        "lane_eval_0.cmds", "lane_eval_1.cmds"}
    for rid in ids:
        assert sum(rid in p.read_text() for p in lanes if p.name.startswith(f"lane_{rid.split('_')[0]}_")) == 1, rid
    submit = (generated / "submit.sh").read_text()
    assert all(p.name in submit for p in lanes)
    for name in ("preflight_remote.sh", "submit.sh", "pull_results.sh", "bundle.sh"):
        run("bash", "-n", str(generated / name))
    assert all(rid in (generated / "pull_results.sh").read_text() for rid in ids)
    assert all(rid in (generated / "bundle.sh").read_text() for rid in ids)

    # Execute the two toy commands for one cell, then fabricate a short and a
    # missing cell. The eval predicate also checks its producer's checkpoint.
    runroot = tmp / "runs"
    runroot.mkdir()
    env = dict(os.environ, RUNROOT=str(runroot))
    for rid in ("train_a", "eval_a"):
        cmd = next(r["cmd"] for r in table["runs"] if r["run_id"] == rid)
        run("bash", "-c", cmd, env=env)
    short = runroot / "train_b"
    (short / "ckpt/model-5").mkdir(parents=True)
    (short / "ckpt/model-5/done").touch()
    (short / "scores.jsonl").write_text('{"score":1}\n')
    ev = runroot / "eval_b"
    ev.mkdir()
    (ev / "result.json").write_text('{"accuracy":1}\n')
    report = wavecheck.check(spec, runroot)
    got = {r["run_id"]: r["status"] for r in report["results"]}
    assert got == {"train_a": "DONE", "eval_a": "DONE",
                   "train_b": "PARTIAL", "eval_b": "PARTIAL",
                   "train_c": "MISSING", "eval_c": "MISSING"}, got
    assert not report["outliers"]
    checked = run(sys.executable, str(FLEET / "wavecheck.py"), str(SPEC),
                  "--runroot", str(runroot), "--json", expect=1)
    assert {r["run_id"]: r["status"] for r in json.loads(checked)["results"]} == got
    pending = run(sys.executable, str(FLEET / "wavecheck.py"), str(SPEC),
                  "--runroot", str(runroot), "--pending", expect=1)
    assert set(pending.splitlines()) == {"train_b", "eval_b", "train_c", "eval_c"}

    # The same signature table is used by the CLI and lane library.
    snippets = {"oom": ("CUDA out of memory", "oom", "other"),
                "disk": ("No space left on device", "disk_full", "never"),
                "unknown": ("unexpected toy failure", "unknown", "unknown")}
    logs = []
    for name, (message, cls, retry) in snippets.items():
        p = tmp / f"{name}.out"
        p.write_text(message + "\n")
        logs.append(p)
        hit = triage.classify(message, triage.load_signatures())
        assert (hit["class"], hit["retry"]) == (cls, retry)
    output = run(sys.executable, str(FLEET / "triage.py"), "classify",
                 *(str(p) for p in logs))
    assert all(cls in output for _, cls, _ in snippets.values())

    records = [dict(family="toy/train", gpu="test", seconds=s, ok=True)
               for s in (10, 20, 30)]
    db = {"records": records, "aggregate": durations.aggregate(records)}
    dbfile = tmp / "durations.json"
    dbfile.write_text(json.dumps(db))
    assert durations.predict(durations.load(dbfile), "toy/train", "test")[0] == 20
    packed = packing.pack([("a", 30, 1), ("b", 20, 1), ("c", 10, 1)], ["0", "1"])
    assert packed["load"] == {"0": 30, "1": 30}, packed
    assert {u[0] for lane in packed["assign"].values() for u in lane} == {"a", "b", "c"}

    # Validation must reject a misspelled expansion variable before generation.
    bad = json.loads(SPEC.read_text())
    bad["stages"][0]["run_id"] = "train_${typo}"
    badfile = tmp / "bad.json"
    badfile.write_text(json.dumps(bad))
    try:
        wavespec.load(badfile)
    except wavespec.SpecError:
        pass
    else:
        raise AssertionError("bad expansion passed validation")

print("t9_waves: generation, artifacts, completion, triage, packing, validation OK")
