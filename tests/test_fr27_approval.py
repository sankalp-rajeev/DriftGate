"""FR-27: SNS approval email with approve and reject links (Lambda function URL, one-time
token); no response within a set time means reject.

AC: approve starts Deploy; reject or timeout ends the run; decision recorded in the run record.
"""

from pathlib import Path

import pytest
from orchestrator_helpers import (
    APPROVAL_URL,
    build_succeeds,
    links_from,
    push_event,
    read_record,
    scheduled_event,
    stage,
    url_event,
)

from orchestrator import approval
from orchestrator.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def started_run(pipeline):
    run_id = pipeline["orc"].handle(push_event())["run_id"]
    assert build_succeeds(pipeline, run_id)["action"] == "approval_requested"
    return run_id


def email_links(pipeline):
    return links_from(pipeline["sns"].published[-1]["Message"])


def click(pipeline, link, method="POST", **override):
    _url, params = email_links(pipeline)[link]
    params = {**params, **override}
    return approval.handle(url_event(method, params), pipeline["orc"])


def deployments(pipeline):
    return pipeline["cd"].list_deployments()["deployments"]


def test_build_success_publishes_email_with_both_links(pipeline):
    run_id = started_run(pipeline)
    [msg] = pipeline["sns"].published
    assert msg["TopicArn"] == pipeline["topic_arn"]
    assert run_id in msg["Subject"]
    links = email_links(pipeline)
    assert set(links) == {"approve", "reject"}
    for url, params in links.values():
        assert url.startswith(APPROVAL_URL)
        assert params["run_id"] == run_id
    # One token per approval request, shared by both links; the record keeps only its hash.
    assert links["approve"][1]["token"] == links["reject"][1]["token"]
    rec = read_record(pipeline["s3"], run_id)
    st = stage(rec, "approval")
    assert st["status"] == "running"
    assert links["approve"][1]["token"] not in str(rec)
    assert st["details"]["deadline"] == "2026-11-24T13:00:00+00:00"
    assert deployments(pipeline) == []


def test_get_only_renders_confirmation_and_does_not_decide(pipeline):
    run_id = started_run(pipeline)
    resp = click(pipeline, "approve", method="GET")
    assert resp["statusCode"] == 200
    assert "<form method='post'>" in resp["body"]
    assert stage(read_record(pipeline["s3"], run_id), "approval")["status"] == "running"
    assert deployments(pipeline) == []


def test_approve_starts_deploy_and_records_decision(pipeline):
    run_id = started_run(pipeline)
    pipeline["clock"].advance(120)
    resp = click(pipeline, "approve")
    assert resp["statusCode"] == 200
    rec = read_record(pipeline["s3"], run_id)
    st = stage(rec, "approval")
    assert st["status"] == "succeeded"
    assert st["outcome"] == "approved"
    assert st["details"]["decision"] == "approve"
    assert st["details"]["decided_at"] == "2026-11-24T12:02:00+00:00"
    assert st["details"]["decided_by"] == "203.0.113.7"
    assert stage(rec, "deploy")["status"] == "running"
    assert len(deployments(pipeline)) == 1


def test_reject_ends_run_without_deploy(pipeline):
    run_id = started_run(pipeline)
    resp = click(pipeline, "reject")
    assert resp["statusCode"] == 200
    rec = read_record(pipeline["s3"], run_id)
    assert rec["status"] == "failed"
    assert stage(rec, "approval")["outcome"] == "rejected"
    assert stage(rec, "approval")["details"]["decision"] == "reject"
    assert stage(rec, "deploy")["status"] == "skipped"
    assert deployments(pipeline) == []


def test_token_is_one_time(pipeline):
    started_run(pipeline)
    assert click(pipeline, "approve")["statusCode"] == 200
    again = click(pipeline, "approve")
    assert again["statusCode"] == 409
    assert click(pipeline, "reject")["statusCode"] == 409
    assert len(deployments(pipeline)) == 1


def test_wrong_token_is_refused(pipeline):
    run_id = started_run(pipeline)
    resp = click(pipeline, "approve", token="forged")
    assert resp["statusCode"] == 403
    assert stage(read_record(pipeline["s3"], run_id), "approval")["status"] == "running"
    assert deployments(pipeline) == []


