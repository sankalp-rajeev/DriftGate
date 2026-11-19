"""NFR-06: least-privilege policy documents, one per service, each marked
enforced or documented-only. Offline only (no Access Analyzer call)."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IAM = ROOT / "iam"
CONTROLS = json.loads((IAM / "controls.json").read_text())
POLICY_FILES = sorted(p for p in IAM.glob("*.json") if p.name != "controls.json")

# Actions that AWS does not scope to a resource ARN, so "Resource": "*" is required.
NO_RESOURCE_LEVEL = {
    "ssm:UpdateInstanceInformation",
    "ssm:GetCommandInvocation",
    "ssm:ListCommandInvocations",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
    "ec2messages:AcknowledgeMessage",
    "ec2messages:DeleteMessage",
    "ec2messages:FailMessage",
    "ec2messages:GetEndpoint",
    "ec2messages:GetMessages",
    "ec2messages:SendReply",
    "codedeploy-commands-secure:GetDeploymentSpecification",
    "codedeploy-commands-secure:PollHostCommand",
    "codedeploy-commands-secure:PutHostCommandAcknowledgement",
    "codedeploy-commands-secure:PutHostCommandComplete",
    "cloudwatch:PutMetricData",
    "cloudwatch:DescribeAlarms",
    "ec2:DescribeInstances",
    "ec2:DescribeInstanceStatus",
    "tag:GetResources",
    "elasticloadbalancing:DescribeTargetGroups",
    "elasticloadbalancing:DescribeTargetHealth",
}

EXPECTED_SERVICES = {
    "Lambda orchestrator",
    "Lambda approval handler",
    "EC2 build host",
    "EC2 serving instances",
    "CodeDeploy service role",
    "S3 artifact bucket",
}


def as_list(value):
    return value if isinstance(value, list) else [value]


def statements(path):
    return as_list(json.loads(path.read_text())["Statement"])


def test_policy_files_exist():
    assert len(POLICY_FILES) >= 6


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_policy_is_valid_iam_json(path):
    doc = json.loads(path.read_text())
    assert doc["Version"] == "2012-10-17"
    sids = [s["Sid"] for s in statements(path)]
    assert len(sids) == len(set(sids))
    for s in statements(path):
        assert s["Effect"] in {"Allow", "Deny"}
        assert s["Action"] and s["Resource"]


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_no_wildcard_allow(path):
    for s in statements(path):
        if s["Effect"] != "Allow":
            continue
        actions = as_list(s["Action"])
        for a in actions:
            assert a != "*" and not a.endswith(":*"), (path.name, a)
        if "*" in as_list(s["Resource"]):
            extra = set(actions) - NO_RESOURCE_LEVEL
            assert not extra, (path.name, s["Sid"], extra)


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_us_east_1_or_placeholder_region_only(path):
    text = path.read_text()
    regions = set(re.findall(r"arn:aws:[a-z0-9-]+:([a-z0-9-]*):", text))
    assert regions <= {"", "${Region}"}, regions


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_placeholders_documented(path):
    used = set(re.findall(r"\$\{([A-Za-z]+)\}", path.read_text()))
    assert used <= set(CONTROLS["placeholders"]), used - set(CONTROLS["placeholders"])


def test_every_policy_file_has_one_control_and_status():
    referenced = [c["policy"] for c in CONTROLS["controls"] if c["policy"]]
    assert sorted(referenced) == [p.name for p in POLICY_FILES]
    for c in CONTROLS["controls"]:
        assert c["status"] in {"enforced", "documented-only"}, c["id"]


def test_one_policy_per_service():
    services = {c["service"] for c in CONTROLS["controls"] if c["policy"]}
    assert EXPECTED_SERVICES <= services


def test_identity_policies_documented_only_under_labrole():
    for c in CONTROLS["controls"]:
        if c["kind"] == "identity-policy":
            assert c["status"] == "documented-only", c["id"]


def test_nfr06_resource_controls_are_listed_as_enforced():
    enforced = {c["id"] for c in CONTROLS["controls"] if c["status"] == "enforced"}
    assert {
        "sg-alb",
        "sg-serving",
        "sg-build-host",
        "ssm-not-ssh",
        "imdsv2",
        "s3-bucket-policy",
        "lambda-resource-orchestrator",
        "lambda-resource-approval-url",
    } <= enforced


def test_applied_in_files_exist():
    for c in CONTROLS["controls"]:
        if c["applied_in"]:
            assert (ROOT / c["applied_in"]).is_file(), c["id"]


def test_bucket_policy_tls_account_and_encryption():
    sts = {s["Sid"]: s for s in statements(IAM / "s3-bucket-policy.json")}
    assert sts["DenyNonTls"]["Condition"]["Bool"]["aws:SecureTransport"] == "false"
    assert "aws:PrincipalAccount" in sts["DenyOutsideAccount"]["Condition"]["StringNotEquals"]
    assert "s3:x-amz-server-side-encryption" in sts["DenyUnencryptedPut"]["Condition"]["Null"]
    assert all(s["Effect"] == "Deny" for s in sts.values())


def test_orchestrator_invocable_only_by_eventbridge_rules():
    (s,) = statements(IAM / "lambda-resource-orchestrator.json")
    assert s["Principal"] == {"Service": "events.amazonaws.com"}
    assert s["Condition"]["ArnLike"]["AWS:SourceArn"].startswith("arn:aws:events:")


def test_approval_handler_invocable_only_via_function_url():
    (s,) = statements(IAM / "lambda-resource-approval-url.json")
    assert s["Action"] == "lambda:InvokeFunctionUrl"
    assert "lambda:FunctionUrlAuthType" in s["Condition"]["StringEquals"]


def test_build_host_send_command_scoped_by_tag():
    sts = statements(IAM / "orchestrator-lambda.json")
    send = [s for s in sts if s["Action"] == "ssm:SendCommand"]
    instance = [s for s in send if ":instance/" in s["Resource"]]
    assert instance and all(
        "ssm:resourceTag/Role" in s["Condition"]["StringEquals"] for s in instance
    )
