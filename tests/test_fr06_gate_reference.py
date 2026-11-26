"""PRD Section 6.8 (T-32): `gate reference` and the reference key
reference/<model-sha256>/<config-hash>/<instance-type>/. Fake llama-server only."""

import copy
import json
from functools import partial
from pathlib import Path

import pytest
import yaml
from baseline_serving import run_gate, serving_config
from fake_llama import FakeLlama

from gate import cli
from gate import reference as ref
from gate.manifest import load_manifest, load_yaml

SERVING = serving_config()
GATE = load_yaml(Path("gate/config.yaml"))
pytestmark = pytest.mark.usefixtures("baseline_workspace")


def _with(**changes):
    cfg = copy.deepcopy(SERVING)
    for dotted, value in changes.items():
        node = cfg
        *path, last = dotted.split("__")
        for k in path:
            node = node[k]
        node[last] = value
    return cfg


def test_key_layout_uses_model_sha256_config_hash_and_instance_type():
    key = ref.reference_key(SERVING, "t3.large")
    sha = load_manifest()["artifacts"][SERVING["model"]]["sha256"]
    assert key == f"reference/{sha}/{ref.config_hash(SERVING)}/t3.large/"
    assert len(ref.config_hash(SERVING)) == 64


@pytest.mark.parametrize(
    "change",
    [{"server__host": "0.0.0.0"}, {"server__port": 9999}, {"server__metrics": False}],
)
def test_where_the_server_listens_does_not_change_the_hash(change):
    assert ref.config_hash(_with(**change)) == ref.config_hash(SERVING)


@pytest.mark.parametrize(
    "change",
    [
        {"threads": 2},
        {"cache__type_k": "q4_0"},
        {"context__ctx_size": 2048},
        {"sampler__temperature": 0.2},
        {"chat_template": "tests/fixtures/templates/f4.jinja"},
        {"server__parallel": 2},
    ],
)
def test_behaviour_settings_change_the_hash(change):
    assert ref.config_hash(_with(**change)) != ref.config_hash(SERVING)


def test_template_is_hashed_by_content_and_engine_by_lock_commit(tmp_path):
    copy_path = tmp_path / "same.jinja"
    copy_path.write_bytes(Path(SERVING["chat_template"]).read_bytes())
    assert ref.config_hash(_with(chat_template=str(copy_path))) == ref.config_hash(SERVING)
    lock = tmp_path / "lock"
    lock.write_text("commit: deadbeef\n")
    assert ref.config_hash(_with(llama_cpp_lock=str(lock))) != ref.config_hash(SERVING)
    eff = ref.effective_config(SERVING)
    assert "model" not in eff and "models" not in eff and "port" not in eff["server"]


def test_model_change_changes_the_key_through_its_sha256():
    f1 = _with(model="qwen2.5-1.5b-instruct-q4_k_m.gguf")
    assert ref.config_hash(f1) == ref.config_hash(SERVING)
    assert ref.reference_key(f1, "x") != ref.reference_key(SERVING, "x")


def test_thread_map_is_hashed_in_full_independent_of_machine(monkeypatch):
    monkeypatch.setenv(ref.INSTANCE_ENV, "local-Sankalps-Razer")
    original = ref.config_hash(SERVING)
    # Changing another machine's setting must still invalidate this machine's reference.
    changed = copy.deepcopy(SERVING)
    changed["threads"]["t3.large"] = 3
    assert ref.config_hash(changed) != original
    monkeypatch.setenv(ref.INSTANCE_ENV, "t3.large")
    assert ref.config_hash(SERVING) == original


def test_instance_type_resolution():
    assert ref.instance_type("t3.large", env={}) == "t3.large"
    assert ref.instance_type(None, env={ref.INSTANCE_ENV: "t3.medium"}) == "t3.medium"
    assert ref.instance_type(None, env={}, imds=lambda: "t3.large") == "t3.large"
    assert ref.instance_type(None, env={}, imds=lambda: None, hostname="my_laptop") == (
        "local-my-laptop"
    )
    with pytest.raises(ValueError):
        ref.instance_type("../etc", env={})


def _gate_config(tmp_path, **store):
    cfg = copy.deepcopy(GATE)
    cfg["reference_store"] = dict(cfg["reference_store"], local_root=str(tmp_path), **store)
    cfg["thresholds"] = str(tmp_path / "no-thresholds.json")
    path = tmp_path / "gate.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_print_key_needs_no_server(tmp_path, capsys):
    g = _gate_config(tmp_path, s3_uri="s3://bucket")
    args = ["reference", "--gate-config", str(g), "--instance-type", "t3.large", "--print-key"]
    assert run_gate(args) == 0
    out = json.loads(capsys.readouterr().out)
    key = ref.reference_key(SERVING, "t3.large")
    assert out == {"key": key, "local_dir": str(tmp_path / key), "s3_uri": f"s3://bucket/{key}"}


