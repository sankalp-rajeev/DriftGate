"""Demo commits use the GitHub tree and extend CodeCommit without touching the checkout."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from baseline_serving import copy_serving

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def demo(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Demo Test")
    for name in ("faults", "gate"):
        shutil.copytree(ROOT / name, repo / name, ignore=shutil.ignore_patterns("__pycache__"))
    copy_serving(repo)
    for name in ("pyproject.toml", "uv.lock", "Makefile", "README.md"):
        shutil.copy(ROOT / name, repo / name)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    for remote in ("origin", "codecommit"):
        bare = tmp_path / remote
        subprocess.run(
            ["git", "clone", "--bare", str(repo), str(bare)], check=True, capture_output=True
        )
        git(repo, "remote", "add", remote, str(bare))
    (repo / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
    (repo / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    # Neither staged, unstaged, untracked nor branch-only changes may leak into demos.
    (repo / "local-only").write_text("private checkout change")
    git(repo, "add", "local-only")
    git(repo, "commit", "-m", "local branch")
    (repo / "staged").write_text("staged")
    git(repo, "add", "staged")
    (repo / "serving/config.yaml").write_text("dirty")
    (repo / "untracked").write_text("untracked")
    before = git(repo, "status", "--porcelain=v1")
    head = git(repo, "rev-parse", "HEAD")

    def run(fault):
        command = (
            ["make", "--no-print-directory", "demo-reset"]
            if fault == "RESET"
            else ["make", "--no-print-directory", "demo-push", f"F={fault}"]
        )
        result = subprocess.run(
            command,
            cwd=repo,
            env={k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "BASH_ENV")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        commit = result.stdout.strip()
        assert len(commit) == 40
        assert git(repo, "ls-remote", "codecommit", "refs/heads/main").split()[0] == commit
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "status", "--porcelain=v1") == before
        return commit

    return repo, baseline, run


def test_clean_faults_and_reset_extend_codecommit_with_only_requested_tree(demo):
    repo, baseline, run = demo
    clean = run("CLEAN")
    assert git(repo, "rev-parse", f"{clean}^") == baseline
    assert git(repo, "diff", "--name-only", baseline, clean) == "docs/demo-log.md"
    assert "CLEAN" in git(repo, "show", f"{clean}:docs/demo-log.md")
    assert git(repo, "show", "-s", "--format=%s", clean).startswith("demo: ")

    parent = clean
    for fault, changed in (("F1", "model"), ("F9", "threads"), ("F4", "template")):
        commit = run(fault)
        assert git(repo, "rev-parse", f"{commit}^") == parent
        paths = git(repo, "diff", "--name-only", baseline, commit).splitlines()
        assert all(path.startswith("serving/") for path in paths)
        cfg = yaml.safe_load(git(repo, "show", f"{commit}:serving/config.yaml"))
        if changed == "model":
            assert cfg["model"] == "qwen2.5-1.5b-instruct-q4_k_m.gguf"
        elif changed == "threads":
            assert cfg["threads"] == 1
            assert cfg["model"] == "qwen2.5-1.5b-instruct-q8_0.gguf"
        else:
            assert paths == ["serving/templates/qwen2.5-instruct.jinja"]
        definitions = yaml.safe_load((repo / "faults/faults.yaml").read_text())["faults"]
        assert git(repo, "show", "-s", "--format=%B", commit) == (
            "demo: " + definitions[fault]["description"]
        )
        parent = commit

    reset = run("RESET")
    assert git(repo, "rev-parse", f"{reset}^") == parent
    assert git(repo, "rev-parse", f"{reset}^{{tree}}") == git(
        repo, "rev-parse", f"{baseline}^{{tree}}"
    )


def test_unknown_fault_does_not_push(demo):
    repo, baseline, _ = demo
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/demo_push.sh"), "F999"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert git(repo, "ls-remote", "codecommit", "refs/heads/main").split()[0] == baseline
