"""Lambda orchestrator (FR-15), standing in for CodePipeline (ADR-007).

Event-driven: every invocation handles one EventBridge event, makes at most one stage
transition (one AWS start call plus a run-record write) and returns. Nothing waits on SSM or
CodeDeploy; their status-change events invoke the function again.

  CodeCommit push to the configured branch  -> new run, source done, build stage started;
                                               if a run is active, record rejected-busy
  build stage start                         -> build host (tag Role=build-host) started if
                                               stopped; the build command is sent (SSM) once
                                               both its EC2 "running" state-change event and
                                               SSM online status have arrived
  EC2 instance state change (build host)    -> records "running"; starts the host if it was
                                               stopping when the build needed it
  SSM command status (terminal)             -> build done, build host idle timer starts;
                                               on success approval requested (SNS)
  approval decision (orchestrator/approval) -> approve starts deploy; reject ends the run
  scheduled sweep (EventBridge rate rule)   -> approvals past their deadline are rejected;
                                               a waiting build re-checks SSM online status; an
                                               idle build host is stopped at the configured limit
  CodeDeploy deployment state (terminal)    -> deploy done, run finished

One active run at a time (PRD 6.8): `runs/active.json` is created with a conditional put and
deleted when the run ends. A lock past its `expires_at` (2 h) is stale: the next push takes
it over and the old run is marked abandoned.
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
from botocore.exceptions import ClientError

from orchestrator import run_record as rr
from orchestrator.config import Config, load_config

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

PUSH_DETAIL_TYPE = "CodeCommit Repository State Change"
SSM_DETAIL_TYPE = "EC2 Command Status-change Notification"
DEPLOY_DETAIL_TYPE = "CodeDeploy Deployment State-change Notification"
EC2_STATE_DETAIL_TYPE = "EC2 Instance State-change Notification"
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
        ec2=None,
    ):
        self.cfg = config
        self.clock = clock
        self.s3 = s3 or boto3.client("s3", region_name=config.region)
        self.ssm = ssm or boto3.client("ssm", region_name=config.region)
        self.codedeploy = codedeploy or boto3.client("codedeploy", region_name=config.region)
        self.sns = sns or boto3.client("sns", region_name=config.region)
        self.ec2 = ec2 or boto3.client("ec2", region_name=config.region)
        self.store = rr.RunStore(
            self.s3, config.run_bucket, config.run_prefix, config.lock_ttl_seconds
        )

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
        elif source == "aws.ec2" and detail_type == EC2_STATE_DETAIL_TYPE:
            result = self.on_instance_state(event)
        elif source == "aws.events" and detail_type == SCHEDULED_DETAIL_TYPE:
            result = {
                "action": "swept",
                "expired": self.sweep_approvals()["expired"],
                "build_host": self.sweep_build_host(),
            }
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

        trigger = {
            "repository": d.get("repositoryName"),
            "branch": d["referenceName"],
            "commit_id": d["commitId"],
            "event_id": event_id,
            "event_time": event.get("time"),
        }
        now = self.clock()
        run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{trigger['commit_id'][:8]}"
        lock = self.store.acquire_lock(run_id, now, event_id=event_id)
        if not lock:
            if lock.event_id == event_id:
                return _ignored("duplicate push event", lock.held_by)
            return self._reject_busy(trigger, run_id, now, lock.held_by)
        return self._start_run(trigger, run_id, now, lock)

    def _start_run(
        self, trigger: dict[str, Any], run_id: str, now: datetime, lock: rr.LockResult
    ) -> dict[str, Any]:
        out: dict[str, Any] = {"action": "run_started", "run_id": run_id}
        try:
            if lock.stale:
                stale_id = lock.stale["run_id"]
                reason = f"stale lock (expired {rr.iso(self.store.lock_expires_at(lock.stale))})"
                self.store.abandon(stale_id, reason, now)
                out["abandoned_run_id"] = stale_id
            existing = self.store.lookup("event", trigger["event_id"])
            if existing:
                self.store.release_lock(run_id)
                return _ignored("duplicate push event", existing)
            record = rr.new_record(run_id, trigger, self.STAGES, now)
            rr.start_stage(record, "source", now)
            rr.end_stage(record, "source", "succeeded", f"push {trigger['commit_id']}", now)
            out["build"] = self._start_build(record)
        except Exception:
            self.store.release_lock(run_id)
            raise
        # Indexed last so a failed start can be retried by EventBridge redelivery.
        self.store.put_index("event", trigger["event_id"], run_id)
        return out

    def _reject_busy(
        self, trigger: dict[str, Any], run_id: str, now: datetime, holder: str | None
    ) -> dict[str, Any]:
        event_id = trigger["event_id"]
        existing = self.store.lookup("event", event_id)
        if existing:
            return _ignored("duplicate push event", existing)
        # A distinct event can name the same commit in the same second as the active run.
        suffix = hashlib.sha256(event_id.encode()).hexdigest()[:12]
        run_id = f"{run_id}-busy-{suffix}"
        record = rr.new_record(run_id, trigger, self.STAGES, now)
        rr.end_stage(record, "source", "failed", "rejected-busy", now, {"active_run_id": holder})
        rr.finish_run(record, "rejected-busy", now)
        self.store.save(record)
        self.store.put_index("event", event_id, run_id)
        return {
            "action": "rejected-busy",
            "run_id": run_id,
            "commit_id": trigger["commit_id"],
            "active_run_id": holder,
        }

    # ---- build host (PRD 6.8) -----------------------------------------------------------

    def _build_host(self) -> dict[str, Any] | None:
        """The (non-terminated) instance tagged with the build host tag, or None."""
        tag = self.cfg.build.host_tag
        resp = self.ec2.describe_instances(
            Filters=[
                {"Name": f"tag:{tag['key']}", "Values": [tag["value"]]},
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )
        found = [i for r in resp["Reservations"] for i in r["Instances"]]
        return sorted(found, key=lambda i: i["InstanceId"])[0] if found else None

    def _ssm_online(self, instance_id: str) -> bool:
        resp = self.ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        return any(i.get("PingStatus") == "Online" for i in resp["InstanceInformationList"])

    def _start_build(self, record: dict[str, Any]) -> dict[str, Any]:
        """Begin the build stage: send the build now if the host is ready, else wake it."""
        now = self.clock()
        host = self._build_host()
        if host is None:
            rr.start_stage(record, "build", now)
            rr.end_stage(record, "build", "failed", "no build host", now)
            return self._finish(record, "failed")
        instance_id = host["InstanceId"]
        state = host["State"]["Name"]
        wait = {
            "instance_id": instance_id,
            "host_state_at_start": state,
            "ec2_running": state == "running",
            "ssm_online": False,
            "started_host": False,
            "start_when_stopped": state == "stopping",
        }
        if state == "stopped":
            self.ec2.start_instances(InstanceIds=[instance_id])
            wait["started_host"] = True
        rr.start_stage(record, "build", now, {**wait, "waiting_for_host": True})
        self.store.put_index("host-wait", instance_id, record["run_id"])
        return self._try_send_build(record)

    def _try_send_build(self, record: dict[str, Any]) -> dict[str, Any]:
        """Send the build command once both readiness signals are in; otherwise keep waiting."""
        details = rr.get_stage(record, "build")["details"]
        instance_id = details["instance_id"]
        if details["ec2_running"] and self._ssm_online(instance_id):
            details["ssm_online"] = True
            details["host_ready_at"] = rr.iso(self.clock())
            self.store.delete_index("host-wait", instance_id)
            return self._reference_or_build(record, instance_id)
        self.store.save(record)
        return {"action": "waiting_for_host", "run_id": record["run_id"]}

    # ---- automatic reference run (PRD 6.8) --------------------------------------------------

    def reference_exists(self, key: str) -> bool:
        """Whether a reference measurement is stored under `key` in the run bucket."""
        try:
            self.s3.head_object(Bucket=self.cfg.run_bucket, Key=f"{key}measurement.json")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def _reference_or_build(self, record: dict[str, Any], instance_id: str) -> dict[str, Any]:
        """Before the build, run `gate reference` on the build host if no reference exists for
        the configured key. The key is an explicit input (orchestrator config `reference.key`),
        never derived here."""
        ref = self.cfg.reference
        key = ref.key if not ref.key or ref.key.endswith("/") else ref.key + "/"
        details = rr.get_stage(record, "build")["details"]
        if not key or not ref.revision:
            details["reference"] = {"status": "skipped", "reason": "no reference key configured"}
        elif self.reference_exists(key):
            details["reference"] = {"status": "exists", "key": key}
        else:
            return self._start_reference(record, instance_id, key)
        self._send_build(record, instance_id)
        return {"action": "build_started", "run_id": record["run_id"]}

    def _start_reference(self, record: dict[str, Any], instance_id: str, key: str):
        now = self.clock()
        run_id = record["run_id"]
        fields = {
            **self._template_fields(record),
            "reference_key": key,
            "reference_revision": self.cfg.reference.revision,
        }
        resp = self.ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName=self.cfg.build.document,
            Comment=f"driftgate reference {run_id}"[:100],
            Parameters={
                "commands": [c.format(**fields) for c in self.cfg.reference.commands],
                "executionTimeout": [str(self.cfg.build.execution_timeout_seconds)],
            },
            OutputS3BucketName=self.cfg.run_bucket,
            OutputS3KeyPrefix=self.store.run_key_prefix(run_id, "reference-log/"),
        )
        command_id = resp["Command"]["CommandId"]
        self.store.put_index("ssm", command_id, run_id)
        details = rr.get_stage(record, "build")["details"]
        details["reference"] = {
            "status": "running",
            "key": key,
            "revision": self.cfg.reference.revision,
            "command_id": command_id,
            "started_at": rr.iso(now),
            "log_uri": self.store.run_uri(run_id, "reference-log/"),
        }
        self.store.put_host_usage(instance_id, now)
        self.store.save(record)
        return {"action": "reference_started", "run_id": run_id}

    def _on_reference_status(self, record: dict[str, Any], status: str) -> dict[str, Any]:
        now = self.clock()
        build = rr.get_stage(record, "build")
        ref = build["details"]["reference"]
        ref["status"] = "succeeded" if status in SSM_SUCCESS else "failed"
        ref["outcome"] = status
        ref["ended_at"] = rr.iso(now)
        if status in SSM_FAILURE:
            self._record_build_host_usage(build, now)
            rr.end_stage(record, "build", "failed", f"reference run {status}", now)
            return self._finish(record, "failed")
        self._send_build(record, build["details"]["instance_id"])
        return {"action": "build_started", "run_id": record["run_id"]}

    def on_instance_state(self, event: dict[str, Any]) -> dict[str, Any]:
        d = event.get("detail", {})
        instance_id, state = d.get("instance-id"), d.get("state")
        record = self._record_for("host-wait", instance_id)
        if record is None:
            return _ignored(f"instance {instance_id} {state}: no build waiting on it")
        build = rr.get_stage(record, "build")
        if build["status"] != "running" or not build["details"].get("waiting_for_host"):
            return _ignored("build not waiting for its host", record["run_id"])
        details = build["details"]
        if state == "stopped" and details.get("start_when_stopped"):
            self.ec2.start_instances(InstanceIds=[instance_id])
            details["start_when_stopped"] = False
            details["started_host"] = True
            self.store.save(record)
            return {"action": "host_starting", "run_id": record["run_id"]}
        if state == "running":
            details["ec2_running"] = True
            details["ec2_running_at"] = rr.iso(self.clock())
            return self._try_send_build(record)
        return _ignored(f"instance state {state}", record["run_id"])

    def _record_build_host_usage(self, build: dict[str, Any], now: datetime) -> None:
        instance_id = build["details"].get("instance_id")
        if instance_id:
            self.store.put_host_usage(instance_id, now)

    def sweep_build_host(self) -> dict[str, Any]:
        """Re-check SSM readiness and stop the build host after the configured idle window."""
        for instance_id in self.store.list_index("host-wait"):
            record = self._record_for("host-wait", instance_id)
            if record is None or rr.get_stage(record, "build")["status"] != "running":
                self.store.delete_index("host-wait", instance_id)
                continue
            return self._try_send_build(record)
        host = self._build_host()
        if host is None or host["State"]["Name"] != "running":
            return {"action": "none"}
        holder = self.store.lock_holder()
        if holder:
            active = self.store.load(holder)
            # A new run takes the lock before writing its first run record.
            if active is None or rr.get_stage(active, "build")["status"] in ("pending", "running"):
                return {"action": "none", "reason": "build in progress"}
        usage = self.store.host_usage()
        last = host["LaunchTime"]
        if usage and usage["instance_id"] == host["InstanceId"]:
            last = max(last, datetime.fromisoformat(usage["last_used_at"]))
        idle = (self.clock() - last).total_seconds()
        if idle < self.cfg.build.idle_stop_minutes * 60:
            return {"action": "none", "idle_seconds": int(idle)}
        if self.store.lock_holder() != holder:
            return {"action": "none", "reason": "active run changed during sweep"}
        self.ec2.stop_instances(InstanceIds=[host["InstanceId"]])
        return {"action": "stopped_idle_host", "instance_id": host["InstanceId"]}

    def _send_build(self, record: dict[str, Any], instance_id: str) -> None:
        now = self.clock()
        run_id = record["run_id"]
        fields = self._template_fields(record)
        commands = [c.format(**fields) for c in self.cfg.build.commands]
        log_prefix = self.store.run_key_prefix(run_id, "build-log/")
        resp = self.ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName=self.cfg.build.document,
            Comment=f"driftgate build {run_id}"[:100],
            Parameters={
                "commands": commands,
                "executionTimeout": [str(self.cfg.build.execution_timeout_seconds)],
            },
            OutputS3BucketName=self.cfg.run_bucket,
            OutputS3KeyPrefix=log_prefix,
        )
        command_id = resp["Command"]["CommandId"]
        self.store.put_index("ssm", command_id, run_id)
        details = rr.get_stage(record, "build")["details"]
        details.update(
            {
                "waiting_for_host": False,
                "command_id": command_id,
                "command_sent_at": rr.iso(now),
                "instance_ids": [instance_id],
                "log_uri": self.store.run_uri(run_id, "build-log/"),
            }
        )
        record["updated_at"] = rr.iso(now)
        self.store.put_host_usage(instance_id, now)
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
        build = rr.get_stage(record, "build")
        if build["status"] != "running":
            return _ignored("build already finished", record["run_id"])
        ref = build["details"].get("reference", {})
        if ref.get("command_id") == d.get("command-id"):
            if ref.get("status") != "running":
                return _ignored("reference run already finished", record["run_id"])
            return self._on_reference_status(record, status)
        if build["details"].get("command_id") != d.get("command-id"):
            return _ignored("not this run's build command", record["run_id"])
        now = self.clock()
        self._record_build_host_usage(build, now)
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
        if self.start_deploy(record)["action"] != "deploy_started":
            return {"result": "deploy_rejected", "run_id": run_id}
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
        bundle_uri = f"s3://{fields['bundle_bucket']}/{fields['bundle_key']}"
        try:
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
        except ClientError as e:
            # E.g. DeploymentGroupAlreadyHasActiveDeployment (a deployment started outside
            # DriftGate, or a rollback still running). Record it and end the run so the lock
            # is released, instead of failing the invocation and leaving the run stuck.
            err = e.response.get("Error", {})
            rr.start_stage(record, "deploy", now, {"bundle_uri": bundle_uri})
            rr.end_stage(
                record,
                "deploy",
                "failed",
                err.get("Code", "ClientError"),
                now,
                {"error": err.get("Message", str(e))},
            )
            out = self._finish(record, "failed")
            return {**out, "reason": "deploy_rejected"}
        deployment_id = resp["deploymentId"]
        self.store.put_index("codedeploy", deployment_id, record["run_id"])
        rr.start_stage(
            record,
            "deploy",
            now,
            {"deployment_id": deployment_id, "bundle_uri": bundle_uri},
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
        self.store.release_lock(record["run_id"])
        return {
            "action": "run_finished",
            "run_id": record["run_id"],
            "status": status,
        }

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
            "working_directory": self.cfg.build.working_directory,
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
