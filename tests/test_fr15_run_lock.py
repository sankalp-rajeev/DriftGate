"""FR-15 / PRD 6.8: one active run at a time.

The lock is `runs/active.json`, created with an S3 conditional put (If-None-Match: *); moto
5.2.5 models it (a second conditional put returns 412 PreconditionFailed). A push during an
active run queues as "latest"; a newer push replaces the queued one; when the run ends, for
any reason, the lock is released and the queued push starts.
"""

import json

import boto3
import pytest
from botocore.exceptions import ClientError
from orchestrator_helpers import (
    BUCKET,
    COMMIT,
    REGION,
    approve_latest,
    build_succeeds,
    deploy_event,
    host_comes_up,
    links_from,
    push_event,
    read_record,
    ssm_event,
    stage,
    url_event,
)

from orchestrator import approval

C2 = "2" * 40
C3 = "3" * 40


def lock(pipeline):
    try:
        obj = pipeline["s3"].get_object(Bucket=BUCKET, Key="runs/active.json")
    except pipeline["s3"].exceptions.NoSuchKey:
        return None
    return json.loads(obj["Body"].read())


def queued(pipeline):
    return pipeline["orc"].store.queued()


def ssm_commands():
    return boto3.client("ssm", region_name=REGION).list_commands()["Commands"]


def first_run(pipeline):
    out = pipeline["orc"].handle(push_event())
    assert out["action"] == "run_started"
    return out["run_id"]


def test_first_push_acquires_lock(pipeline):
    run_id = first_run(pipeline)
    held = lock(pipeline)
    assert held["run_id"] == run_id
    assert held["acquired_at"] == "2026-11-24T12:00:00+00:00"


def test_lock_is_a_conditional_create(pipeline):
    store = pipeline["orc"].store
    assert store.acquire_lock("run-a", pipeline["clock"]())
    assert not store.acquire_lock("run-b", pipeline["clock"]())
    assert store.lock_holder() == "run-a"


def test_lock_released_only_by_its_holder(pipeline):
    store = pipeline["orc"].store
    store.acquire_lock("run-a", pipeline["clock"]())
    assert not store.release_lock("run-b")
    assert store.lock_holder() == "run-a"
    assert store.release_lock("run-a")
    assert store.lock_holder() is None


def test_second_push_is_queued_not_started(pipeline):
    run_id = first_run(pipeline)
    out = pipeline["orc"].handle(
        push_event(commit=C2, event_id="evt-2", time="2026-11-24T12:01:00Z")
    )
    assert out == {"action": "queued", "commit_id": C2, "active_run_id": run_id}
    assert queued(pipeline)["trigger"]["commit_id"] == C2
    assert len(ssm_commands()) == 1
    assert lock(pipeline)["run_id"] == run_id


def test_third_push_replaces_the_queued_one(pipeline):
    first_run(pipeline)
    orc = pipeline["orc"]
    orc.handle(push_event(commit=C2, event_id="evt-2", time="2026-11-24T12:01:00Z"))
    out = orc.handle(push_event(commit=C3, event_id="evt-3", time="2026-11-24T12:02:00Z"))
    assert out["action"] == "queued"
    assert out["replaced_commit_id"] == C2
    assert queued(pipeline)["trigger"]["commit_id"] == C3
    assert len(ssm_commands()) == 1


def test_late_delivered_older_push_does_not_replace_newer(pipeline):
    first_run(pipeline)
    orc = pipeline["orc"]
    orc.handle(push_event(commit=C3, event_id="evt-3", time="2026-11-24T12:02:00Z"))
    out = orc.handle(push_event(commit=C2, event_id="evt-2", time="2026-11-24T12:01:00Z"))
    assert out["action"] == "ignored"
    assert queued(pipeline)["trigger"]["commit_id"] == C3


def _queue_two(pipeline):
    orc = pipeline["orc"]
    orc.handle(push_event(commit=C2, event_id="evt-2", time="2026-11-24T12:01:00Z"))
    orc.handle(push_event(commit=C3, event_id="evt-3", time="2026-11-24T12:02:00Z"))


