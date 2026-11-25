"""The operator sees the newest pipeline run without downloading logs or approval tokens."""

import json
from datetime import UTC, datetime

import boto3
from moto import mock_aws

from orchestrator import run_record


@mock_aws
def test_pipeline_status_reports_newest_run_and_stage_details(capsys):
    from orchestrator.status import main

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="lab-status")
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    record = run_record.new_record(
        "20261125T120000Z-abcdef01",
        {"commit_id": "abcdef01"},
        ["source", "build", "approval", "deploy"],
        now,
    )
    run_record.end_stage(record, "source", "succeeded", "fetched", now)
    run_record.start_stage(record, "build", now, {"log_uri": "s3://lab-status/build-log/"})
    run_record.end_stage(record, "build", "succeeded", "passed", now)
    run_record.start_stage(record, "approval", now, {"token_sha256": "do-not-display"})
    run_record.end_stage(record, "approval", "succeeded", "approved", now)
    run_record.start_stage(record, "deploy", now, {"deployment_id": "d-DEMO"})
    # Old run updated last, plus control/index objects: neither is the newest run.
    for key, body in (
        ("runs/20261125T120000Z-abcdef01/run.json", record),
        ("runs/20261124T120000Z-00000000/run.json", record | {"run_id": "old"}),
        ("runs/active.json", {"run_id": "control"}),
        ("runs/index/ssm/run.json", {"run_id": "index"}),
    ):
        s3.put_object(Bucket="lab-status", Key=key, Body=json.dumps(body))
    assert main(["--bucket", "lab-status"]) == 0
    out = capsys.readouterr().out
    status = json.loads(out)
    assert status["run_id"] == "20261125T120000Z-abcdef01"
    assert status["commit"] == "abcdef01"
    assert status["deployment_id"] == "d-DEMO"
    assert status["build_log_uri"] == "s3://lab-status/build-log/"
    assert status["approval"]["state"] == "approved"
    assert status["stages"][-1] == {
        "name": "deploy",
        "status": "running",
        "started_at": "2026-11-25T12:00:00+00:00",
        "ended_at": None,
        "outcome": None,
    }
    assert "do-not-display" not in out


@mock_aws
def test_pipeline_status_before_first_run(capsys):
    from orchestrator.status import main

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="lab-status")
    assert main(["--bucket", "lab-status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["run_id"] is None
    assert status["status"] == "no_runs"
