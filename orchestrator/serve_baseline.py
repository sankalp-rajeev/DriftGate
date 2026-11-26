"""Restore the last successful revision behind the existing Lab ALB."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from orchestrator.config import Config, load_config
from orchestrator.run_record import run_record_keys


def _stack_outputs(cfn, name: str) -> dict[str, str]:
    stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    return {output["OutputKey"]: output["OutputValue"] for output in stack.get("Outputs", [])}


def _saved_revision(s3, bucket: str, config: Config) -> dict:
    """Recover the most recent successful bundle if the deployment group was recreated."""
    for key in sorted(run_record_keys(s3, bucket, config.run_prefix), reverse=True):
        with s3.get_object(Bucket=bucket, Key=key)["Body"] as body:
            record = json.load(body)
        if record["status"] != "succeeded":
            continue
        for stage in record["stages"]:
            if stage["name"] == "deploy" and stage["status"] == "succeeded":
                uri = urlsplit(stage["details"].get("bundle_uri", ""))
                if uri.scheme == "s3" and uri.netloc and uri.path.lstrip("/"):
                    return {
                        "revisionType": "S3",
                        "s3Location": {
                            "bucket": uri.netloc,
                            "key": uri.path.lstrip("/"),
                            "bundleType": config.deploy.bundle_type,
                        },
                    }
    raise RuntimeError("No successful CodeDeploy revision or saved pipeline bundle found")


def _resume_serving_instances(ec2, ids: list[str]) -> None:
    instances = {
        instance["InstanceId"]: instance["State"]["Name"]
        for reservation in ec2.describe_instances(InstanceIds=ids)["Reservations"]
        for instance in reservation["Instances"]
    }
    stopping = [instance_id for instance_id in ids if instances[instance_id] == "stopping"]
    if stopping:
        ec2.get_waiter("instance_stopped").wait(InstanceIds=stopping)
    stopped = [
        instance_id for instance_id in ids if instances[instance_id] in ("stopped", "stopping")
    ]
    if stopped:
        ec2.start_instances(InstanceIds=stopped)
    ec2.get_waiter("instance_status_ok").wait(InstanceIds=ids, IncludeAllInstances=True)


def serve_baseline(
    session: boto3.Session,
    config: Config,
    *,
    network_stack: str,
    cd_stack: str,
    storage_stack: str,
) -> tuple[str, str]:
    cfn = session.client("cloudformation")
    alb = _stack_outputs(cfn, network_stack)["AlbDnsName"]
    serving = _stack_outputs(cfn, cd_stack)
    target = {
        "applicationName": serving["ApplicationName"],
        "deploymentGroupName": serving["DeploymentGroupName"],
    }
    cd = session.client("codedeploy")
    group = cd.get_deployment_group(**target)["deploymentGroupInfo"]
    last_id = group.get("lastSuccessfulDeployment", {}).get("deploymentId")
    if last_id:
        revision = cd.get_deployment(deploymentId=last_id)["deploymentInfo"]["revision"]
    else:
        bucket = _stack_outputs(cfn, storage_stack)["BucketName"]
        revision = _saved_revision(session.client("s3"), bucket, config)

    _resume_serving_instances(session.client("ec2"), serving["ServingInstanceIds"].split(","))
    deployment_id = cd.create_deployment(
        **target, revision=revision, description="Restore last successful revision"
    )["deploymentId"]
    print(f"Waiting for CodeDeploy deployment {deployment_id}", file=sys.stderr)
    cd.get_waiter("deployment_successful").wait(deploymentId=deployment_id)
    return deployment_id, f"http://{alb}"


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=config.region)
    parser.add_argument("--network-stack", required=True)
    parser.add_argument("--cd-stack", required=True)
    parser.add_argument("--storage-stack", required=True)
    args = parser.parse_args()
    try:
        deployment_id, url = serve_baseline(
            boto3.Session(region_name=args.region),
            config,
            network_stack=args.network_stack,
            cd_stack=args.cd_stack,
            storage_stack=args.storage_stack,
        )
    except (BotoCoreError, ClientError, KeyError, RuntimeError) as exc:
        print(f"serve-baseline: {exc}", file=sys.stderr)
        return 1
    print(f"Deployment {deployment_id} succeeded")
    print(f"ALB URL: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
