"""Lambda orchestrator (FR-15), standing in for CodePipeline (ADR-007).

Event-driven: every invocation handles one EventBridge event, makes at most one stage
transition (one AWS start call plus a run-record write) and returns. Nothing waits on SSM or
CodeDeploy; their status-change events invoke the function again.

  CodeCommit push to the configured branch  -> new run, source done, build started (SSM)
  SSM command status (terminal)             -> build done; on success approval requested (SNS)
  approval decision (orchestrator/approval) -> approve starts deploy; reject ends the run
  scheduled sweep (EventBridge rate rule)   -> approvals past their deadline are rejected
  CodeDeploy deployment state (terminal)    -> deploy done, run finished
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import boto3

from orchestrator import run_record as rr
from orchestrator.config import Config, load_config

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

PUSH_DETAIL_TYPE = "CodeCommit Repository State Change"
SSM_DETAIL_TYPE = "EC2 Command Status-change Notification"
DEPLOY_DETAIL_TYPE = "CodeDeploy Deployment State-change Notification"
SCHEDULED_DETAIL_TYPE = "Scheduled Event"

PUSH_EVENTS = {"referenceCreated", "referenceUpdated"}
SSM_SUCCESS = {"Success"}
SSM_FAILURE = {"Failed", "TimedOut", "Cancelled"}
DEPLOY_SUCCESS = {"SUCCESS"}
DEPLOY_FAILURE = {"FAILURE", "STOP"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Orchestrator:
    STAGES = ["source", "build", "approval", "deploy"]

    def __init__(
        self,
        config: Config,
        *,
        clock: Callable[[], datetime] = _utcnow,
        s3=None,
        ssm=None,
        codedeploy=None,
        sns=None,
    ):
        self.cfg = config
        self.clock = clock
        self.s3 = s3 or boto3.client("s3", region_name=config.region)
        self.ssm = ssm or boto3.client("ssm", region_name=config.region)
        self.codedeploy = codedeploy or boto3.client("codedeploy", region_name=config.region)
        self.sns = sns or boto3.client("sns", region_name=config.region)
        self.store = rr.RunStore(self.s3, config.run_bucket, config.run_prefix)

    # ---- dispatch -------------------------------------------------------------------------

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        source = event.get("source")
        detail_type = event.get("detail-type")
        if source == "aws.codecommit" and detail_type == PUSH_DETAIL_TYPE:
            result = self.on_push(event)
        elif source == "aws.ssm" and detail_type == SSM_DETAIL_TYPE:
            result = self.on_ssm_status(event)
        elif source == "aws.codedeploy" and detail_type == DEPLOY_DETAIL_TYPE:
            result = self.on_deploy_state(event)
        elif source == "aws.events" and detail_type == SCHEDULED_DETAIL_TYPE:
            result = self.sweep_approvals()
        else:
            result = _ignored(f"unhandled event {source}/{detail_type}")
        log.info(json.dumps({"event_id": event.get("id"), **result}))
        return result

    # ---- source -> build ------------------------------------------------------------------

    def on_push(self, event: dict[str, Any]) -> dict[str, Any]:
        d = event.get("detail", {})
        if d.get("event") not in PUSH_EVENTS:
            return _ignored(f"codecommit event {d.get('event')}")
        if d.get("referenceType") != "branch" or d.get("referenceName") != self.cfg.branch:
            return _ignored(f"ref {d.get('referenceType')}/{d.get('referenceName')}")
        event_id = event.get("id") or d["commitId"]
        existing = self.store.lookup("event", event_id)
        if existing:
            return _ignored("duplicate push event", existing)

        now = self.clock()
        run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{d['commitId'][:8]}"
        trigger = {
            "repository": d.get("repositoryName"),
            "branch": d["referenceName"],
            "commit_id": d["commitId"],
            "event_id": event_id,
            "event_time": event.get("time"),
        }
        record = rr.new_record(run_id, trigger, self.STAGES, now)
        rr.start_stage(record, "source", now)
        rr.end_stage(record, "source", "succeeded", f"push {d['commitId']}", now)
        self._start_build(record)
        # Indexed last so a failed start can be retried by EventBridge redelivery.
        self.store.put_index("event", event_id, run_id)
        return {"action": "run_started", "run_id": run_id}

    def _start_build(self, record: dict[str, Any]) -> None:
        now = self.clock()
        run_id = record["run_id"]
        fields = self._template_fields(record)
        commands = [c.format(**fields) for c in self.cfg.build.commands]
        log_prefix = self.store.run_key_prefix(run_id, "build-log/")
        resp = self.ssm.send_command(
            InstanceIds=self.cfg.build.instance_ids,
            DocumentName=self.cfg.build.document,
            Comment=f"driftgate build {run_id}"[:100],
            Parameters={
                "commands": commands,
                "workingDirectory": [self.cfg.build.working_directory],
            },
            OutputS3BucketName=self.cfg.run_bucket,
            OutputS3KeyPrefix=log_prefix,
        )
        command_id = resp["Command"]["CommandId"]
        self.store.put_index("ssm", command_id, run_id)
        rr.start_stage(
            record,
            "build",
            now,
            {
                "command_id": command_id,
                "instance_ids": list(self.cfg.build.instance_ids),
                "log_uri": self.store.run_uri(run_id, "build-log/"),
            },
        )
        self.store.save(record)

    # ---- build -> next stage --------------------------------------------------------------

    def on_ssm_status(self, event: dict[str, Any]) -> dict[str, Any]:
        d = event.get("detail", {})
        status = d.get("status")
        if status not in SSM_SUCCESS | SSM_FAILURE:
            return _ignored(f"ssm status {status}")
        record = self._record_for("ssm", d.get("command-id"))
        if record is None:
            return _ignored("unknown ssm command")
        if rr.get_stage(record, "build")["status"] != "running":
            return _ignored("build already finished", record["run_id"])
        now = self.clock()
        if status in SSM_FAILURE:
            rr.end_stage(record, "build", "failed", status, now)
            return self._finish(record, "failed")
        rr.end_stage(record, "build", "succeeded", status, now)
        return self.after_build(record)

    def after_build(self, record: dict[str, Any]) -> dict[str, Any]:
        """Transition taken when the build succeeds."""
        return self.request_approval(record)

    # ---- approval (FR-27, ADR-010) ----------------------------------------------------------

    def request_approval(self, record: dict[str, Any]) -> dict[str, Any]:
        """Publish approve/reject links carrying a one-time token; only its hash is stored."""
        now = self.clock()
        ac = self.cfg.approval
        run_id = record["run_id"]
        token = secrets.token_urlsafe(32)
        deadline = now + timedelta(seconds=ac.timeout_seconds)
        links = {d: approval_link(ac.function_url, run_id, d, token) for d in DECISIONS}
        trigger = record["trigger"]
        message = "\n".join(
            [
                f"DriftGate run {run_id} passed the build stage and is waiting for approval.",
                "",
                f"Repository: {trigger.get('repository')}",
                f"Branch:     {trigger.get('branch')}",
                f"Commit:     {trigger.get('commit_id')}",
                f"Run record: {self.store.run_uri(run_id, 'run.json')}",
                "",
                f"Approve: {links['approve']}",
                f"Reject:  {links['reject']}",
                "",
                f"No decision by {rr.iso(deadline)} (UTC) means reject.",
                "Each link opens a confirmation page; the token works once.",
            ]
        )
        resp = self.sns.publish(
            TopicArn=ac.topic_arn,
            Subject=f"DriftGate approval: {run_id}"[:100],
            Message=message,
        )
        rr.start_stage(
            record,
            "approval",
            now,
            {
                "token_sha256": _token_hash(token),
                "deadline": rr.iso(deadline),
                "timeout_seconds": ac.timeout_seconds,
                "sns_message_id": resp["MessageId"],
            },
        )
        self.store.put_index("approval-pending", run_id, run_id)
        self.store.save(record)
        return {"action": "approval_requested", "run_id": run_id}

    def decide(
        self, run_id: str, decision: str, token: str, actor: str | None = None
    ) -> dict[str, Any]:
        """Apply an approve/reject click. Returns {"result": ..., "run_id": ...}.

        result is one of: approved, rejected, timeout, unknown_run, not_pending,
        bad_token, bad_decision.
        """
        if decision not in DECISIONS:
            return {"result": "bad_decision", "run_id": run_id}
        record = self.store.load(run_id) if run_id else None
        if record is None:
            return {"result": "unknown_run", "run_id": run_id}
        stage = rr.get_stage(record, "approval")
        expected = stage["details"].get("token_sha256")
        if stage["status"] != "running" or not expected:
            return {"result": "not_pending", "run_id": run_id}
        if not token or not hmac.compare_digest(expected, _token_hash(token)):
            return {"result": "bad_token", "run_id": run_id}
        now = self.clock()
        if now >= datetime.fromisoformat(stage["details"]["deadline"]):
            self._end_approval(record, "failed", "timeout", now, {})
            self._finish(record, "failed")
            return {"result": "timeout", "run_id": run_id}
        details = {"decision": decision, "decided_at": rr.iso(now), "decided_by": actor}
        if decision == "reject":
            self._end_approval(record, "failed", "rejected", now, details)
            self._finish(record, "failed")
            return {"result": "rejected", "run_id": run_id}
        self._end_approval(record, "succeeded", "approved", now, details)
        self.start_deploy(record)
        return {"result": "approved", "run_id": run_id}

    def sweep_approvals(self) -> dict[str, Any]:
        """Reject every pending approval whose deadline has passed (FR-27 timeout)."""
        now = self.clock()
        expired = []
        for run_id in self.store.list_index("approval-pending"):
            record = self.store.load(run_id)
            stage = rr.get_stage(record, "approval") if record else None
            if stage is None or stage["status"] != "running":
                self.store.delete_index("approval-pending", run_id)
                continue
            if now >= datetime.fromisoformat(stage["details"]["deadline"]):
                self._end_approval(record, "failed", "timeout", now, {"decision": "timeout"})
                self._finish(record, "failed")
                expired.append(run_id)
        return {"action": "approvals_swept", "expired": expired}

    def _end_approval(
        self,
        record: dict[str, Any],
        status: str,
        outcome: str,
        now: datetime,
        details: dict[str, Any],
    ) -> None:
        # The token is consumed: drop its hash so no second click can match.
        rr.get_stage(record, "approval")["details"].pop("token_sha256", None)
        rr.end_stage(record, "approval", status, outcome, now, details)
        self.store.delete_index("approval-pending", record["run_id"])

    # ---- deploy ---------------------------------------------------------------------------

    def start_deploy(self, record: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        fields = self._template_fields(record)
        dc = self.cfg.deploy
        resp = self.codedeploy.create_deployment(
            applicationName=dc.application_name,
            deploymentGroupName=dc.deployment_group_name,
            description=f"driftgate run {record['run_id']}",
            revision={
                "revisionType": "S3",
                "s3Location": {
                    "bucket": fields["bundle_bucket"],
                    "key": fields["bundle_key"],
                    "bundleType": dc.bundle_type,
                },
            },
        )
        deployment_id = resp["deploymentId"]
        self.store.put_index("codedeploy", deployment_id, record["run_id"])
        rr.start_stage(
            record,
            "deploy",
            now,
            {
                "deployment_id": deployment_id,
                "bundle_uri": f"s3://{fields['bundle_bucket']}/{fields['bundle_key']}",
            },
        )
        self.store.save(record)
        return {"action": "deploy_started", "run_id": record["run_id"]}

    def on_deploy_state(self, event: dict[str, Any]) -> dict[str, Any]:
        d = event.get("detail", {})
        state = d.get("state")
        if state not in DEPLOY_SUCCESS | DEPLOY_FAILURE:
            return _ignored(f"deployment state {state}")
        record = self._record_for("codedeploy", d.get("deploymentId"))
        if record is None:
            return _ignored("unknown deployment")
        if rr.get_stage(record, "deploy")["status"] != "running":
            return _ignored("deploy already finished", record["run_id"])
        ok = state in DEPLOY_SUCCESS
        rr.end_stage(record, "deploy", "succeeded" if ok else "failed", state, self.clock())
        return self._finish(record, "succeeded" if ok else "failed")

    # ---- helpers --------------------------------------------------------------------------

    def _finish(self, record: dict[str, Any], status: str) -> dict[str, Any]:
        rr.finish_run(record, status, self.clock())
        self.store.save(record)
        return {"action": "run_finished", "run_id": record["run_id"], "status": status}

    def _record_for(self, kind: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        run_id = self.store.lookup(kind, key)
        return self.store.load(run_id) if run_id else None

    def _template_fields(self, record: dict[str, Any]) -> dict[str, str]:
        commit_id = record["trigger"]["commit_id"]
        run_id = record["run_id"]
        return {
            "commit_id": commit_id,
            "run_id": run_id,
            "bundle_bucket": self.cfg.deploy.bundle_bucket,
            "bundle_key": self.cfg.deploy.bundle_key.format(commit_id=commit_id, run_id=run_id),
            "run_bucket": self.cfg.run_bucket,
        }


DECISIONS = ("approve", "reject")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def approval_link(function_url: str, run_id: str, decision: str, token: str) -> str:
    query = urlencode({"run_id": run_id, "decision": decision, "token": token})
    return f"{function_url.rstrip('/')}/?{query}"


def _ignored(reason: str, run_id: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"action": "ignored", "reason": reason}
    if run_id:
        out["run_id"] = run_id
    return out


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return Orchestrator(load_config()).handle(event)
