"""FR-21 / NFR-04 / NFR-06: per-session CloudFormation network stack.

Offline only: the template is parsed and linted; nothing calls AWS.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy" / "cloudformation" / "network.yaml"
PARAMS = ROOT / "deploy" / "cloudformation" / "network-params.json"


@pytest.fixture(scope="module")
def template():
    tpl, matches = decode(str(TEMPLATE))
    assert not matches, matches
    return tpl


def resources_of(template, rtype):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == rtype}


def ingress_rules(sg):
    return sg["Properties"].get("SecurityGroupIngress", [])


def test_template_passes_cfn_lint():
    result = subprocess.run(
        [str(Path(sys.executable).with_name("cfn-lint")), str(TEMPLATE)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stack_has_vpc_alb_and_nat(template):
    assert len(resources_of(template, "AWS::EC2::VPC")) == 1
    assert len(resources_of(template, "AWS::EC2::NatGateway")) >= 1
    albs = resources_of(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    assert len(albs) == 1
    (alb,) = albs.values()
    assert alb["Properties"]["Scheme"] == "internet-facing"
    assert alb["Properties"]["Type"] == "application"


def test_s3_gateway_endpoint_present(template):
    endpoints = resources_of(template, "AWS::EC2::VPCEndpoint")
    assert any(
        e["Properties"].get("VpcEndpointType", "Gateway") == "Gateway" for e in endpoints.values()
    )


def test_private_subnets_do_not_map_public_ips(template):
    subnets = resources_of(template, "AWS::EC2::Subnet")
    private = {k: v for k, v in subnets.items() if k.startswith("Private")}
    assert len(private) >= 2
    for s in private.values():
        assert s["Properties"].get("MapPublicIpOnLaunch", False) is False


def test_alb_ingress_only_80_and_443(template):
    sg = template["Resources"]["AlbSecurityGroup"]
    ports = {(r["FromPort"], r["ToPort"]) for r in ingress_rules(sg)}
    assert ports <= {(80, 80), (443, 443)}
    assert ports
    for r in ingress_rules(sg):
        assert r["IpProtocol"] == "tcp"


def test_serving_accepts_only_alb_security_group(template):
    sg = template["Resources"]["ServingSecurityGroup"]
    rules = ingress_rules(sg)
    assert rules
    for r in rules:
        assert "CidrIp" not in r and "CidrIpv6" not in r
        assert r["SourceSecurityGroupId"] == {"Ref": "AlbSecurityGroup"}
    # no standalone ingress resources that would open it up elsewhere
    assert not resources_of(template, "AWS::EC2::SecurityGroupIngress")


def test_build_host_has_no_inbound(template):
    sg = template["Resources"]["BuildHostSecurityGroup"]
    assert ingress_rules(sg) == []


def test_no_ssh_anywhere(template):
    def port(value):
        if isinstance(value, dict):  # {"Ref": "<Parameter>"}: use its default
            return int(template["Parameters"][value["Ref"]]["Default"])
        return int(value)

    for sg in resources_of(template, "AWS::EC2::SecurityGroup").values():
        for r in ingress_rules(sg):
            assert not (port(r["FromPort"]) <= 22 <= port(r["ToPort"]))


def test_launch_templates_require_imdsv2_and_no_key_pair(template):
    lts = resources_of(template, "AWS::EC2::LaunchTemplate")
    assert len(lts) == 2
    for lt in lts.values():
        data = lt["Properties"]["LaunchTemplateData"]
        assert data["MetadataOptions"]["HttpTokens"] == "required"
        assert "KeyName" not in data
        assert data["IamInstanceProfile"] == {"Name": {"Ref": "InstanceProfileName"}}


def test_serving_port_is_a_parameter(template):
    assert "ServingPort" in template["Parameters"]
    params = PARAMS.read_text()
    assert "ServingPort" in params


def test_resources_are_tagged_for_cost(template):
    for name, res in template["Resources"].items():
        tags = res.get("Properties", {}).get("Tags")
        if tags is None or not isinstance(tags, list):
            continue
        assert any(t["Key"] == "Project" for t in tags), name


def test_outputs_export_network_ids(template):
    outputs = template["Outputs"]
    for key in (
        "VpcId",
        "PublicSubnetIds",
        "PrivateSubnetIds",
        "AlbArn",
        "AlbDnsName",
        "AlbListenerArn",
        "ServingSecurityGroupId",
        "BuildHostSecurityGroupId",
        "ServingLaunchTemplateId",
        "BuildHostLaunchTemplateId",
    ):
        assert key in outputs, key
        assert "Export" in outputs[key]


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
@pytest.mark.parametrize(
    ("target", "expected"),
    [("up", "aws cloudformation deploy"), ("down", "aws cloudformation delete-stack")],
)
def test_make_up_and_down_are_single_commands(target, expected):
    # `make -n` prints the recipe without running it, so no AWS call happens.
    result = subprocess.run(
        ["make", "-n", target], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert expected in result.stdout
    assert "--region us-east-1" in result.stdout
    if target == "up":
        assert "network.yaml" in result.stdout
