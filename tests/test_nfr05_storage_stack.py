"""T-31 / NFR-05 / PRD 6.8: persistent driftgate-storage stack with the S3 bucket.

Offline only: the template is parsed and linted; nothing calls AWS.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parents[1]
CFN = ROOT / "deploy" / "cloudformation"
TEMPLATE = CFN / "storage.yaml"
IAM_POLICY = ROOT / "iam" / "s3-bucket-policy.json"


@pytest.fixture(scope="module")
def template():
    tpl, matches = decode(str(TEMPLATE))
    assert not matches, matches
    return tpl


@pytest.fixture(scope="module")
def bucket(template):
    buckets = [r for r in template["Resources"].values() if r["Type"] == "AWS::S3::Bucket"]
    assert len(buckets) == 1
    return buckets[0]["Properties"]


def normalize(node):
    """Turn CloudFormation intrinsics into the ${Placeholder} form used in iam/."""
    if isinstance(node, dict):
        if set(node) == {"Fn::Sub"}:
            return node["Fn::Sub"].replace("${AWS::AccountId}", "${AccountId}")
        return {k: normalize(v) for k, v in node.items()}
    if isinstance(node, list):
        return [normalize(v) for v in node]
    return node


def test_template_passes_cfn_lint():
    result = subprocess.run(
        [str(Path(sys.executable).with_name("cfn-lint")), str(TEMPLATE)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_versioning_enabled(bucket):
    assert bucket["VersioningConfiguration"]["Status"] == "Enabled"


def test_sse_by_default(bucket):
    (rule,) = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] in {"AES256", "aws:kms"}


def test_block_public_access_all_four(bucket):
    bpa = bucket["PublicAccessBlockConfiguration"]
    for key in (
        "BlockPublicAcls",
        "BlockPublicPolicy",
        "IgnorePublicAcls",
        "RestrictPublicBuckets",
    ):
        assert bpa[key] is True, key


def test_acls_disabled(bucket):
    assert bucket["OwnershipControls"]["Rules"] == [{"ObjectOwnership": "BucketOwnerEnforced"}]


def test_bucket_policy_matches_iam_document(template):
    (policy,) = [r for r in template["Resources"].values() if r["Type"] == "AWS::S3::BucketPolicy"]
    assert policy["Properties"]["Bucket"] == {"Ref": "ArtifactBucket"}
    in_template = normalize(policy["Properties"]["PolicyDocument"])
    assert in_template == json.loads(IAM_POLICY.read_text())


def test_bucket_policy_tls_deny(template):
    doc = template["Resources"]["ArtifactBucketPolicy"]["Properties"]["PolicyDocument"]
    sts = {s["Sid"]: s for s in doc["Statement"]}
    tls = sts["DenyNonTls"]
    assert tls["Effect"] == "Deny" and tls["Principal"] == "*" and tls["Action"] == "s3:*"
    assert tls["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_bucket_name_is_parameterised_and_exported(template, bucket):
    assert "BucketNamePrefix" in template["Parameters"]
    assert "${BucketNamePrefix}" in bucket["BucketName"]["Fn::Sub"]
    assert "${AWS::AccountId}" in bucket["BucketName"]["Fn::Sub"]
    out = template["Outputs"]["BucketName"]
    assert out["Value"] == {"Ref": "ArtifactBucket"}
    assert out["Export"]


def test_network_stack_does_not_own_a_bucket():
    tpl, _ = decode(str(CFN / "network.yaml"))
    assert not [r for r in tpl["Resources"].values() if r["Type"] == "AWS::S3::Bucket"]


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("storage-up", "aws cloudformation deploy"),
        ("storage-down", "aws cloudformation delete-stack"),
    ],
)
def test_make_storage_targets(target, expected):
    # `make -n` prints the recipe without running it, so no AWS call happens.
    out = subprocess.run(
        ["make", "-n", target], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    assert expected in out
    assert "--stack-name driftgate-storage" in out
    assert "--region us-east-1" in out
    if target == "storage-up":
        assert "storage.yaml" in out


def test_session_down_leaves_storage_stack():
    out = subprocess.run(
        ["make", "-n", "down"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    assert "driftgate-storage" not in out


def _lifecycle_rules(bucket):
    return bucket["LifecycleConfiguration"]["Rules"]


def test_noncurrent_versions_expire_after_7_days(bucket):
    rules = [r for r in _lifecycle_rules(bucket) if "NoncurrentVersionExpiration" in r]
    assert len(rules) == 1 and rules[0]["Status"] == "Enabled"
    assert rules[0]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 7}


def test_incomplete_multipart_uploads_aborted_after_1_day(bucket):
    rules = [r for r in _lifecycle_rules(bucket) if "AbortIncompleteMultipartUpload" in r]
    assert len(rules) == 1 and rules[0]["Status"] == "Enabled"
    assert rules[0]["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 1}


def test_no_rule_expires_current_versions(bucket):
    # ExpirationInDays / ExpirationDate are the only rule fields that expire current versions.
    for rule in _lifecycle_rules(bucket):
        assert not {"ExpirationInDays", "ExpirationDate"} & set(rule), rule.get("Id")


def test_lifecycle_keeps_bucket_policy_in_sync(template):
    # Lifecycle changes must not drift the bucket policy from iam/s3-bucket-policy.json.
    test_bucket_policy_matches_iam_document(template)
