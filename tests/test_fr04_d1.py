"""FR-04 (D1): non-zero exit on the F4 and F5 template fixtures; zero on the reference."""

import json
import os
from pathlib import Path

import pytest

from gate import d1, manifest
from gate.manifest import load_yaml

CONFIG = load_yaml(Path("serving/config.yaml"))
FIXTURES = Path("tests/fixtures/templates")
REFERENCE_GGUF = Path(load_yaml(Path("serving/models.yaml"))["models_dir"]) / CONFIG["model"]
HAVE_MODEL = REFERENCE_GGUF.exists()


def test_reference_template_is_pinned_in_the_manifest():
    (entry,) = [
        t for t in manifest.verify_templates() if t["template"] == str(d1.REFERENCE_TEMPLATE)
    ]
    assert entry["ok"]


def test_three_fixed_conversations():
    assert set(d1.CONVERSATIONS) == {"plain_chat", "system_turn", "tool_call"}
    assert d1.CONVERSATIONS["system_turn"]["messages"][0]["role"] == "system"
    assert d1.CONVERSATIONS["tool_call"]["messages"][1]["tool_calls"]


def test_served_reference_template_renders_like_upstream():
    r = d1.template_diff(Path(CONFIG["chat_template"]))
    assert r["ok"], r
    assert all(c["diff"] is None for c in r["cases"].values())


@pytest.mark.parametrize(
    ("fixture", "case"), [("f4.jinja", "plain_chat"), ("f5.jinja", "tool_call")]
)
def test_f4_and_f5_fixtures_differ(fixture, case):
    r = d1.template_diff(FIXTURES / fixture)
    assert r["ok"] is False
    assert r["cases"][case]["ok"] is False
    assert r["cases"][case]["diff"].startswith("--- reference/")


def test_template_that_fails_to_render_is_a_failure(tmp_path):
    bad = tmp_path / "bad.jinja"
    bad.write_text("{{ raise_exception('nope') }}")
    r = d1.template_diff(bad)
    assert r["ok"] is False
    assert "nope" in r["cases"]["plain_chat"]["error"]


@pytest.mark.skipif(not HAVE_MODEL, reason="reference GGUF not downloaded")
@pytest.mark.parametrize(
    ("template", "code"),
    [(None, 0), (FIXTURES / "f4.jinja", 1), (FIXTURES / "f5.jinja", 1)],
)
def test_cli_exit_codes(tmp_path, template, code):
    args = ["--out", str(tmp_path / "d1.json")]
    if template is not None:
        args += ["--template", str(template)]
    assert d1.main(args) == code
    result = json.loads((tmp_path / "d1.json").read_text())
    assert result["schema"] == "driftgate.d1/1"
    assert result["checks"]["ggufdoctor"]["ok"] is True  # the GGUF itself is clean
    assert result["checks"]["template_diff"]["ok"] is (code == 0)
    assert (tmp_path / "d1" / "ggufdoctor.json").is_file()


@pytest.mark.skipif(not HAVE_MODEL, reason="reference GGUF not downloaded")
def test_ggufdoctor_failure_fails_d1(tmp_path, monkeypatch):
    monkeypatch.setattr(d1, "ggufdoctor", lambda gguf, report: {"ok": False, "exit_code": 1})
    result = d1.run(Path("serving/config.yaml"), None, tmp_path)
    assert result["ok"] is False
    assert os.path.exists(tmp_path)
