"""Shared helpers for the orchestrator tests (FR-15, FR-27). All AWS is moto."""

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

REGION = "us-east-1"
BUCKET = "driftgate-runs-test"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
APPROVAL_URL = "https://abc123.lambda-url.us-east-1.on.aws/"


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class SpySNS:
    """Forwards to the moto SNS client and keeps every publish call for inspection."""

    def __init__(self, client):
        self.client = client
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return self.client.publish(**kwargs)


def push_event(
    branch="main",
    commit=COMMIT,
    event="referenceUpdated",
    event_id="evt-1",
    time="2026-11-24T12:00:00Z",
):
    return {
        "version": "0",
        "id": event_id,
        "detail-type": "CodeCommit Repository State Change",
        "source": "aws.codecommit",
        "time": time,
        "region": REGION,
        "resources": [f"arn:aws:codecommit:{REGION}:123456789012:driftgate"],
        "detail": {
            "event": event,
            "repositoryName": "driftgate",
            "referenceType": "branch",
            "referenceName": branch,
            "commitId": commit,
        },
    }


def ssm_event(command_id, status):
    return {
        "id": f"ssm-{command_id}-{status}",
        "detail-type": "EC2 Command Status-change Notification",
        "source": "aws.ssm",
        "detail": {"command-id": command_id, "status": status},
    }


def deploy_event(deployment_id, state):
    return {
        "id": f"cd-{deployment_id}-{state}",
        "detail-type": "CodeDeploy Deployment State-change Notification",
        "source": "aws.codedeploy",
        "detail": {"deploymentId": deployment_id, "state": state, "application": "driftgate"},
    }


def scheduled_event():
    return {"id": "sched-1", "detail-type": "Scheduled Event", "source": "aws.events"}


def read_record(s3, run_id):
    obj = s3.get_object(Bucket=BUCKET, Key=f"runs/{run_id}/run.json")
    return json.loads(obj["Body"].read())


def stage(record, name):
    return next(s for s in record["stages"] if s["name"] == name)


def links_from(message):
    """{decision: (url, params)} for the approve and reject links in an SNS message."""
    out = {}
    for url in re.findall(r"https://\S+", message):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if "decision" in q:
            out[q["decision"]] = (url, q)
    return out


def url_event(method, params, source_ip="203.0.113.7"):
    """A Lambda function URL (payload v2) event. GET puts params in the query; POST in a form."""
    from urllib.parse import urlencode

    event = {"requestContext": {"http": {"method": method, "sourceIp": source_ip}}}
    if method == "GET":
        event["queryStringParameters"] = params
    else:
        event["body"] = urlencode(params)
        event["isBase64Encoded"] = False
    return event


def approve_latest(pipeline, run_id, decision="approve"):
    """Click the link from the most recent approval email through the orchestrator."""
    _url, q = links_from(pipeline["sns"].published[-1]["Message"])[decision]
    assert q["run_id"] == run_id
    return pipeline["orc"].decide(run_id, decision, q["token"])


def build_succeeds(pipeline, run_id):
    command_id = stage(read_record(pipeline["s3"], run_id), "build")["details"]["command_id"]
    return pipeline["orc"].handle(ssm_event(command_id, "Success"))


def ec2_event(instance_id, state):
    return {
        "id": f"ec2-{instance_id}-{state}",
        "detail-type": "EC2 Instance State-change Notification",
        "source": "aws.ec2",
        "detail": {"instance-id": instance_id, "state": state},
    }


def host_comes_up(pipeline):
    """The build host's EC2 'running' event arrives (it is already SSM-online in the fixture)."""
    return pipeline["orc"].handle(ec2_event(pipeline["host_id"], "running"))
