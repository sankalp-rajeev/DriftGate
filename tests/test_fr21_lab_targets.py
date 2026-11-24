"""Run the Learner Lab Make targets against stand-ins for AWS and CodeCommit."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def lab(tmp_path):
    shutil.copy(ROOT / "Makefile", tmp_path)
    log = tmp_path / "calls"
    env = os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(log)}
    env.pop("APPROVAL_EMAIL", None)
    for command, script in {
        "aws": """
case "$1 $2" in
  "sts get-caller-identity") echo 123456789012 ;;
  "cloudformation describe-stacks") echo lab-bucket ;;
  "cloudformation wait") exit "${WAIT_STATUS:-0}" ;;
esac
""",
        "git": """
case "$1" in
  rev-parse) echo 0123456789abcdef ;;
  archive) touch artifacts/orchestrator.zip ;;
esac
""",
        "uv": "",
    }.items():
        path = tmp_path / command
        path.write_text('#!/bin/bash\necho "${0##*/} $*" >> "$CALLS"\n' + script)
        path.chmod(0o755)

    def run(target, *variables, **extra_env):
        log.write_text("")
        result = subprocess.run(
            ["make", target, *variables],
            cwd=tmp_path,
            env=env | extra_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result, log.read_text().splitlines()

    return run


def test_session_down_waits_for_codedeploy_before_deleting_network(lab):
    result, calls = lab("session-down", "STACK_NAME=net-example", "CD_STACK_NAME=cd-example", "-j4")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "aws cloudformation delete-stack --region us-east-1 --stack-name cd-example",
        "aws cloudformation wait stack-delete-complete --region us-east-1 --stack-name cd-example",
        "aws cloudformation delete-stack --region us-east-1 --stack-name net-example",
        "aws cloudformation wait stack-delete-complete --region us-east-1 --stack-name net-example",
    ]
    result, calls = lab("session-down", WAIT_STATUS="1")
    assert result.returncode != 0
    assert len(calls) == 2


def test_codecommit_push_and_stack_defaults_use_the_same_repository(lab):
    result, calls = lab("codecommit-push")
    assert result.returncode == 0, result.stderr
    assert calls == ["git push codecommit::us-east-1://driftgate main:main"]
    for path in ("deploy/cloudformation/codedeploy.yaml", "orchestrator/template.yaml"):
        template, errors = decode(str(ROOT / path))
        assert not errors
        assert template["Parameters"]["RepositoryName"]["Default"] == "driftgate"


def test_cd_up_passes_network_bucket_repository_and_lab_role(lab):
    result, calls = lab("cd-up", "REPOSITORY_NAME=example")
    assert result.returncode == 0, result.stderr
    assert any("get-caller-identity" in call for call in calls[:-1])
    for option in (
        "--stack-name driftgate-cd",
        "--template-file deploy/cloudformation/codedeploy.yaml",
        "NetworkStackName=driftgate-net",
        "BucketName=lab-bucket",
        "RepositoryName=example",
        "LabRoleArn=arn:aws:iam::123456789012:role/LabRole",
    ):
        assert option in calls[-1]


def test_orch_up_requires_email_before_packaging_or_aws_calls(lab):
    result, calls = lab("orch-up")
    assert result.returncode != 0
    assert "APPROVAL_EMAIL" in result.stderr
    assert calls == []


def test_orch_up_uploads_encrypted_versioned_zip_before_deploying(lab):
    result, calls = lab("orch-up", "APPROVAL_EMAIL=test@example.org")
    assert result.returncode == 0, result.stderr
    assert calls.index(
        "git archive --format=zip -o artifacts/orchestrator.zip HEAD orchestrator/"
    ) < (next(i for i, call in enumerate(calls) if call.startswith("aws s3 cp ")))
    key = "lambda/orchestrator-0123456789abcdef.zip"
    assert any(
        f"aws s3 cp --region us-east-1 --sse AES256 artifacts/orchestrator.zip "
        f"s3://lab-bucket/{key}" == call
        for call in calls[:-1]
    )
    for option in (
        "--template-file orchestrator/template.yaml",
        "RunBucketName=lab-bucket",
        "CodeBucket=lab-bucket",
        f"CodeKey={key}",
        "ApprovalEmail=test@example.org",
        "LabRoleArn=arn:aws:iam::123456789012:role/LabRole",
        "RepositoryName=driftgate",
    ):
        assert option in calls[-1]


def test_lab_status_lists_stack_status_and_project_instance_states(lab):
    result, calls = lab("lab-status")
    assert result.returncode == 0, result.stderr
    assert calls[0].startswith("aws cloudformation describe-stacks")
    for stack in ("driftgate-storage", "driftgate-net", "driftgate-cd", "driftgate-orchestrator"):
        assert stack in calls[0]
    assert "StackStatus" in calls[0]
    assert calls[1].startswith("aws ec2 describe-instances")
    assert "Name=tag:Project,Values=driftgate" in calls[1]
    assert "State.Name" in calls[1]
