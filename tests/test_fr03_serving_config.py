"""FR-03: serving config lives in one versioned file; every fault is a config or artifact diff.

Also covers the PRD Section 6.2.1 model and F3-flag decisions.
"""

import copy
import os
import struct
import subprocess
from pathlib import Path

import pytest

from gate import serve
from gate.manifest import load_yaml

CONFIG = load_yaml(Path("serving/config.yaml"))
MODELS = load_yaml(Path("serving/models.yaml"))
BIN = Path("/opt/llama/bin")


def _argv(cfg):
    return serve.build_argv(cfg, Path("models"), BIN)


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def _with(**changes):
    cfg = copy.deepcopy(CONFIG)
    for dotted, value in changes.items():
        node = cfg
        *path, last = dotted.split("__")
        for key in path:
            node = node[key]
        node[last] = value
    return cfg


# Fault ladder (PRD Section 6.3) expressed as config diffs; value -> expected flag value.
FAULT_DIFFS = {
    "F1": ({"model": "qwen2.5-1.5b-instruct-q4_k_m.gguf"}, "--model"),
    "F2": ({"model": "qwen2.5-1.5b-instruct-q2_k.gguf"}, "--model"),
    "F4": ({"chat_template": "serving/templates/f4.jinja"}, "--chat-template-file"),
    "F5": ({"chat_template": "serving/templates/f5.jinja"}, "--chat-template-file"),
    "F6": ({"sampler__temperature": 0.2}, "--temp"),
    "F7": ({"context__ctx_size": 64}, "--ctx-size"),
    "F9": ({"threads": 1}, "--threads"),
}


@pytest.mark.parametrize(
    ("machine", "threads"),
    [("local-Sankalps-Razer", "8"), ("t3.large", "2"), ("local-other", "2")],
)
def test_launcher_selects_threads_for_the_reference_key_machine(monkeypatch, machine, threads):
    monkeypatch.setenv("DRIFTGATE_INSTANCE_TYPE", machine)
    before = copy.deepcopy(CONFIG)
    assert _flag(_argv(CONFIG), "--threads") == threads
    assert CONFIG == before  # selection must preserve the map for config hashing


def test_reference_config_uses_reference_model_and_template():
    argv = _argv(CONFIG)
    assert _flag(argv, "--model") == "models/qwen2.5-1.5b-instruct-q8_0.gguf"
    assert Path(_flag(argv, "--chat-template-file")).is_file()
    assert "--metrics" in argv
    assert "--jinja" in argv
    assert _flag(argv, "--port") == str(CONFIG["server"]["port"])


@pytest.mark.parametrize("fault", sorted(FAULT_DIFFS))
def test_fault_is_a_config_diff(fault):
    diff, flag = FAULT_DIFFS[fault]
    (value,) = diff.values()
    base, faulted = _argv(CONFIG), _argv(_with(**diff))
    assert _flag(base, flag) != _flag(faulted, flag)
    assert _flag(faulted, flag).endswith(str(value))
    # Only that one flag changes.
    changed = [i for i, (a, b) in enumerate(zip(base, faulted, strict=True)) if a != b]
    assert len(changed) == 1


def test_f3_is_the_prd_kv_cache_diff():
    faulted = _argv(_with(cache__type_k="q4_0", cache__type_v="q4_0", cache__flash_attn="on"))
    assert _flag(faulted, "--cache-type-k") == "q4_0"
    assert _flag(faulted, "--cache-type-v") == "q4_0"
    assert _flag(faulted, "--flash-attn") == "on"


def test_f8_is_an_engine_lock_diff():
    lock = load_yaml(Path(CONFIG["llama_cpp_lock"]))
    assert serve.engine_matches(lock, lock["commit"][:9])
    assert not serve.engine_matches(lock, "deadbeef0")
    assert not serve.engine_matches(lock, None)


def test_models_yaml_matches_prd_6_2_1():
    by_role = {m["role"]: m for m in MODELS["models"]}
    assert by_role["reference"]["repo"] == "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
    assert by_role["reference"]["file"] == "qwen2.5-1.5b-instruct-q8_0.gguf"
    assert by_role["F1"]["file"] == "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    assert by_role["F2"]["file"] == "qwen2.5-1.5b-instruct-q2_k.gguf"
    assert by_role["ci"]["repo"] == "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
    assert by_role["ci"]["file"] == "qwen2.5-0.5b-instruct-q8_0.gguf"
    for m in MODELS["models"]:
        assert len(m["commit"]) == 40  # full pinned commit, never a branch
    assert MODELS["url_template"] == "https://huggingface.co/{repo}/resolve/{commit}/{file}"


LLAMA_SERVER = serve.llama_bin_dir() / "llama-server"


@pytest.mark.skipif(not os.access(LLAMA_SERVER, os.X_OK), reason="llama-server not built")
def test_f3_flags_exist_in_pinned_build_help():
    out = subprocess.run([str(LLAMA_SERVER), "--help"], capture_output=True, text=True).stdout
    assert "--cache-type-k TYPE" in out
    assert "--cache-type-v TYPE" in out
    assert "--flash-attn [on|off|auto]" in out
    for line in out.splitlines():
        if "allowed values" in line:
            assert "q4_0" in line


@pytest.mark.skipif(not os.access(LLAMA_SERVER, os.X_OK), reason="llama-server not built")
def test_built_binary_matches_lock():
    lock = load_yaml(Path(CONFIG["llama_cpp_lock"]))
    assert serve.engine_matches(lock, serve.binary_commit(serve.llama_bin_dir()))


def _embedded_chat_template(gguf: Path) -> bytes:
    """Read the `tokenizer.chat_template` string value from a GGUF header."""
    with open(gguf, "rb") as f:
        head = f.read(16 << 20)
    key = b"tokenizer.chat_template"
    at = head.index(key) + len(key)  # then: u32 value type (8 = string), u64 length, bytes
    (n,) = struct.unpack("<Q", head[at + 4 : at + 12])
    return head[at + 12 : at + 12 + n]


REFERENCE_GGUF = Path(MODELS["models_dir"]) / CONFIG["model"]


@pytest.mark.skipif(not REFERENCE_GGUF.exists(), reason="reference model not downloaded")
def test_reference_template_is_the_one_embedded_in_the_reference_gguf():
    assert Path(CONFIG["chat_template"]).read_bytes() == _embedded_chat_template(REFERENCE_GGUF)
