"""FR-15: push-triggered, event-driven Lambda orchestrator with a JSON run record in S3.

AC: a commit to `main` starts a run with no manual steps besides approval; the run record
shows every stage with timestamps and outcome. All AWS calls are mocked with moto.
"""

import time
from datetime import datetime
from pathlib import Path

import boto3
import pytest
from orchestrator_helpers import (
    BUCKET,
    COMMIT,
    REGION,
    approve_latest,
    build_succeeds,
    deploy_event,
    push_event,
    read_record,
    ssm_event,
    stage,
)

from orchestrator.config import load_config
from orchestrator.run_record import RUN_RECORD_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_push_to_main_starts_run_and_build(pipeline):
    out = pipeline["orc"].handle(push_event())
    assert out["action"] == "run_started"
    rec = read_record(pipeline["s3"], out["run_id"])
    assert rec["schema_version"] == RUN_RECORD_SCHEMA_VERSION
    assert rec["status"] == "running"
    assert rec["trigger"]["commit_id"] == COMMIT
    assert rec["trigger"]["branch"] == "main"
    assert stage(rec, "source")["status"] == "succeeded"
    build = stage(rec, "build")
    assert build["status"] == "running"
    assert build["details"]["command_id"]
    assert build["details"]["log_uri"].startswith(f"s3://{BUCKET}/runs/{out['run_id']}/")


def test_build_command_targets_build_host_with_commit(pipeline):
    out = pipeline["orc"].handle(push_event())
    rec = read_record(pipeline["s3"], out["run_id"])
    ssm = boto3.client("ssm", region_name=REGION)
    cmd = ssm.list_commands(CommandId=stage(rec, "build")["details"]["command_id"])["Commands"][0]
    assert cmd["InstanceIds"] == [pipeline["host_id"]]
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
def test_irrelevant_events_are_ignored(pipeline, event):
    out = pipeline["orc"].handle(event)
    assert out["action"] == "ignored"
    assert pipeline["s3"].list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


def test_duplicate_push_event_does_not_start_second_run(pipeline):
    first = pipeline["orc"].handle(push_event(event_id="dup"))
    second = pipeline["orc"].handle(push_event(event_id="dup"))
    assert second["action"] == "ignored"
    assert second["run_id"] == first["run_id"]


def test_full_run_to_success_records_every_stage_with_timestamps(pipeline):
    orc, clock = pipeline["orc"], pipeline["clock"]
    run_id = orc.handle(push_event())["run_id"]
    command_id = stage(read_record(pipeline["s3"], run_id), "build")["details"]["command_id"]

    clock.advance(300)
    assert orc.handle(ssm_event(command_id, "InProgress"))["action"] == "ignored"
    out = orc.handle(ssm_event(command_id, "Success"))
    assert out["action"] == "approval_requested"
    rec = read_record(pipeline["s3"], run_id)
    assert stage(rec, "build")["status"] == "succeeded"
    assert stage(rec, "build")["outcome"] == "Success"

    clock.advance(60)
    assert approve_latest(pipeline, run_id)["result"] == "approved"
    deployment_id = stage(read_record(pipeline["s3"], run_id), "deploy")["details"]["deployment_id"]

    clock.advance(600)
    out = orc.handle(deploy_event(deployment_id, "SUCCESS"))
    assert out["action"] == "run_finished"
    rec = read_record(pipeline["s3"], run_id)
    assert rec["status"] == "succeeded"
    assert [s["name"] for s in rec["stages"]] == ["source", "build", "approval", "deploy"]
    for s in rec["stages"]:
        assert s["status"] == "succeeded"
        assert s["outcome"]
        start = datetime.fromisoformat(s["started_at"])
        end = datetime.fromisoformat(s["ended_at"])
        assert start <= end
    assert stage(rec, "build")["ended_at"] == "2026-11-24T12:05:00+00:00"
    assert stage(rec, "approval")["ended_at"] == "2026-11-24T12:06:00+00:00"
    assert stage(rec, "deploy")["ended_at"] == "2026-11-24T12:16:00+00:00"
    assert rec["ended_at"] == "2026-11-24T12:16:00+00:00"


def test_deploy_uses_bundle_for_commit(pipeline):
    orc = pipeline["orc"]
    run_id = orc.handle(push_event())["run_id"]
    build_succeeds(pipeline, run_id)
    approve_latest(pipeline, run_id)
    deployment_id = stage(read_record(pipeline["s3"], run_id), "deploy")["details"]["deployment_id"]
    cd = boto3.client("codedeploy", region_name=REGION)
    info = cd.get_deployment(deploymentId=deployment_id)["deploymentInfo"]
    loc = info["revision"]["s3Location"]
    assert COMMIT in loc["key"]
    assert info["deploymentGroupName"] == pipeline["cfg"].deploy.deployment_group_name


