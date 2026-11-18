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
    "DRIFTGATE_BUILD_INSTANCE_IDS": ("build", "instance_ids"),
    "DRIFTGATE_CODEDEPLOY_APPLICATION": ("deploy", "application_name"),
    "DRIFTGATE_CODEDEPLOY_GROUP": ("deploy", "deployment_group_name"),
    "DRIFTGATE_BUNDLE_BUCKET": ("deploy", "bundle_bucket"),
}


@dataclass(frozen=True)
class BuildConfig:
    instance_ids: list[str]
    document: str
    working_directory: str
    commands: list[str]


@dataclass(frozen=True)
class DeployConfig:
    application_name: str
    deployment_group_name: str
    bundle_bucket: str
    bundle_key: str
    bundle_type: str


@dataclass(frozen=True)
class Config:
    region: str
    branch: str
    run_bucket: str
    run_prefix: str
    build: BuildConfig
    deploy: DeployConfig
    raw: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        """Raw access to a config section added by later stages (e.g. `approval`)."""
        return self.raw.get(name, {})


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("DRIFTGATE_CONFIG") or DEFAULT_CONFIG_PATH)
    raw = json.loads(path.read_text())
    for var, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(var)
        if not value:
            continue
        target = raw if section is None else raw.setdefault(section, {})
        target[key] = (
            [v.strip() for v in value.split(",") if v.strip()] if key.endswith("_ids") else value
        )
    deploy = dict(raw["deploy"])
    deploy["bundle_bucket"] = deploy.get("bundle_bucket") or raw["run_bucket"]
    return Config(
        region=raw["region"],
        branch=raw["branch"],
        run_bucket=raw["run_bucket"],
        run_prefix=raw["run_prefix"],
        build=BuildConfig(**raw["build"]),
        deploy=DeployConfig(**deploy),
        raw=raw,
    )
