"""JSON run record kept in S3 (FR-15).

Layout under `<run_prefix>`:
  active.json                    lock: the one active run {"run_id", "acquired_at", "expires_at"},
                                 taken with a conditional put (If-None-Match: *), deleted when the
                                 run ends. Past expires_at (2 h) it is stale: the next run replaces
                                 it (If-Match on its ETag) and the old run is marked abandoned
  build-host.json                {"instance_id", "last_used_at"} for the idle-stop sweep
  queued.json                    the latest push that arrived during the active run (a newer
                                 push replaces it); started when the lock is released
  <run_id>/run.json              the run record (schema below)
  <run_id>/build-log/...         SSM Run Command output (written by SSM)
  index/<kind>/<key>.json        pointer {"run_id": ...} from an external id to its run
                                 (kinds: event, ssm, codedeploy, approval-pending)

Run record schema (version 1):
  schema_version, run_id, status (running|succeeded|failed|abandoned), current_stage,
  created_at, updated_at, ended_at (null while running),
  trigger {repository, branch, commit_id, event_id, event_time},
  stages [{name, status (pending|running|succeeded|failed|skipped|abandoned),
           started_at, ended_at, outcome, details {}}]
Timestamps are ISO 8601 UTC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

RUN_RECORD_SCHEMA_VERSION = 1

TERMINAL_STAGE_STATES = {"succeeded", "failed", "skipped", "abandoned"}
LOCK_CONFLICT_CODES = ("PreconditionFailed", "ConditionalRequestConflict")


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def new_record(
    run_id: str, trigger: dict[str, Any], stage_names: list[str], now: datetime
) -> dict[str, Any]:
    return {
        "schema_version": RUN_RECORD_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "current_stage": stage_names[0],
        "created_at": iso(now),
        "updated_at": iso(now),
        "ended_at": None,
        "trigger": trigger,
        "stages": [
            {
                "name": name,
                "status": "pending",
                "started_at": None,
                "ended_at": None,
                "outcome": None,
                "details": {},
            }
            for name in stage_names
        ],
    }


def get_stage(record: dict[str, Any], name: str) -> dict[str, Any]:
    for s in record["stages"]:
        if s["name"] == name:
            return s
    raise KeyError(name)


def start_stage(
    record: dict[str, Any], name: str, now: datetime, details: dict[str, Any] | None = None
) -> None:
    s = get_stage(record, name)
    s["status"] = "running"
    s["started_at"] = iso(now)
    s["details"].update(details or {})
    record["current_stage"] = name
    record["updated_at"] = iso(now)


def end_stage(
    record: dict[str, Any],
    name: str,
    status: str,
    outcome: str,
    now: datetime,
    details: dict[str, Any] | None = None,
) -> None:
    s = get_stage(record, name)
    s["status"] = status
    s["outcome"] = outcome
    s["started_at"] = s["started_at"] or iso(now)
    s["ended_at"] = iso(now)
    s["details"].update(details or {})
    record["updated_at"] = iso(now)


def finish_run(record: dict[str, Any], status: str, now: datetime) -> None:
    """End the run; every stage that never ran is marked skipped."""
    for s in record["stages"]:
        if s["status"] == "pending":
            s["status"] = "skipped"
            s["outcome"] = "not run"
    record["status"] = status
    record["ended_at"] = iso(now)
    record["updated_at"] = iso(now)


def abandon_run(record: dict[str, Any], reason: str, now: datetime) -> None:
    """Mark a run that will never finish: its running stage and the run become abandoned."""
    for s in record["stages"]:
        if s["status"] == "running":
            s["status"] = "abandoned"
            s["outcome"] = reason
            s["ended_at"] = iso(now)
        elif s["status"] == "pending":
            s["status"] = "skipped"
            s["outcome"] = "not run"
    record["status"] = "abandoned"
    record["abandoned_reason"] = reason
    record["ended_at"] = iso(now)
    record["updated_at"] = iso(now)


@dataclass(frozen=True)
class LockResult:
    acquired: bool
    stale: dict[str, Any] | None = None  # the expired lock that was taken over, if any

    def __bool__(self) -> bool:
        return self.acquired


class RunStore:
    """Reads and writes run records and index pointers in one S3 bucket."""

    def __init__(self, s3, bucket: str, prefix: str, lock_ttl_seconds: int = 7200):
        self.s3 = s3
        self.bucket = bucket
        self.prefix = prefix
        self.lock_ttl_seconds = lock_ttl_seconds

    def record_key(self, run_id: str) -> str:
        return f"{self.prefix}{run_id}/run.json"

    def run_uri(self, run_id: str, suffix: str = "") -> str:
        return f"s3://{self.bucket}/{self.prefix}{run_id}/{suffix}"

    def run_key_prefix(self, run_id: str, suffix: str = "") -> str:
        return f"{self.prefix}{run_id}/{suffix}"

    def _put_json(self, key: str, body: dict[str, Any]) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(body, indent=2, sort_keys=True).encode(),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )

    def _get_json(self, key: str) -> dict[str, Any] | None:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except self.s3.exceptions.NoSuchKey:
            return None
        return json.loads(obj["Body"].read())

    def save(self, record: dict[str, Any]) -> None:
        self._put_json(self.record_key(record["run_id"]), record)

    def load(self, run_id: str) -> dict[str, Any] | None:
        return self._get_json(self.record_key(run_id))

    def _index_key(self, kind: str, key: str) -> str:
        return f"{self.prefix}index/{kind}/{key}.json"

    def put_index(self, kind: str, key: str, run_id: str) -> None:
        self._put_json(self._index_key(kind, key), {"run_id": run_id})

    def delete_index(self, kind: str, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self._index_key(kind, key))

    def list_index(self, kind: str) -> list[str]:
        """Keys (not run ids) currently in an index, e.g. runs awaiting approval."""
        prefix = f"{self.prefix}index/{kind}/"
        keys: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"][len(prefix) : -len(".json")])
        return keys

    # ---- one active run (PRD 6.8) ---------------------------------------------------------

    @property
    def lock_key(self) -> str:
        return f"{self.prefix}active.json"

    @property
    def queue_key(self) -> str:
        return f"{self.prefix}queued.json"

    def _put_lock(self, body: dict[str, Any], **condition: str) -> bool:
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=self.lock_key,
                Body=json.dumps(body).encode(),
                ContentType="application/json",
                ServerSideEncryption="AES256",
                **condition,
            )
        except self.s3.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in LOCK_CONFLICT_CODES:
                return False
            raise
        return True

    def lock_expires_at(self, lock: dict[str, Any]) -> datetime:
        if lock.get("expires_at"):
            return datetime.fromisoformat(lock["expires_at"])
        return datetime.fromisoformat(lock["acquired_at"]) + timedelta(
            seconds=self.lock_ttl_seconds
        )

    def acquire_lock(self, run_id: str, now: datetime) -> LockResult:
        """Take `active.json` if absent (If-None-Match: *), or replace it if it has expired
        (If-Match on the ETag read, so two runs cannot both take over the same stale lock)."""
        body = {
            "run_id": run_id,
            "acquired_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=self.lock_ttl_seconds)),
        }
        if self._put_lock(body, IfNoneMatch="*"):
            return LockResult(True)
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.lock_key)
        except self.s3.exceptions.NoSuchKey:
            # Released between our put and our read: try once more as a fresh lock.
            return LockResult(self._put_lock(body, IfNoneMatch="*"))
        current = json.loads(obj["Body"].read())
        if now < self.lock_expires_at(current):
            return LockResult(False)
        if self._put_lock(body, IfMatch=obj["ETag"]):
            return LockResult(True, stale=current)
        return LockResult(False)

    def delete_lock(self) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self.lock_key)

    @property
    def host_key(self) -> str:
        return f"{self.prefix}build-host.json"

    def host_usage(self) -> dict[str, Any] | None:
        return self._get_json(self.host_key)

    def put_host_usage(self, instance_id: str, now: datetime) -> None:
        self._put_json(self.host_key, {"instance_id": instance_id, "last_used_at": iso(now)})

    def lock_holder(self) -> str | None:
        found = self._get_json(self.lock_key)
        return found["run_id"] if found else None

    def release_lock(self, run_id: str) -> bool:
        """Delete the lock if `run_id` holds it. Returns whether it was released."""
        if self.lock_holder() != run_id:
            return False
        self.s3.delete_object(Bucket=self.bucket, Key=self.lock_key)
        return True

    def abandon(self, run_id: str, reason: str, now: datetime) -> bool:
        """Mark a still-running run abandoned and drop its wait indexes. False if not running."""
        record = self.load(run_id)
        if record is None or record["status"] != "running":
            return False
        abandon_run(record, reason, now)
        self.save(record)
        self.delete_index("approval-pending", run_id)
        host = get_stage(record, "build")["details"].get("instance_id")
        if host and self.lookup("host-wait", host) == run_id:
            self.delete_index("host-wait", host)
        return True

    def queued(self) -> dict[str, Any] | None:
        return self._get_json(self.queue_key)

    def put_queued(self, entry: dict[str, Any]) -> None:
        self._put_json(self.queue_key, entry)

    def clear_queued(self) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self.queue_key)

    def lookup(self, kind: str, key: str) -> str | None:
        found = self._get_json(self._index_key(kind, key))
        return found["run_id"] if found else None
