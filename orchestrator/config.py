"""Orchestrator configuration: `orchestrator/config.json`, with a few environment overrides.

The Lambda environment (set by `orchestrator/template.yaml`) supplies the per-session values
that only exist once the Lab stack is up: bucket name, build host instance id, CodeDeploy names.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")

# Environment variable -> (section, key). Section None means top level.
ENV_OVERRIDES: dict[str, tuple[str | None, str]] = {
    "DRIFTGATE_RUN_BUCKET": (None, "run_bucket"),
    "DRIFTGATE_BRANCH": (None, "branch"),
    "DRIFTGATE_CODEDEPLOY_APPLICATION": ("deploy", "application_name"),
    "DRIFTGATE_CODEDEPLOY_GROUP": ("deploy", "deployment_group_name"),
    "DRIFTGATE_BUNDLE_BUCKET": ("deploy", "bundle_bucket"),
    "DRIFTGATE_APPROVAL_TOPIC_ARN": ("approval", "topic_arn"),
    "DRIFTGATE_APPROVAL_URL": ("approval", "function_url"),
    "DRIFTGATE_GATE_REFERENCE_KEY": ("reference", "key"),
    "DRIFTGATE_GATE_REFERENCE_REVISION": ("reference", "revision"),
}


@dataclass(frozen=True)
class BuildConfig:
    # The build host is found by this tag (PRD 6.8: Role=build-host), started at the build
    # stage if stopped, stopped when the build ends, and stopped by the sweep when idle longer
    # than host_idle_stop_seconds (PRD 6.8: 30 minutes).
    host_tag: dict[str, str]
    host_idle_stop_seconds: int
    document: str
    working_directory: str
    commands: list[str]
    execution_timeout_seconds: int = 7200


@dataclass(frozen=True)
class DeployConfig:
    application_name: str
    deployment_group_name: str
    bundle_bucket: str
    bundle_key: str
    bundle_type: str


@dataclass(frozen=True)
class ReferenceConfig:
    # PRD 6.8 automatic reference run. `key` is the baseline's reference key, copied by the owner
    # from `python -m gate reference --print-key` on the build host's instance type
    # (reference/<baseline model sha>/<baseline config hash>/<instance-type>/; gate/baseline.yaml
    # pins the first two parts). The Lambda package has no gate/ code, so it does not derive it;
    # empty `key` or `revision` disables the automatic reference run.
    key: str
    # Git revision (commit, branch or tag) that is the reference; checked out for the run.
    revision: str
    commands: list[str]


@dataclass(frozen=True)
class ApprovalConfig:
    topic_arn: str
    function_url: str
    # FR-27 / PRD 6.8: no decision within this many seconds means reject. The one-time token
    # expires at the same moment (the approval deadline); there is no separate token TTL.
    timeout_seconds: int


@dataclass(frozen=True)
class Config:
    region: str
    branch: str
    run_bucket: str
    run_prefix: str
    # PRD 6.8: runs/active.json expires 2 h after it is taken; an expired lock is stale.
    lock_ttl_seconds: int
    build: BuildConfig
    deploy: DeployConfig
    approval: ApprovalConfig
    reference: ReferenceConfig
    raw: dict[str, Any]


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("DRIFTGATE_CONFIG") or DEFAULT_CONFIG_PATH)
    raw = json.loads(path.read_text())
    for var, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(var)
        if not value:
            continue
        target = raw if section is None else raw.setdefault(section, {})
        target[key] = value
    deploy = dict(raw["deploy"])
    deploy["bundle_bucket"] = deploy.get("bundle_bucket") or raw["run_bucket"]
    return Config(
        region=raw["region"],
        branch=raw["branch"],
        run_bucket=raw["run_bucket"],
        run_prefix=raw["run_prefix"],
        lock_ttl_seconds=int(raw["lock_ttl_seconds"]),
        build=BuildConfig(**raw["build"]),
        deploy=DeployConfig(**deploy),
        approval=ApprovalConfig(
            topic_arn=raw["approval"]["topic_arn"],
            function_url=raw["approval"]["function_url"],
            timeout_seconds=int(raw["approval"]["timeout_seconds"]),
        ),
        reference=ReferenceConfig(**raw["reference"]),
        raw=raw,
    )