def test_reference_measures_and_stores_under_the_key_without_upload(tmp_path, monkeypatch):
    def no_aws(*_a, **_k):
        raise AssertionError("upload must be off by default")

    monkeypatch.setattr(ref, "upload", no_aws)
    monkeypatch.delenv(cli.FORCE_FAIL_ENV, raising=False)
    g = _gate_config(tmp_path, s3_uri="s3://bucket")
    with FakeLlama() as server:
        url = f"http://127.0.0.1:{server.port}"
        args = ["reference", "--gate-config", str(g), "--server", url, "--seed", "0"]
        assert run_gate(args + ["--budget", "4", "--instance-type", "t3.large"]) == 0

        key = ref.reference_key(SERVING, "t3.large")
        path = tmp_path / key / "measurement.json"
        m = json.loads(path.read_text())
        assert m["schema"] == cli.MEASUREMENT_SCHEMA
        assert m["budget"] == 4 and m["seed"] == 0
        assert m["reference"]["key"] == key
        assert m["reference"]["config_hash"] == ref.config_hash(SERVING)
        assert m["reference"]["instance_type"] == "t3.large"

        # The gate finds it through the same key: no --reference needed.
        out = tmp_path / "verdict.json"
        run = ["run", "--gate-config", str(g), "--server", url, "--budget", "4", "--seed", "0"]
        assert run_gate(run + ["--instance-type", "t3.large", "--out", str(out)]) == 0
    v = json.loads(out.read_text())
    assert v["detectors"]["D3"]["mean_agreement_share"] == 1.0
    assert v["decision"]["mode"] == "report"
    assert v["decision"]["gate_score"] is None


@pytest.mark.parametrize(
    ("instance", "use_key", "budget_override", "expected"),
    [
        ("t3.large", False, None, 64),
        ("t3.large", True, None, 64),
        ("local-laptop", False, None, 256),
        ("t3.large", False, 4, 4),
    ],
)
def test_reference_budget_uses_instance_type_unless_overridden(
    tmp_path, instance, use_key, budget_override, expected
):
    g = _gate_config(tmp_path)
    key = ref.reference_key(SERVING, instance)
    location = ["--reference-key", key] if use_key else ["--instance-type", instance]
    budget = [] if budget_override is None else ["--budget", str(budget_override)]
    with FakeLlama() as server:
        assert run_gate(
            ["reference", "--gate-config", str(g),
             "--server", f"http://127.0.0.1:{server.port}", "--seed", "0", *location, *budget]
        ) == 0  # fmt: skip
    measurement = json.loads((tmp_path / key / "measurement.json").read_text())
    assert (len(measurement["d3"]), len(measurement["d5"])) == (expected, expected)


def test_upload_only_with_explicit_flag(tmp_path, monkeypatch):
    calls = []

    def run_aws(args, *, check):
        assert check
        calls.append(args)

    monkeypatch.setattr(ref, "upload", partial(ref.upload, run=run_aws))
    g = _gate_config(tmp_path)
    with FakeLlama() as server:
        url = f"http://127.0.0.1:{server.port}"
        base = ["reference", "--gate-config", str(g), "--server", url, "--seed", "0"]
        base += ["--budget", "2", "--instance-type", "t3.large"]
        assert run_gate(base + ["--upload"]) == 2  # no S3 URI configured
        assert run_gate(base + ["--s3-uri", "s3://bkt/"]) == 0
        assert not calls
        assert run_gate(base + ["--upload", "--s3-uri", "s3://bkt/"]) == 0
    key = ref.reference_key(SERVING, "t3.large")
    assert calls == [
        [
            "aws",
            "s3",
            "cp",
            "--only-show-errors",
            "--sse",
            "AES256",
            str(tmp_path / key / "measurement.json"),
            f"s3://bkt/{key}measurement.json",
        ]
    ]


def test_missing_reference_is_an_input_error(tmp_path):
    g = _gate_config(tmp_path)
    cand = tmp_path / "cand.json"
    cand.write_text("{}")
    args = ["compare", "--gate-config", str(g), "--candidate", str(cand)]
    assert run_gate(args + ["--instance-type", "nowhere"]) == 2


def test_candidates_use_the_baseline_key(tmp_path):
    g = _gate_config(tmp_path)
    cfg = load_yaml(g)
    base = ref.load_baseline(cfg["baseline"])
    want = f"reference/{base['model_sha256']}/{base['config_hash']}/t3.large/"
    assert ref.resolve_key(cfg, SERVING, "t3.large") == want
    f1 = dict(SERVING, model="qwen2.5-1.5b-instruct-q4_k_m.gguf")
    assert ref.resolve_key(cfg, f1, "t3.large") == want  # a candidate never picks its own key
    assert ref.resolve_key(cfg, SERVING, explicit_key="reference/x/y/z") == "reference/x/y/z/"


def test_baseline_file_matches_the_repo_reference_config():
    cfg = copy.deepcopy(GATE)
    assert ref.is_baseline(cfg, SERVING)


def test_baseline_rejects_malformed_hashes(tmp_path):
    bad = tmp_path / "baseline.yaml"
    bad.write_text("model_sha256: abc\nconfig_hash: def\n")
    with pytest.raises(ValueError):
        ref.load_baseline(bad)


def test_reference_is_only_written_from_the_baseline_config(tmp_path):
    g = _gate_config(tmp_path)
    serving = serving_config()
    serving["sampler"]["temperature"] = 1.3
    assert not ref.is_baseline(load_yaml(g), serving)
