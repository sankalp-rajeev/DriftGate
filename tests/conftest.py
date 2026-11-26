import boto3
import pytest
from moto import mock_aws
from orchestrator_helpers import APPROVAL_URL, BUCKET, REGION, FakeClock, SpySNS

from orchestrator.config import load_config
from orchestrator.handler import Orchestrator


@pytest.fixture
def baseline_config(tmp_path):
    from baseline_serving import write_config

    return write_config(tmp_path / "baseline-config.yaml")


@pytest.fixture
def baseline_workspace(tmp_path, monkeypatch):
    from baseline_serving import workspace

    path = workspace(tmp_path / "baseline")
    monkeypatch.chdir(path)
    monkeypatch.setenv("UV_NO_SYNC", "1")
    return path


@pytest.fixture(autouse=True)
def isolate_deploy_environment(monkeypatch, tmp_path):
    """Keep host deployment settings out of tests; test fixtures can opt in explicitly."""
    monkeypatch.setenv("DRIFTGATE_BUILD_ENV", str(tmp_path / "no-build.env"))
    monkeypatch.setenv("DRIFTGATE_INSTANCE_ENV", str(tmp_path / "no-instance.env"))
    for key in (
        "DRIFTGATE_UPLOAD",
        "DRIFTGATE_BUCKET",
        "DRIFTGATE_BUNDLE_BUCKET",
        "DRIFTGATE_FORCE_FAIL",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def pipeline(monkeypatch):
    """Mocked Lab account: run bucket, CodeDeploy group, approval topic and a running build
    host (tag Role=build-host). moto does not implement ssm:DescribeInstanceInformation, so SSM
    online status comes from the `ssm_online` set (the host starts online)."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DRIFTGATE_RUN_BUCKET", BUCKET)
    monkeypatch.setenv("DRIFTGATE_APPROVAL_URL", APPROVAL_URL)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        sns_client = boto3.client("sns", region_name=REGION)
        topic_arn = sns_client.create_topic(Name="driftgate-approval")["TopicArn"]
        monkeypatch.setenv("DRIFTGATE_APPROVAL_TOPIC_ARN", topic_arn)
        cfg = load_config()
        cd = boto3.client("codedeploy", region_name=REGION)
        cd.create_application(applicationName=cfg.deploy.application_name)
        cd.create_deployment_group(
            applicationName=cfg.deploy.application_name,
            deploymentGroupName=cfg.deploy.deployment_group_name,
            serviceRoleArn="arn:aws:iam::123456789012:role/LabRole",
        )
        ec2 = boto3.client("ec2", region_name=REGION)
        host_id = ec2.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": [{"Key": "Role", "Value": "build-host"}]}
            ],
        )["Instances"][0]["InstanceId"]
        ssm_online = {host_id}
        monkeypatch.setattr(
            Orchestrator, "_ssm_online", lambda self, instance_id: instance_id in ssm_online
        )
        clock = FakeClock()
        sns = SpySNS(sns_client)
        yield {
            "orc": Orchestrator(cfg, clock=clock, sns=sns),
            "s3": s3,
            "sns": sns,
            "cd": cd,
            "clock": clock,
            "cfg": cfg,
            "topic_arn": topic_arn,
            "ec2": ec2,
            "host_id": host_id,
            "ssm_online": ssm_online,
        }


from llama_fixtures import ci_server  # noqa: E402, F401  (live llama-server fixture)
