"""Publish a gate verdict to CloudWatch (FR-20, PRD 6.8). Called by validate_service.sh.

GateScore is published only when the verdict was decided in enforce mode (report-mode verdicts
carry no gate score); the raw per-detector values listed in deploy/cloudwatch-metrics.json are
always published. Values that are missing or not finite (e.g. a D6 ratio against a zero
reference) are skipped. Prints a JSON summary; exits 0 even when nothing was published.

  publish_metrics.py --verdict V.json --deployment-group G [--config deploy/cloudwatch-metrics.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "cloudwatch-metrics.json"


def _lookup(obj, dotted: str):
    for key in dotted.split("."):
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def _number(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        return None
    return float(v)


def metric_data(verdict: dict, config: dict, deployment_group: str) -> list[dict]:
    dims = [{"Name": config["dimension"], "Value": deployment_group}]
    data = []
    decision = verdict.get("decision", {})
    gate_score = _number(decision.get("gate_score"))
    if decision.get("mode") == "enforce" and gate_score is not None:
        g = config["gate_score"]
        data.append(
            {"MetricName": g["name"], "Dimensions": dims, "Value": gate_score, "Unit": g["unit"]}
        )
    for m in config["raw"]:
        value = _number(_lookup(verdict.get("detectors", {}).get(m["detector"], {}), m["path"]))
        if value is not None:
            data.append(
                {"MetricName": m["name"], "Dimensions": dims, "Value": value, "Unit": m["unit"]}
            )
    return data


def publish(cloudwatch, namespace: str, data: list[dict]) -> None:
    for i in range(0, len(data), 20):  # PutMetricData accepts at most 20 items per call here
        cloudwatch.put_metric_data(Namespace=namespace, MetricData=data[i : i + 20])


def main(argv: list[str] | None = None, cloudwatch=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--verdict", type=Path, required=True)
    p.add_argument("--deployment-group", required=True)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--region", default="us-east-1")
    args = p.parse_args(argv)
    config = json.loads(args.config.read_text())
    verdict = json.loads(args.verdict.read_text())
    data = metric_data(verdict, config, args.deployment_group)
    if cloudwatch is None:
        import boto3

        cloudwatch = boto3.client("cloudwatch", region_name=args.region)
    publish(cloudwatch, config["namespace"], data)
    json.dump({"namespace": config["namespace"], "published": [d["MetricName"] for d in data]},
              sys.stdout)  # fmt: skip
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