def test_unknown_run_and_bad_decision(pipeline):
    started_run(pipeline)
    assert click(pipeline, "approve", run_id="nope")["statusCode"] == 404
    assert click(pipeline, "approve", decision="maybe")["statusCode"] == 400
    assert click(pipeline, "approve", method="GET", decision="maybe")["statusCode"] == 400


def test_click_after_deadline_is_timeout_reject(pipeline):
    run_id = started_run(pipeline)
    pipeline["clock"].advance(pipeline["cfg"].approval.timeout_seconds)
    resp = click(pipeline, "approve")
    assert resp["statusCode"] == 410
    rec = read_record(pipeline["s3"], run_id)
    assert rec["status"] == "failed"
    assert stage(rec, "approval")["outcome"] == "timeout"
    assert deployments(pipeline) == []


def test_scheduled_sweep_rejects_expired_approvals_only(pipeline):
    orc, clock = pipeline["orc"], pipeline["clock"]
    old = started_run(pipeline)
    clock.advance(pipeline["cfg"].approval.timeout_seconds - 60)
    assert orc.handle(scheduled_event())["expired"] == []
    fresh = orc.handle(push_event(event_id="evt-2", commit="f" * 40))["run_id"]
    build_succeeds(pipeline, fresh)

    clock.advance(60)
    out = orc.handle(scheduled_event())
    assert out == {"action": "approvals_swept", "expired": [old]}
    rec = read_record(pipeline["s3"], old)
    assert rec["status"] == "failed"
    assert stage(rec, "approval")["outcome"] == "timeout"
    assert stage(rec, "approval")["details"]["decision"] == "timeout"
    assert stage(read_record(pipeline["s3"], fresh), "approval")["status"] == "running"
    # A sweep after the decision is a no-op, and the old link no longer works.
    assert orc.handle(scheduled_event())["expired"] == []
    assert deployments(pipeline) == []


def test_post_with_base64_body(pipeline):
    import base64

    started_run(pipeline)
    _url, params = email_links(pipeline)["approve"]
    event = url_event("POST", params)
    event["body"] = base64.b64encode(event["body"].encode()).decode()
    event["isBase64Encoded"] = True
    assert approval.handle(event, pipeline["orc"])["statusCode"] == 200


def test_approval_lambda_entrypoint(pipeline):
    run_id = started_run(pipeline)
    _url, params = email_links(pipeline)["reject"]
    resp = approval.lambda_handler(url_event("POST", params), None)
    assert resp["statusCode"] == 200
    assert read_record(pipeline["s3"], run_id)["status"] == "failed"


def test_timeout_is_a_labelled_config_placeholder():
    cfg = load_config(ROOT / "orchestrator" / "config.json")
    assert cfg.approval.timeout_seconds > 0
    assert "PLACEHOLDER" in cfg.raw["approval"]["timeout_seconds_note"]


@pytest.fixture
def template():
    from cfnlint.decode import decode

    tpl, errors = decode(str(ROOT / "orchestrator" / "template.yaml"))
    assert not errors
    return tpl["Resources"]


def test_template_has_function_url_topic_and_sweep(template):
    by_type = {}
    for name, res in template.items():
        by_type.setdefault(res["Type"], []).append((name, res))
    [(_n, url)] = by_type["AWS::Lambda::Url"]
    assert url["Properties"]["AuthType"] == "NONE"
    assert url["Properties"]["TargetFunctionArn"] == {"Ref": "ApprovalFunction"}
    assert template["ApprovalFunction"]["Properties"]["Handler"] == (
        "orchestrator.approval.lambda_handler"
    )
    assert by_type["AWS::SNS::Topic"]
    [(_n, sub)] = by_type["AWS::SNS::Subscription"]
    assert sub["Properties"]["Protocol"] == "email"
    sweep = [r for _n, r in by_type["AWS::Events::Rule"] if "ScheduleExpression" in r["Properties"]]
    assert len(sweep) == 1
    # Public invoke is allowed only through the function URL (NFR-06).
    url_perms = [
        r["Properties"]
        for _n, r in by_type["AWS::Lambda::Permission"]
        if r["Properties"].get("FunctionUrlAuthType") == "NONE"
    ]
    assert url_perms == [
        {
            "Action": "lambda:InvokeFunctionUrl",
            "FunctionName": {"Ref": "ApprovalFunction"},
            "FunctionUrlAuthType": "NONE",
            "Principal": "*",
        }
    ]
