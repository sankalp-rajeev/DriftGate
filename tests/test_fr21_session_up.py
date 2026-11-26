"""Session startup ordering and bounded READY wait through fake AWS/Make commands."""

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def session(tmp_path):
    log = tmp_path / "calls"
    parameters = tmp_path / "parameters.json"
    for command, body in {
        "aws": r"""
case "$1 $2" in
  "sts get-caller-identity") exit "${STS_STATUS:-0}" ;;
  "cloudformation describe-stacks")
    if [[ "$*" = *BuildHostId* ]]; then echo i-build; else echo alb.example; fi ;;
  "ssm describe-instance-information") echo Online ;;
  "ssm send-command")
    for arg in "$@"; do
      if [[ "$arg" = file://* ]]; then cp "${arg#file://}" "$PARAMETERS"; fi
    done
    echo command-1 ;;
  "ssm get-command-invocation")
    if [[ "$*" = *StandardOutputContent* ]]; then echo 'DriftGate build host READY';
    else echo "${SSM_STATUS:-Success}"; fi ;;
  *) exit 99 ;;
esac
""",
        "git": 'if [[ -n "${TEST_REAL_GIT:-}" ]]; then exec "$TEST_REAL_GIT" "$@"; fi\n'
        'exit "${GIT_STATUS:-0}"\n',
        "make": 'exit "${MAKE_STATUS:-0}"\n',
        "sleep": "exit 0\n",
        "date": r"""
value=$(cat "$CLOCK" 2>/dev/null || echo 0)
echo "$((value + 500))" > "$CLOCK"
echo "$value"
""",
    }.items():
        path = tmp_path / command
        path.write_text('#!/bin/bash\nset -eu\necho "${0##*/} $*" >> "$CALLS"\n' + body)
        path.chmod(0o755)

    def run(cwd=ROOT, **extra):
        log.write_text("")
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/lab_session_up.sh")],
            cwd=cwd,
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "CALLS": str(log),
                "PARAMETERS": str(parameters),
                "CLOCK": str(tmp_path / "clock"),
                "APPROVAL_EMAIL": "demo@example.org",
            }
            | extra,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result, log.read_text().splitlines(), parameters

    return run


@pytest.mark.parametrize("state", ["fault", "same-tree", "empty"])
def test_session_up_syncs_codecommit_without_rewriting_demo_history(session, tmp_path, state):
    git = shutil.which("git")
    repo = tmp_path / "checkout"
    repo.mkdir()

    def run_git(*args):
        return subprocess.check_output([git, "-C", str(repo), *args], text=True).strip()

    run_git("init", "-b", "main")
    run_git("config", "user.name", "Session Test")
    run_git("config", "user.email", "test@example.org")
    (repo / "config.txt").write_text("baseline")
    run_git("add", ".")
    run_git("commit", "-m", "baseline")
    base = run_git("rev-parse", "HEAD")
    for remote in ("origin", "codecommit"):
        path = tmp_path / remote
        subprocess.run(
            [git, "clone", "--bare", str(repo), str(path)], check=True, capture_output=True
        )
        run_git("remote", "add", remote, str(path))
    if state == "empty":
        subprocess.run(
            [git, "--git-dir", str(tmp_path / "codecommit"), "update-ref", "-d", "refs/heads/main"],
            check=True,
        )
        parent = None
    else:
        if state == "fault":
            (repo / "config.txt").write_text("fault")
            run_git("add", ".")
        run_git("commit", "--allow-empty", "-m", "demo")
        run_git("push", "codecommit", "HEAD:main")
        parent = run_git("rev-parse", "HEAD")
    before = run_git("rev-parse", "HEAD")
    result, _, _ = session(cwd=repo, TEST_REAL_GIT=git)
    assert result.returncode == 0, result.stderr
    tip = run_git("ls-remote", "codecommit", "refs/heads/main").split()[0]
    assert run_git("rev-parse", f"{tip}^{{tree}}") == run_git("rev-parse", f"{base}^{{tree}}")
    assert run_git("rev-parse", "HEAD") == before
    if state == "fault":
        assert run_git("rev-parse", f"{tip}^") == parent
        assert "expected" in result.stdout.lower()
        assert "reject" in result.stdout.lower() and "approval email" in result.stdout.lower()
    elif state == "same-tree":
        assert tip == parent  # an already restored demo history needs no push
    else:
        assert tip == base


def test_session_up_orders_push_and_stacks_then_waits_for_ready(session):
    result, calls, parameters = session()
    assert result.returncode == 0, result.stderr
    assert calls[0].startswith("aws sts get-caller-identity")
    assert calls[1:4] == [
        "git fetch origin main",
        "git ls-remote codecommit refs/heads/main",
        "git push codecommit refs/remotes/origin/main:refs/heads/main",
    ]
    assert [call for call in calls if call.startswith("make ")] == [
        "make up",
        "make cd-up",
        "make orch-up",
    ]
    data = json.loads(parameters.read_text())
    encoded = data["commands"][0].split()[1]
    decoded = base64.b64decode(encoded).decode()
    assert "export HOME=/root PATH=/usr/local/bin:$PATH" in decoded
    assert "test -f /opt/driftgate/.bootstrap-done" in decoded
    assert data["commands"][0].endswith("| base64 --decode | bash")
    assert "http://alb.example" in result.stdout
    assert "confirm" in result.stdout.lower() and "SNS" in result.stdout
    assert not any("delete" in call or "down" in call for call in calls)


@pytest.mark.parametrize(
    "failure",
    [
        {"STS_STATUS": "1"},
        {"GIT_STATUS": "1"},
        {"MAKE_STATUS": "1"},
        {"SSM_STATUS": "Failed"},
        {"SSM_STATUS": "InProgress"},
    ],
)
def test_session_up_stops_on_failure_or_ready_timeout(session, failure):
    result, calls, _ = session(**failure)
    assert result.returncode != 0
    assert "ALB URL" not in result.stdout
    assert not any("delete" in call or "down" in call for call in calls)