@pytest.mark.parametrize("status", ["Failed", "TimedOut", "Cancelled"])
def test_build_failure_stops_run_before_deploy(pipeline, status):
    orc = pipeline["orc"]
    run_id = orc.handle(push_event())["run_id"]
    command_id = stage(read_record(pipeline["s3"], run_id), "build")["details"]["command_id"]
    out = orc.handle(ssm_event(command_id, status))
    assert out["action"] == "run_finished"
    rec = read_record(pipeline["s3"], run_id)
    assert rec["status"] == "failed"
    assert stage(rec, "build")["status"] == "failed"
    assert stage(rec, "build")["outcome"] == status
    assert stage(rec, "approval")["status"] == "skipped"
    assert stage(rec, "deploy")["status"] == "skipped"
    assert pipeline["sns"].published == []
    cd = boto3.client("codedeploy", region_name=REGION)
    assert cd.list_deployments()["deployments"] == []


@pytest.mark.parametrize("state", ["FAILURE", "STOP"])
def test_deploy_failure_marks_run_failed(pipeline, state):
    orc = pipeline["orc"]
    run_id = orc.handle(push_event())["run_id"]
    build_succeeds(pipeline, run_id)
    approve_latest(pipeline, run_id)
    deployment_id = stage(read_record(pipeline["s3"], run_id), "deploy")["details"]["deployment_id"]
    orc.handle(deploy_event(deployment_id, state))
    rec = read_record(pipeline["s3"], run_id)
    assert rec["status"] == "failed"
    assert stage(rec, "deploy")["status"] == "failed"
    assert stage(rec, "deploy")["outcome"] == state


def test_repeated_terminal_event_is_idempotent(pipeline):
    orc = pipeline["orc"]
    run_id = orc.handle(push_event())["run_id"]
    command_id = stage(read_record(pipeline["s3"], run_id), "build")["details"]["command_id"]
    orc.handle(ssm_event(command_id, "Success"))
    again = orc.handle(ssm_event(command_id, "Success"))
    assert again["action"] == "ignored"
    assert len(pipeline["sns"].published) == 1


def test_unknown_command_and_deployment_ids_are_ignored(pipeline):
    assert pipeline["orc"].handle(ssm_event("not-ours", "Success"))["action"] == "ignored"
    assert pipeline["orc"].handle(deploy_event("d-NOTOURS", "SUCCESS"))["action"] == "ignored"


def test_no_invocation_waits(pipeline, monkeypatch):
    """FR-15: each invocation makes its transition and returns; nothing sleeps or polls."""

    def boom(*_a, **_k):
        raise AssertionError("orchestrator must not sleep")

    monkeypatch.setattr(time, "sleep", boom)
    orc = pipeline["orc"]
    run_id = orc.handle(push_event())["run_id"]
    build_succeeds(pipeline, run_id)
    approve_latest(pipeline, run_id)
    deployment_id = stage(read_record(pipeline["s3"], run_id), "deploy")["details"]["deployment_id"]
    orc.handle(deploy_event(deployment_id, "SUCCESS"))
    assert read_record(pipeline["s3"], run_id)["status"] == "succeeded"


def test_lambda_handler_entrypoint(pipeline, monkeypatch):
    from orchestrator import handler

    out = handler.lambda_handler(push_event(event_id="entry"), None)
    assert out["action"] == "run_started"
    assert read_record(pipeline["s3"], out["run_id"])["trigger"]["event_id"] == "entry"


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
    patterns = {
        k: v["Properties"]["EventPattern"]
        for k, v in rules.items()
        if "EventPattern" in v["Properties"]
    }
    sources = sorted(p["source"][0] for p in patterns.values())
    assert sources == ["aws.codecommit", "aws.codedeploy", "aws.ec2", "aws.ssm"]
    push = next(p for p in patterns.values() if p["source"] == ["aws.codecommit"])
    assert push["detail"]["referenceType"] == ["branch"]
    # One Lambda permission per rule, each scoped to that rule's ARN (NFR-06).
    perms = [v for v in resources.values() if v["Type"] == "AWS::Lambda::Permission"]
    rule_perms = [p for p in perms if p["Properties"]["Principal"] == "events.amazonaws.com"]
    assert len(rule_perms) == len(rules)
    assert all("SourceArn" in p["Properties"] for p in rule_perms)