def _assert_queued_run_started(pipeline, finished_run, out):
    nxt = out["next"]
    assert nxt["action"] == "run_started"
    assert nxt["run_id"] != finished_run
    assert lock(pipeline)["run_id"] == nxt["run_id"]
    assert queued(pipeline) is None
    rec = read_record(pipeline["s3"], nxt["run_id"])
    assert rec["trigger"]["commit_id"] == C3
    build = stage(rec, "build")
    assert build["status"] == "running"
    # The first build stopped the host, so the queued run wakes it and waits for it.
    assert build["details"]["waiting_for_host"] is True
    assert build["details"]["started_host"] is True
    assert host_comes_up(pipeline)["action"] == "build_started"
    assert len(ssm_commands()) == 2


def test_release_on_build_failure_starts_queued_run(pipeline):
    run_id = first_run(pipeline)
    _queue_two(pipeline)
    pipeline["clock"].advance(60)
    command_id = stage(read_record(pipeline["s3"], run_id), "build")["details"]["command_id"]
    out = pipeline["orc"].handle(ssm_event(command_id, "Failed"))
    assert out["status"] == "failed"
    _assert_queued_run_started(pipeline, run_id, out)


def test_release_on_reject_starts_queued_run(pipeline):
    run_id = first_run(pipeline)
    build_succeeds(pipeline, run_id)
    _queue_two(pipeline)
    pipeline["clock"].advance(60)
    out = approve_latest(pipeline, run_id, decision="reject")
    assert out["result"] == "rejected"
    holder = lock(pipeline)["run_id"]
    assert holder != run_id
    assert read_record(pipeline["s3"], holder)["trigger"]["commit_id"] == C3


def test_release_on_deploy_success_starts_queued_run(pipeline):
    run_id = first_run(pipeline)
    build_succeeds(pipeline, run_id)
    approve_latest(pipeline, run_id)
    _queue_two(pipeline)
    pipeline["clock"].advance(60)
    deployment_id = stage(read_record(pipeline["s3"], run_id), "deploy")["details"]["deployment_id"]
    out = pipeline["orc"].handle(deploy_event(deployment_id, "SUCCESS"))
    assert out["status"] == "succeeded"
    _assert_queued_run_started(pipeline, run_id, out)


def test_run_end_without_queue_leaves_no_lock(pipeline):
    run_id = first_run(pipeline)
    command_id = stage(read_record(pipeline["s3"], run_id), "build")["details"]["command_id"]
    out = pipeline["orc"].handle(ssm_event(command_id, "Failed"))
    assert "next" not in out
    assert lock(pipeline) is None
    # The next push starts straight away.
    assert pipeline["orc"].handle(push_event(commit=C2, event_id="evt-2"))["action"] == (
        "run_started"
    )


def test_failed_build_start_releases_lock(pipeline, monkeypatch):
    def refuse(**_kwargs):
        raise ClientError({"Error": {"Code": "InvalidInstanceId", "Message": "x"}}, "SendCommand")

    monkeypatch.setattr(pipeline["orc"].ssm, "send_command", refuse)
    with pytest.raises(ClientError):
        pipeline["orc"].handle(push_event())
    assert lock(pipeline) is None


class RejectingCodeDeploy:
    def create_deployment(self, **_kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "DeploymentGroupAlreadyHasActiveDeployment",
                    "Message": "The deployment group already has an active deployment.",
                }
            },
            "CreateDeployment",
        )


def test_codedeploy_rejecting_a_deployment_ends_run_and_releases_lock(pipeline):
    orc = pipeline["orc"]
    orc.codedeploy = RejectingCodeDeploy()
    run_id = first_run(pipeline)
    build_succeeds(pipeline, run_id)
    _queue_two(pipeline)
    _url, params = links_from(pipeline["sns"].published[-1]["Message"])["approve"]
    resp = approval.handle(url_event("POST", params), orc)
    assert resp["statusCode"] == 503
    rec = read_record(pipeline["s3"], run_id)
    assert rec["status"] == "failed"
    assert stage(rec, "approval")["outcome"] == "approved"
    dep = stage(rec, "deploy")
    assert dep["status"] == "failed"
    assert dep["outcome"] == "DeploymentGroupAlreadyHasActiveDeployment"
    assert "active deployment" in dep["details"]["error"]
    holder = lock(pipeline)["run_id"]
    assert holder != run_id
    assert read_record(pipeline["s3"], holder)["trigger"]["commit_id"] == C3


def test_build_command_is_exactly_prd_6_8(pipeline):
    run_id = first_run(pipeline)
    [cmd] = ssm_commands()
    assert cmd["Parameters"]["commands"] == [
        f"cd /opt/driftgate && git fetch && git checkout {COMMIT} && "
        f"deploy/build.sh --commit {COMMIT} --run-id {run_id}"
    ]


