"""FR-15: push-triggered, event-driven Lambda orchestrator with a JSON run record in S3.

AC: a commit to `main` starts a run with no manual steps besides approval; the run record
shows every stage with timestamps and outcome. All AWS calls are mocked with moto.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from orchestrator.config import load_config
from orchestrator.handler import Orchestrator
from orchestrator.run_record import RUN_RECORD_SCHEMA_VERSION

REGION = "us-east-1"
BUCKET = "driftgate-runs-test"
INSTANCE = "i-0123456789abcdef0"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def push_event(branch="main", commit=COMMIT, event="referenceUpdated", event_id="evt-1"):
    return {
        "version": "0",
        "id": event_id,
        "detail-type": "CodeCommit Repository State Change",
        "source": "aws.codecommit",
        "time": "2026-11-24T12:00:00Z",
        "region": REGION,
        "resources": [f"arn:aws:codecommit:{REGION}:123456789012:driftgate"],
        "detail": {
            "event": event,
            "repositoryName": "driftgate",
            "referenceType": "branch",
            "referenceName": branch,
            "commitId": commit,
        },
    }


def ssm_event(command_id, status):
    return {
        "id": f"ssm-{command_id}-{status}",
        "detail-type": "EC2 Command Status-change Notification",
        "source": "aws.ssm",
        "detail": {"command-id": command_id, "status": status},
    }


def deploy_event(deployment_id, state):
    return {
        "id": f"cd-{deployment_id}-{state}",
        "detail-type": "CodeDeploy Deployment State-change Notification",
        "source": "aws.codedeploy",
        "detail": {"deploymentId": deployment_id, "state": state, "application": "driftgate"},
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DRIFTGATE_RUN_BUCKET", BUCKET)
    monkeypatch.setenv("DRIFTGATE_BUILD_INSTANCE_IDS", INSTANCE)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        cd = boto3.client("codedeploy", region_name=REGION)
        cfg = load_config()
        cd.create_application(applicationName=cfg.deploy.application_name)
        cd.create_deployment_group(
            applicationName=cfg.deploy.application_name,
            deploymentGroupName=cfg.deploy.deployment_group_name,
            serviceRoleArn="arn:aws:iam::123456789012:role/LabRole",
        )
        clock = FakeClock()
        yield {"orc": Orchestrator(cfg, clock=clock), "s3": s3, "clock": clock, "cfg": cfg}


def read_record(s3, run_id):
    obj = s3.get_object(Bucket=BUCKET, Key=f"runs/{run_id}/run.json")
    return json.loads(obj["Body"].read())


def stage(record, name):
    return next(s for s in record["stages"] if s["name"] == name)


def test_push_to_main_starts_run_and_build(env):
    out = env["orc"].handle(push_event())
    assert out["action"] == "run_started"
    rec = read_record(env["s3"], out["run_id"])
    assert rec["schema_version"] == RUN_RECORD_SCHEMA_VERSION
    assert rec["status"] == "running"
    assert rec["trigger"]["commit_id"] == COMMIT
    assert rec["trigger"]["branch"] == "main"
    assert stage(rec, "source")["status"] == "succeeded"
    build = stage(rec, "build")
    assert build["status"] == "running"
    assert build["details"]["command_id"]
    assert build["details"]["log_uri"].startswith(f"s3://{BUCKET}/runs/{out['run_id']}/")


def test_build_command_targets_build_host_with_commit(env):
    out = env["orc"].handle(push_event())
    rec = read_record(env["s3"], out["run_id"])
    ssm = boto3.client("ssm", region_name=REGION)
    cmd = ssm.list_commands(CommandId=stage(rec, "build")["details"]["command_id"])["Commands"][0]
    assert cmd["InstanceIds"] == [INSTANCE]
    assert cmd["DocumentName"] == "AWS-RunShellScript"
    assert any(COMMIT in c for c in cmd["Parameters"]["commands"])
    assert cmd["OutputS3BucketName"] == BUCKET


@pytest.mark.parametrize(
    "event",
    [
        push_event(branch="feature"),
        push_event(event="referenceDeleted"),
        {"source": "aws.s3", "detail-type": "Object Created", "detail": {}},
    ],
)
def test_irrelevant_events_are_ignored(env, event):
    out = env["orc"].handle(event)
    assert out["action"] == "ignored"
    assert env["s3"].list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


def test_duplicate_push_event_does_not_start_second_run(env):
    first = env["orc"].handle(push_event(event_id="dup"))
    second = env["orc"].handle(push_event(event_id="dup"))
    assert second["action"] == "ignored"
    assert second["run_id"] == first["run_id"]


def test_full_run_to_success_records_every_stage_with_timestamps(env):
    orc, clock = env["orc"], env["clock"]
    run_id = orc.handle(push_event())["run_id"]
    command_id = stage(read_record(env["s3"], run_id), "build")["details"]["command_id"]

    clock.advance(300)
    assert orc.handle(ssm_event(command_id, "InProgress"))["action"] == "ignored"
    out = orc.handle(ssm_event(command_id, "Success"))
    assert out["action"] == "deploy_started"
    rec = read_record(env["s3"], run_id)
    deployment_id = stage(rec, "deploy")["details"]["deployment_id"]
    assert stage(rec, "build")["status"] == "succeeded"
    assert stage(rec, "build")["outcome"] == "Success"

    clock.advance(600)
    out = orc.handle(deploy_event(deployment_id, "SUCCESS"))
    assert out["action"] == "run_finished"
    rec = read_record(env["s3"], run_id)
    assert rec["status"] == "succeeded"
    assert [s["name"] for s in rec["stages"]] == ["source", "build", "deploy"]
    for s in rec["stages"]:
        assert s["status"] == "succeeded"
        assert s["outcome"]
        start = datetime.fromisoformat(s["started_at"])
        end = datetime.fromisoformat(s["ended_at"])
        assert start <= end
    assert stage(rec, "build")["ended_at"] == "2026-11-24T12:05:00+00:00"
    assert stage(rec, "deploy")["ended_at"] == "2026-11-24T12:15:00+00:00"
    assert rec["ended_at"] == "2026-11-24T12:15:00+00:00"


def test_deploy_uses_bundle_for_commit(env):
    orc = env["orc"]
    run_id = orc.handle(push_event())["run_id"]
    command_id = stage(read_record(env["s3"], run_id), "build")["details"]["command_id"]
    orc.handle(ssm_event(command_id, "Success"))
    deployment_id = stage(read_record(env["s3"], run_id), "deploy")["details"]["deployment_id"]
    cd = boto3.client("codedeploy", region_name=REGION)
    info = cd.get_deployment(deploymentId=deployment_id)["deploymentInfo"]
    loc = info["revision"]["s3Location"]
    assert COMMIT in loc["key"]
    assert info["deploymentGroupName"] == env["cfg"].deploy.deployment_group_name


@pytest.mark.parametrize("status", ["Failed", "TimedOut", "Cancelled"])
def test_build_failure_stops_run_before_deploy(env, status):
    orc = env["orc"]
    run_id = orc.handle(push_event())["run_id"]
    command_id = stage(read_record(env["s3"], run_id), "build")["details"]["command_id"]
    out = orc.handle(ssm_event(command_id, status))
    assert out["action"] == "run_finished"
    rec = read_record(env["s3"], run_id)
    assert rec["status"] == "failed"
    assert stage(rec, "build")["status"] == "failed"
    assert stage(rec, "build")["outcome"] == status
    assert stage(rec, "deploy")["status"] == "skipped"
    cd = boto3.client("codedeploy", region_name=REGION)
    assert cd.list_deployments()["deployments"] == []


@pytest.mark.parametrize("state", ["FAILURE", "STOP"])
def test_deploy_failure_marks_run_failed(env, state):
    orc = env["orc"]
    run_id = orc.handle(push_event())["run_id"]
    command_id = stage(read_record(env["s3"], run_id), "build")["details"]["command_id"]
    orc.handle(ssm_event(command_id, "Success"))
    deployment_id = stage(read_record(env["s3"], run_id), "deploy")["details"]["deployment_id"]
    orc.handle(deploy_event(deployment_id, state))
    rec = read_record(env["s3"], run_id)
    assert rec["status"] == "failed"
    assert stage(rec, "deploy")["status"] == "failed"
    assert stage(rec, "deploy")["outcome"] == state


def test_repeated_terminal_event_is_idempotent(env):
    orc = env["orc"]
    run_id = orc.handle(push_event())["run_id"]
    command_id = stage(read_record(env["s3"], run_id), "build")["details"]["command_id"]
    orc.handle(ssm_event(command_id, "Success"))
    again = orc.handle(ssm_event(command_id, "Success"))
    assert again["action"] == "ignored"
    cd = boto3.client("codedeploy", region_name=REGION)
    assert len(cd.list_deployments()["deployments"]) == 1


def test_unknown_command_and_deployment_ids_are_ignored(env):
    assert env["orc"].handle(ssm_event("not-ours", "Success"))["action"] == "ignored"
    assert env["orc"].handle(deploy_event("d-NOTOURS", "SUCCESS"))["action"] == "ignored"


def test_no_invocation_waits(env, monkeypatch):
    """FR-15: each invocation makes its transition and returns; nothing sleeps or polls."""

    def boom(*_a, **_k):
        raise AssertionError("orchestrator must not sleep")

    monkeypatch.setattr(time, "sleep", boom)
    orc = env["orc"]
    run_id = orc.handle(push_event())["run_id"]
    command_id = stage(read_record(env["s3"], run_id), "build")["details"]["command_id"]
    orc.handle(ssm_event(command_id, "Success"))
    deployment_id = stage(read_record(env["s3"], run_id), "deploy")["details"]["deployment_id"]
    orc.handle(deploy_event(deployment_id, "SUCCESS"))
    assert read_record(env["s3"], run_id)["status"] == "succeeded"


def test_lambda_handler_entrypoint(env, monkeypatch):
    from orchestrator import handler

    out = handler.lambda_handler(push_event(event_id="entry"), None)
    assert out["action"] == "run_started"
    assert read_record(env["s3"], out["run_id"])["trigger"]["event_id"] == "entry"


def test_config_values_come_from_config_file():
    cfg = load_config(ROOT / "orchestrator" / "config.json")
    assert cfg.branch == "main"
    assert cfg.build.document == "AWS-RunShellScript"
    assert "{commit_id}" in " ".join(cfg.build.commands)
    assert "{commit_id}" in cfg.deploy.bundle_key


def test_cloudformation_template_lints_clean():
    from cfnlint.api import lint_file

    matches = lint_file(ROOT / "orchestrator" / "template.yaml")
    assert [str(m) for m in matches] == []


def test_template_wires_eventbridge_rules_to_orchestrator():
    from cfnlint.decode import decode

    template, errors = decode(str(ROOT / "orchestrator" / "template.yaml"))
    assert not errors
    resources = template["Resources"]
    rules = {k: v for k, v in resources.items() if v["Type"] == "AWS::Events::Rule"}
    patterns = {k: v["Properties"]["EventPattern"] for k, v in rules.items()}
    sources = sorted(p["source"][0] for p in patterns.values())
    assert sources == ["aws.codecommit", "aws.codedeploy", "aws.ssm"]
    push = next(p for p in patterns.values() if p["source"] == ["aws.codecommit"])
    assert push["detail"]["referenceType"] == ["branch"]
    # One Lambda permission per rule, each scoped to that rule's ARN (NFR-06).
    perms = [v for v in resources.values() if v["Type"] == "AWS::Lambda::Permission"]
    rule_perms = [p for p in perms if p["Properties"]["Principal"] == "events.amazonaws.com"]
    assert len(rule_perms) == len(rules)
    assert all("SourceArn" in p["Properties"] for p in rule_perms)
