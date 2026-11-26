"""FR-02: pin model artifacts by SHA-256 in a manifest; the build fails on any difference."""

import hashlib
import json

from baseline_serving import MANIFEST, MODELS

from gate import manifest, models


def _setup(tmp_path, content=b"weights"):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "m.gguf").write_bytes(content)
    mpath = tmp_path / "manifest.json"
    entry = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content), "source": "x"}
    mpath.write_text(json.dumps({"schema_version": 1, "artifacts": {"m.gguf": entry}}))
    return models_dir, mpath


def test_verify_passes_when_hash_matches(tmp_path):
    models_dir, mpath = _setup(tmp_path)
    result = manifest.verify(["m.gguf"], mpath, models_dir)
    assert result["ok"] is True
    assert result["artifacts"][0]["reason"] is None


def test_verify_fails_when_hash_differs(tmp_path):
    models_dir, mpath = _setup(tmp_path)
    (models_dir / "m.gguf").write_bytes(b"tampered")
    result = manifest.verify(["m.gguf"], mpath, models_dir)
    assert result["ok"] is False
    assert result["artifacts"][0]["reason"] == "sha256 mismatch"


def test_verify_fails_when_file_missing_or_unlisted(tmp_path):
    models_dir, mpath = _setup(tmp_path)
    assert manifest.verify(["other.gguf"], mpath, models_dir)["ok"] is False
    (models_dir / "m.gguf").unlink()
    assert manifest.verify(["m.gguf"], mpath, models_dir)["artifacts"][0]["reason"] == (
        "file missing"
    )


def test_cli_exit_code_is_nonzero_on_mismatch(tmp_path, monkeypatch, capsys):
    models_dir, mpath = _setup(tmp_path)
    (tmp_path / "models.yaml").write_text(f"models_dir: {models_dir}\n")
    (tmp_path / "config.yaml").write_text("model: m.gguf\n")
    args = ["verify", "--config", str(tmp_path / "config.yaml"), "--models"]
    args += [str(tmp_path / "models.yaml"), "--manifest", str(mpath)]
    assert manifest.main(args) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    (models_dir / "m.gguf").write_bytes(b"tampered")
    assert manifest.main(args) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_first_download_records_hash_and_later_mismatch_fails(tmp_path):
    cfg = {
        "models_dir": str(tmp_path / "models"),
        "url_template": "https://huggingface.co/{repo}/resolve/{commit}/{file}",
    }
    entry = {"file": "m.gguf", "role": "reference", "repo": "org/repo", "commit": "abc"}
    seen = []

    def fake_fetch(url, dest):
        seen.append(url)
        dest.write_bytes(b"weights")

    man = {"schema_version": 1, "artifacts": {}}
    first = models.ensure(cfg, man, entry, fetch=fake_fetch)
    assert seen == ["https://huggingface.co/org/repo/resolve/abc/m.gguf"]
    assert first["status"] == "recorded"
    assert man["artifacts"]["m.gguf"]["sha256"] == hashlib.sha256(b"weights").hexdigest()

    second = models.ensure(cfg, man, entry, fetch=fake_fetch)
    assert second == first | {"downloaded": False, "status": "verified"}

    (tmp_path / "models" / "m.gguf").write_bytes(b"tampered")
    assert models.ensure(cfg, man, entry, fetch=fake_fetch)["status"] == "mismatch"
    assert man["artifacts"]["m.gguf"]["sha256"] == hashlib.sha256(b"weights").hexdigest()


def test_committed_manifest_covers_every_model_in_models_yaml():
    cfg = manifest.load_yaml(MODELS)
    man = manifest.load_manifest(MANIFEST)
    for entry in cfg["models"]:
        rec = man["artifacts"][entry["file"]]
        assert len(rec["sha256"]) == 64
        assert rec["source"] == models.artifact_url(cfg, entry)
        assert f"/resolve/{entry['commit']}/" in rec["source"]
