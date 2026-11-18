import boto3
import pytest
from moto import mock_aws
from orchestrator_helpers import APPROVAL_URL, BUCKET, INSTANCE, REGION, FakeClock, SpySNS

from orchestrator.config import load_config
from orchestrator.handler import Orchestrator


@pytest.fixture
def pipeline(monkeypatch):
    """Mocked Lab account with the run bucket, CodeDeploy group and approval topic."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DRIFTGATE_RUN_BUCKET", BUCKET)
    monkeypatch.setenv("DRIFTGATE_BUILD_INSTANCE_IDS", INSTANCE)
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
        }
