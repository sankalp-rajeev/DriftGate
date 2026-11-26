"""FR-12: the A/A harness records D2 and D1 results and run seeds (fakes; no server, no model)."""

import json
from pathlib import Path

import pytest

from faults import aa, inject
from gate import cli, d1, d2

pytestmark = pytest.mark.usefixtures("baseline_workspace")


def _args(out, bundle, **kw):
    ns = aa.argparse.Namespace(
        runs=2, start=0, level="L1", budget=None, seed=0, run_seed=None, d2=True, d1=True,
        d5_only=False, instance_type="t", gate_config=cli.DEFAULT_GATE_CONFIG,
        reference=None, bundle=bundle, models_dir=Path("models"), out=out,
    )  # fmt: skip
    ns.__dict__.update(kw)
    return ns


def test_harness_runs_d2_and_d1_and_records_levels_and_run_seeds(tmp_path, monkeypatch):
    bundle = inject.build("F1", tmp_path / "bundles")
    seen = {}

    def fake_measure(server, n3, seed, **kw):
        seen["levels"] = (n3, kw["d5_budget"], kw["skip_d3"])
        return {"d3": [], "d5": [], "seed": seed}

    monkeypatch.setattr(aa, "start_server", lambda b, m: (object(), "http://x"))
    monkeypatch.setattr(aa, "stop_server", lambda p: None)
    monkeypatch.setattr(aa.cli, "measure", fake_measure)
    monkeypatch.setattr(aa.cli, "verdict", lambda *a, **k: {"detectors": {}})
    monkeypatch.setattr(aa, "reference_measurement", lambda a, g: {})
    monkeypatch.setattr(aa.d2, "run", lambda s, c, p: {"metrics": {"mean_kld": 0.1}})
    monkeypatch.setattr(aa.d1, "run", lambda c, t, o: {"ok": False})
    monkeypatch.setattr(aa.ref_store, "resolve_key", lambda *a, **k: "reference/a/b/c/")
    out = tmp_path / "out"
    aa.run(_args(out, bundle, run_seed=7))
    assert seen["levels"] == (16, 64, False)  # L1: D3 16 + D5 64
    rec = json.loads((out / "run-01.json").read_text())
    assert rec["level"] == "L1" and rec["run_seed"] == 7 and rec["seed"] == 0
    assert (rec["budget"], rec["d5_budget"]) == (16, 64)
    assert json.loads((out / "run-01.d2.json").read_text())["metrics"]["mean_kld"] == 0.1
    assert json.loads((out / "run-01.d1.json").read_text())["ok"] is False
    assert d1 and d2  # the harness uses the real detector modules


def test_d5_only_sweep_run_skips_d3_and_the_verdict(tmp_path, monkeypatch):
    bundle = inject.build("F1", tmp_path / "bundles")
    seen = {}
    monkeypatch.setattr(aa, "start_server", lambda b, m: (object(), "http://x"))
    monkeypatch.setattr(aa, "stop_server", lambda p: None)
    monkeypatch.setattr(
        aa.cli, "measure", lambda s, n, seed, **kw: seen.update(skip=kw["skip_d3"]) or {"d5": []}
    )
    monkeypatch.setattr(aa, "reference_measurement", lambda a, g: {})
    monkeypatch.setattr(aa.d2, "run", lambda s, c, p: {"metrics": {"mean_kld": 0.2}})
    monkeypatch.setattr(aa.ref_store, "resolve_key", lambda *a, **k: "reference/a/b/c/")
    out = tmp_path / "out"
    aa.run(_args(out, bundle, runs=1, d1=False, d5_only=True))
    assert seen["skip"] is True
    assert json.loads((out / "run-00.json").read_text())["verdict"] is None
    assert not (out / "run-00.d1.json").exists()


def test_gate_measure_accepts_a_level(tmp_path):
    p = cli._parser().parse_args(["measure", "--level", "L2", "--seed", "0"])
    assert p.level == "L2" and p.budget is None
