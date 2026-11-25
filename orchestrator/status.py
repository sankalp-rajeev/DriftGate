"""Read the newest pipeline run from S3 for `make pipeline-status` (Lab only)."""

from __future__ import annotations

import argparse
import json
import sys

import boto3

from orchestrator.config import load_config


def pipeline_status(s3, bucket: str, prefix: str = "runs/") -> dict:
    prefix = prefix.rstrip("/") + "/"
    keys = (
        obj["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
        if len(obj["Key"][len(prefix) :].split("/")) == 2 and obj["Key"].endswith("/run.json")
    )
    # Run ids start with YYYYMMDDTHHMMSSZ. LastModified would prefer an old run updated later.
    key = max(keys, default=None)
    if key is None:
        return {"schema": "driftgate.pipeline-status/1", "run_id": None, "status": "no_runs"}
    record = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    stages = {stage["name"]: stage for stage in record["stages"]}
    approval = stages.get("approval", {})
    approval_details = approval.get("details", {})
    return {
        "schema": "driftgate.pipeline-status/1",
        "run_id": record["run_id"],
        "commit": record["trigger"]["commit_id"],
        "status": record["status"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "ended_at": record["ended_at"],
        "stages": [
            {
                field: stage.get(field)
                for field in ("name", "status", "started_at", "ended_at", "outcome")
            }
            for stage in record["stages"]
        ],
        "approval": {
            "state": approval.get("outcome") or approval.get("status", "pending"),
            "decision": approval_details.get("decision"),
            "deadline": approval_details.get("deadline"),
        },
        "deployment_id": stages.get("deploy", {}).get("details", {}).get("deployment_id"),
        "build_log_uri": stages.get("build", {}).get("details", {}).get("log_uri"),
    }


def main(argv: list[str] | None = None) -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bucket", default=config.run_bucket or None, required=not config.run_bucket
    )
    parser.add_argument("--region", default=config.region)
    parser.add_argument("--prefix", default=config.run_prefix)
    args = parser.parse_args(argv)
    s3 = boto3.client("s3", region_name=args.region)
    json.dump(pipeline_status(s3, args.bucket, args.prefix), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