# ---- stale lock (PRD 6.8): expires_at = start + 2 h; an expired lock is taken over ----------


def test_lock_records_expiry_two_hours_after_start(pipeline):
    first_run(pipeline)
    held = lock(pipeline)
    assert held["expires_at"] == "2026-11-24T14:00:00+00:00"
    assert pipeline["cfg"].lock_ttl_seconds == 7200


def test_unexpired_lock_is_not_taken_over(pipeline):
    run_id = first_run(pipeline)
    pipeline["clock"].advance(7200 - 1)
    out = pipeline["orc"].handle(push_event(commit=C2, event_id="evt-2"))
    assert out["action"] == "queued"
    assert lock(pipeline)["run_id"] == run_id


def test_expired_lock_is_taken_over_and_old_run_abandoned(pipeline):
    old = first_run(pipeline)
    pipeline["clock"].advance(7200)
    out = pipeline["orc"].handle(push_event(commit=C2, event_id="evt-2"))
    assert out["action"] == "run_started"
    assert out["abandoned_run_id"] == old
    assert lock(pipeline)["run_id"] == out["run_id"]
    rec = read_record(pipeline["s3"], old)
    assert rec["status"] == "abandoned"
    assert rec["ended_at"] == "2026-11-24T14:00:00+00:00"
    assert "stale lock" in rec["abandoned_reason"]
    assert stage(rec, "build")["status"] == "abandoned"
    assert stage(rec, "deploy")["status"] == "skipped"


def test_late_events_for_an_abandoned_run_are_ignored(pipeline):
    old = first_run(pipeline)
    command_id = stage(read_record(pipeline["s3"], old), "build")["details"]["command_id"]
    pipeline["clock"].advance(7200)
    pipeline["orc"].handle(push_event(commit=C2, event_id="evt-2"))
    assert pipeline["orc"].handle(ssm_event(command_id, "Success"))["action"] == "ignored"
    assert read_record(pipeline["s3"], old)["status"] == "abandoned"


def test_abandoned_pending_approval_is_dropped(pipeline):
    old = first_run(pipeline)
    build_succeeds(pipeline, old)
    pipeline["clock"].advance(7200)
    pipeline["orc"].handle(push_event(commit=C2, event_id="evt-2"))
    assert pipeline["orc"].store.list_index("approval-pending") == []
    assert approve_latest(pipeline, old)["result"] == "not_pending"


def test_stale_takeover_is_conditional_on_the_etag(pipeline):
    """Two runs racing for one stale lock: only the first If-Match put wins."""
    store = pipeline["orc"].store
    now = pipeline["clock"]()
    assert store.acquire_lock("run-old", now)
    pipeline["clock"].advance(7200)
    later = pipeline["clock"]()
    obj = pipeline["s3"].get_object(Bucket=BUCKET, Key="runs/active.json")
    first = store.acquire_lock("run-a", later)
    assert first and first.stale["run_id"] == "run-old"
    # A second contender holding the old ETag loses.
    assert not store._put_lock({"run_id": "run-b"}, IfMatch=obj["ETag"])
    assert store.lock_holder() == "run-a"


def test_unlock_clears_lock_and_abandons_holder(pipeline):
    from orchestrator.unlock import unlock

    run_id = first_run(pipeline)
    out = unlock(pipeline["orc"].store, pipeline["clock"]())
    assert out == {
        "unlocked": True,
        "run_id": run_id,
        "abandoned": True,
        "at": "2026-11-24T12:00:00+00:00",
    }
    assert lock(pipeline) is None
    rec = read_record(pipeline["s3"], run_id)
    assert rec["status"] == "abandoned"
    assert rec["abandoned_reason"] == "manual unlock (make unlock)"
    assert pipeline["orc"].handle(push_event(commit=C2, event_id="evt-2"))["action"] == (
        "run_started"
    )


def test_unlock_without_lock_is_a_no_op(pipeline):
    from orchestrator.unlock import unlock

    assert unlock(pipeline["orc"].store, pipeline["clock"]()) == {
        "unlocked": False,
        "reason": "no lock",
    }


def test_make_unlock_target_runs_the_unlock_module():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "Makefile").read_text()
    assert "unlock:" in text
    assert "python -m orchestrator.unlock --bucket $(BUCKET)" in text
