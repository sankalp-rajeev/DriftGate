"""Manual recovery for a stuck run lock (PRD 6.8, `make unlock`).

Deletes `runs/active.json` and marks the run that held it `abandoned` (reason "manual unlock").

Runs with the operator's own AWS credentials (e.g. the Lab session's), never from tests:
  make unlock BUCKET=<driftgate-storage bucket>
  python -m orchestrator.unlock --bucket <bucket> [--prefix runs/] [--region us-east-1]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

import boto3

from orchestrator.config import load_config
from orchestrator.run_record import RunStore, iso


def unlock(store: RunStore, now: datetime) -> dict:
    holder = store.lock_holder()
    if holder is None:
        return {"unlocked": False, "reason": "no lock"}
    abandoned = store.abandon(holder, "manual unlock (make unlock)", now)
    store.delete_lock()
    return {"unlocked": True, "run_id": holder, "abandoned": abandoned, "at": iso(now)}


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(prog="orchestrator.unlock", description=__doc__.splitlines()[0])
    p.add_argument("--bucket", default=cfg.run_bucket or None, required=not cfg.run_bucket)
    p.add_argument("--prefix", default=cfg.run_prefix)
    p.add_argument("--region", default=cfg.region)
    args = p.parse_args(argv)
    store = RunStore(boto3.client("s3", region_name=args.region), args.bucket, args.prefix)
    json.dump(unlock(store, datetime.now(UTC)), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
