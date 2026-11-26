"""The baseline fixture is checked against the same hashes as the reference store."""

import json
import shutil

import pytest
from baseline_serving import BASELINE


@pytest.mark.parametrize(
    "changed",
    [None, "config.yaml", "templates/qwen2.5-instruct.jinja", "llama_cpp.lock", "manifest.json"],
)
def test_fixture_check_uses_the_pinned_baseline_hashes(tmp_path, capsys, changed):
    from analysis.check_baseline_fixture import main

    fixture = tmp_path / "baseline-serving"
    shutil.copytree(BASELINE, fixture)
    if changed == "config.yaml":
        path = fixture / changed
        path.write_text(path.read_text().replace("temperature: 0.8", "temperature: 1.3"))
    elif changed == "manifest.json":
        path = fixture / changed
        data = json.loads(path.read_text())
        data["artifacts"]["qwen2.5-1.5b-instruct-q8_0.gguf"]["sha256"] = "0" * 64
        path.write_text(json.dumps(data))
    elif changed == "llama_cpp.lock":
        path = fixture / changed
        path.write_text(path.read_text().replace("8c16babba", "000000000"))
    elif changed:
        path = fixture / changed
        path.write_text(path.read_text() + "\nchanged\n")
    assert main(["--fixture-dir", str(fixture)]) == (1 if changed else 0)
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is (changed is None)
