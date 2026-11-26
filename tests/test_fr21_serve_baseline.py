"""Dec 2 serving startup uses the existing ALB and a successful CodeDeploy revision."""

import io
import json
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber

from orchestrator.config import load_config

ROOT = Path(__file__).resolve().parents[1]
TARGET = {"applicationName": "demo-app", "deploymentGroupName": "demo-serving"}
REVISION = {
    "revisionType": "S3",
    "s3Location": {"bucket": "demo-bundles", "key": "bundles/last.zip", "bundleType": "zip"},
}


def test_serve_baseline_target_uses_cd_up_with_existing_network():
    result = subprocess.run(
        ["make", "-n", "serve-baseline", "STACK_NAME=demo-net", "CD_STACK_NAME=demo-cd"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "make cd-up" in result.stdout
    assert "deploy/cloudformation/codedeploy.yaml" in result.stdout
    assert "NetworkStackName=demo-net" in result.stdout
    assert "orchestrator.serve_baseline" in result.stdout
    assert "--network-stack demo-net" in result.stdout.replace('"', "")
    assert "network.yaml" not in result.stdout
    assert "orch-up" not in result.stdout
    assert "demo-push" not in result.stdout


@pytest.fixture
def lab():
    session = boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-east-1",
    )
    clients = {name: session.client(name) for name in ("cloudformation", "codedeploy", "ec2", "s3")}
    session = Mock(spec=boto3.Session)
    session.client.side_effect = clients.__getitem__
    with ExitStack() as stack:
        stubs = {name: stack.enter_context(Stubber(client)) for name, client in clients.items()}
        yield session, stubs
        for stub in stubs.values():
            stub.assert_no_pending_responses()


def stack_output(stub, name, **outputs):
    stub.add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": name,
                    "CreationTime": "2026-11-26T12:00:00Z",
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [
                        {"OutputKey": key, "OutputValue": value} for key, value in outputs.items()
                    ],
                }
            ]
        },
        {"StackName": name},
    )


@pytest.mark.parametrize("has_group_history", [True, False])
def test_serve_baseline_redeploys_successful_bundle_without_build(lab, has_group_history):
    from orchestrator.serve_baseline import serve_baseline

    session, stubs = lab
    cfn, cd, ec2, s3 = (stubs[name] for name in ("cloudformation", "codedeploy", "ec2", "s3"))
    stack_output(cfn, "demo-net", AlbDnsName="existing-alb.example")
    stack_output(
        cfn,
        "demo-cd",
        ApplicationName="demo-app",
        DeploymentGroupName="demo-serving",
        ServingInstanceIds="i-serving-a,i-serving-b",
    )
    group = {"deploymentGroupInfo": dict(TARGET)}
    expected_revision = REVISION
    if has_group_history:
        group["deploymentGroupInfo"]["lastSuccessfulDeployment"] = {"deploymentId": "d-GOOD"}
        expected_revision = {
            **REVISION,
            "s3Location": {**REVISION["s3Location"], "version": "exact-version"},
        }
        cd.add_response("get_deployment_group", group, TARGET)
        cd.add_response(
            "get_deployment",
            {"deploymentInfo": {"status": "Succeeded", "revision": expected_revision}},
            {"deploymentId": "d-GOOD"},
        )
    else:
        cd.add_response("get_deployment_group", group, TARGET)
        stack_output(cfn, "demo-storage", BucketName="demo-bundles")
        failed, good = "runs/20261126T140000Z-bad/run.json", "runs/20261126T130000Z-good/run.json"
        s3.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": key} for key in (good, "runs/active.json", failed)]},
            {"Bucket": "demo-bundles", "Prefix": "runs/"},
        )
        for key, status, uri in (
            (failed, "failed", "s3://demo-bundles/bundles/failed.zip"),
            (good, "succeeded", "s3://demo-bundles/bundles/last.zip"),
        ):
            record = {
                "status": status,
                "stages": [
                    {"name": "deploy", "status": status, "details": {"bundle_uri": uri}},
                ],
            }
            s3.add_response(
                "get_object",
                {"Body": io.BytesIO(json.dumps(record).encode())},
                {"Bucket": "demo-bundles", "Key": key},
            )

    ids = ["i-serving-a", "i-serving-b"]
    ec2.add_response(
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        {"InstanceId": ids[0], "State": {"Name": "running"}},
                        {"InstanceId": ids[1], "State": {"Name": "stopped"}},
                    ]
                }
            ]
        },
        {"InstanceIds": ids},
    )
    ec2.add_response("start_instances", {}, {"InstanceIds": [ids[1]]})
    ec2.add_response(
        "describe_instance_status",
        {
            "InstanceStatuses": [
                {"InstanceId": instance_id, "InstanceStatus": {"Status": "ok"}}
                for instance_id in ids
            ]
        },
        {"InstanceIds": ids, "IncludeAllInstances": True},
    )
    cd.add_response(
        "create_deployment",
        {"deploymentId": "d-BASELINE"},
        TARGET | {"revision": expected_revision, "description": "Restore last successful revision"},
    )
    cd.add_response(
        "get_deployment",
        {"deploymentInfo": {"status": "Succeeded"}},
        {"deploymentId": "d-BASELINE"},
    )

    assert serve_baseline(
        session,
        load_config(),
        network_stack="demo-net",
        cd_stack="demo-cd",
        storage_stack="demo-storage",
    ) == ("d-BASELINE", "http://existing-alb.example")
