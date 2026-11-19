"""FR-10: fault injector builds A/A, F1 to F7 and F9 as reproducible diffs."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from faults import inject
from gate import serve
from gate.manifest import load_yaml

REF = load_yaml(Path("serving/config.yaml"))


def _bundle(tmp_path, fault):
    out = inject.build(fault, tmp_path / "bundles")
    meta = json.loads((out / "bundle.json").read_text())
    config = load_yaml(out / "serving/config.yaml")
    return out, meta, config


def _argv(config):
    return serve.build_argv(config, Path("models"), Path("/bin"))


def test_aa_bundle_is_identical_to_reference(tmp_path):
    out, meta, config = _bundle(tmp_path, "AA")
    assert config == REF
    assert (out / "diff.log").read_text() == ""
    assert meta["diff_lines"] == 0
    for rel in meta["files"]:
        assert (out / rel).read_bytes() == Path(rel).read_bytes()


@pytest.mark.parametrize(
    ("fault", "flag", "value"),
    [
        ("F1", "--model", "models/qwen2.5-1.5b-instruct-q4_k_m.gguf"),
        ("F2", "--model", "models/qwen2.5-1.5b-instruct-q2_k.gguf"),
    ],
)
def test_artifact_faults_swap_the_weights(tmp_path, fault, flag, value):
    out, meta, config = _bundle(tmp_path, fault)
    argv = _argv(config)
    assert argv[argv.index(flag) + 1] == value
    assert meta["layer"] == "artifact"
    diff = (out / "diff.log").read_text()
    assert "-model: qwen2.5-1.5b-instruct-q8_0.gguf" in diff
    assert meta["diff_lines"] == 2  # one line out, one line in


def test_f3_sets_the_prd_kv_cache_flags(tmp_path):
    _, meta, config = _bundle(tmp_path, "F3")
    argv = _argv(config)
    for flag, value in [("--cache-type-k", "q4_0"), ("--cache-type-v", "q4_0")]:
        assert argv[argv.index(flag) + 1] == value
    assert argv[argv.index("--flash-attn") + 1] == "on"  # string, not YAML boolean
    assert meta["diff_lines"] == 6


def test_f4_edits_only_the_chat_template(tmp_path):
    out, meta, config = _bundle(tmp_path, "F4")
    assert config == REF
    ref_t = Path(REF["chat_template"]).read_text()
    new_t = (out / REF["chat_template"]).read_text()
    assert new_t != ref_t
    assert "You are Qwen, created by Alibaba Cloud" in new_t  # tools branch untouched
    assert meta["diff_lines"] == 2
    assert f"{REF['chat_template']}" in (out / "diff.log").read_text()


@pytest.mark.parametrize("fault", ["AA", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F9"])
def test_bundles_are_reproducible(tmp_path, fault):
    a = inject.build(fault, tmp_path / "a")
    b = inject.build(fault, tmp_path / "b")
    assert json.loads((a / "bundle.json").read_text()) == json.loads(
        (b / "bundle.json").read_text()
    )


def test_bundle_is_deployable_by_gate_serve(tmp_path):
    out, _, _ = _bundle(tmp_path, "F3")
    res = subprocess.run(
        [sys.executable, "-m", "gate.serve", "--config", "serving/config.yaml", "--dry-run"],
        cwd=out,
        capture_output=True,
        text=True,
        check=True,
    )
    argv = json.loads(res.stdout)["argv"]
    assert argv[argv.index("--cache-type-k") + 1] == "q4_0"
    assert Path(out / argv[argv.index("--chat-template-file") + 1]).is_file()


def test_set_yaml_value_keeps_comments_and_refuses_unknown_keys():
    text = "a: 1  # one\nsec:\n  b: x   # bee\n  c: 2\nd: 3\n"
    new = inject.set_yaml_value(text, "sec.b", "y")
    assert new == "a: 1  # one\nsec:\n  b: y  # bee\n  c: 2\nd: 3\n"
    assert yaml.safe_load(inject.set_yaml_value(text, "d", 4))["d"] == 4
    with pytest.raises(KeyError):
        inject.set_yaml_value(text, "sec.zzz", 1)


def test_make_fault_target(tmp_path):
    res = subprocess.run(
        ["make", "-s", "fault", "F=F4", f"OUT={tmp_path}"], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "F4" / "bundle.json").is_file()
    assert "+++ F4/serving/templates/qwen2.5-instruct.jinja" in res.stdout


def test_aa_harness_records_one_verdict_per_redeploy(tmp_path):
    """A/A harness: identical redeploys from the AA bundle, explicit --seed, one record per run."""
    from llama_fixtures import DEV_PORT, DEV_THREADS, ci_model_available

    from faults import aa
    from gate import cli

    if not ci_model_available():
        pytest.skip("needs llama-server and the CI model matching the manifest")
    models = load_yaml(Path("serving/models.yaml"))
    ci = next(m["file"] for m in models["models"] if m["role"] == "ci")
    base = dict(REF, model=ci, threads=DEV_THREADS)
    base["server"] = dict(REF["server"], port=DEV_PORT)
    base_path = tmp_path / "ci-config.yaml"
    base_path.write_text(yaml.safe_dump(base, sort_keys=False))
    bundle = inject.build("AA", tmp_path / "bundles", base_config=base_path)

    gate_cfg = load_yaml(Path("gate/config.yaml"))
    proc, url = aa.start_server(bundle, Path("models"))
    try:
        data = gate_cfg["data"]
        ref = cli.measure(
            cli.LlamaServer(url), 2, 0, Path(data["d3_prompts"]), Path(data["d5_panel"])
        )
    finally:
        aa.stop_server(proc)
    (tmp_path / "ref.json").write_text(json.dumps(ref))
    gate_cfg = dict(gate_cfg, thresholds=str(tmp_path / "none.json"))
    (tmp_path / "gate.yaml").write_text(json.dumps(gate_cfg))

    out = tmp_path / "aa"
    aa.main(
        ["--runs", "2", "--budget", "2", "--seed", "0", "--bundle", str(bundle)]
        + ["--reference", str(tmp_path / "ref.json"), "--gate-config", str(tmp_path / "gate.yaml")]
        + ["--out", str(out)]
    )
    records = [json.loads((out / f"run-{i:02d}.json").read_text()) for i in range(2)]
    for i, r in enumerate(records):
        assert r["schema"] == "driftgate.aa-run/1"
        assert r["run"] == i and r["seed"] == 0 and r["budget"] == 2
        assert r["bundle"]["fault"] == "AA"
        d = r["verdict"]["detectors"]
        assert d["D3"]["mean_agreement_share"] == 1.0
        assert d["D5"]["delta_accuracy"] == 0.0
        assert (out / f"run-{i:02d}.measurement.json").is_file()


def test_f5_deletes_only_the_tool_call_instruction_in_the_tools_branch(tmp_path):
    ref = Path("serving/templates/qwen2.5-instruct.jinja").read_text().splitlines()
    out, _, _ = _bundle(tmp_path, "F5")
    bundled = (out / "serving/templates/qwen2.5-instruct.jinja").read_text().splitlines()
    changed = [(a, b) for a, b in zip(ref, bundled, strict=True) if a != b]
    assert len(ref) == len(bundled) and len(changed) == 1
    before, after = changed[0]
    assert "return a json object with function name and arguments within" in before
    assert "return a json object" not in after
    assert "<tool_call>" in after  # the rest of the branch, and every other branch, is unchanged
    assert (
        before.replace(
            "For each function call, return a json object with function name and arguments within "
            "<tool_call></tool_call> XML tags:\\n",
            "",
        )
        == after
    )


def test_f9_is_threads_auto_to_one(tmp_path):
    _, _, config = _bundle(tmp_path, "F9")
    assert REF["threads"] == -1 and config["threads"] == 1
