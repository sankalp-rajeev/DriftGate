"""FR-21 / NFR-04 / NFR-06 / PRD 6.8: per-session CloudFormation network stack.

Offline only: the template is parsed and linted; nothing calls AWS. Conditions are
resolved for both values of the UseNat parameter.
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
AL2023_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"
NO_VALUE = object()


@pytest.fixture(scope="module")
def raw():
    tpl, matches = decode(str(TEMPLATE))
    assert not matches, matches
    return tpl


def _resolve(node, conditions):
    """Resolve Fn::If against known condition values and drop AWS::NoValue."""
    if isinstance(node, dict):
        if set(node) == {"Fn::If"}:
            cond, yes, no = node["Fn::If"]
            return _resolve(yes if conditions[cond] else no, conditions)
        if node == {"Ref": "AWS::NoValue"}:
            return NO_VALUE
        out = {}
        for k, v in node.items():
            r = _resolve(v, conditions)
            if r is not NO_VALUE:
                out[k] = r
        return out
    if isinstance(node, list):
        return [r for r in (_resolve(v, conditions) for v in node) if r is not NO_VALUE]
    return node


def render(raw, use_nat):
    """Template as CloudFormation would create it for UseNat=use_nat."""
    conditions = {"UseNat": use_nat}
    assert set(raw["Conditions"]) == set(conditions)
    tpl = dict(raw)
    for section in ("Resources", "Outputs"):
        tpl[section] = {
            k: _resolve(v, conditions)
            for k, v in raw[section].items()
            if conditions.get(v.get("Condition"), True)
        }
    return tpl


@pytest.fixture(scope="module", params=[True, False], ids=["UseNat=true", "UseNat=false"])
def template(request, raw):
    return render(raw, request.param)


@pytest.fixture(scope="module")
def nat(raw):
    return render(raw, True)


@pytest.fixture(scope="module")
def no_nat(raw):
    return render(raw, False)


def resources_of(template, rtype):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == rtype}


def ingress_rules(sg):
    return sg["Properties"].get("SecurityGroupIngress", [])


def refs(value):
    """Names referenced by !Ref anywhere inside value."""
    if isinstance(value, dict):
        if set(value) == {"Ref"}:
            return {value["Ref"]}
        return set().union(*(refs(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(refs(v) for v in value)) if value else set()
    return set()


def test_template_passes_cfn_lint():
    result = subprocess.run(
        [str(Path(sys.executable).with_name("cfn-lint")), str(TEMPLATE)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_use_nat_parameter_defaults_true(raw):
    param = raw["Parameters"]["UseNat"]
    assert param["Default"] == "true"
    assert set(param["AllowedValues"]) == {"true", "false"}
    assert '"UseNat", "ParameterValue": "true"' in PARAMS.read_text()


def test_stack_has_vpc_and_alb(template):
    assert len(resources_of(template, "AWS::EC2::VPC")) == 1
    albs = resources_of(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    assert len(albs) == 1
    (alb,) = albs.values()
    assert alb["Properties"]["Scheme"] == "internet-facing"
    assert alb["Properties"]["Type"] == "application"
    assert refs(alb["Properties"]["Subnets"]) == {"PublicSubnetA", "PublicSubnetB"}


def test_nat_mode_has_nat_and_private_instance_subnets(nat):
    assert len(resources_of(nat, "AWS::EC2::NatGateway")) == 1
    private = {k: v for k, v in resources_of(nat, "AWS::EC2::Subnet").items() if "Private" in k}
    assert len(private) == 2
    for s in resources_of(nat, "AWS::EC2::Subnet").values():
        assert s["Properties"]["MapPublicIpOnLaunch"] is False
    assert refs(nat["Outputs"]["InstanceSubnetIds"]["Value"]) == {
        "PrivateSubnetA",
        "PrivateSubnetB",
    }


def test_no_nat_mode_puts_instances_in_public_subnets(no_nat):
    assert not resources_of(no_nat, "AWS::EC2::NatGateway")
    assert not resources_of(no_nat, "AWS::EC2::EIP")
    subnets = resources_of(no_nat, "AWS::EC2::Subnet")
    assert set(subnets) == {"PublicSubnetA", "PublicSubnetB"}
    for s in subnets.values():
        assert s["Properties"]["MapPublicIpOnLaunch"] is True
    assert refs(no_nat["Outputs"]["InstanceSubnetIds"]["Value"]) == {
        "PublicSubnetA",
        "PublicSubnetB",
    }
    assert "PrivateSubnetIds" not in no_nat["Outputs"]


def test_no_dangling_refs(template):
    known = set(template["Resources"]) | set(template["Parameters"])
    known |= {"AWS::StackName", "AWS::Region", "AWS::AccountId"}
    for name, body in [*template["Resources"].items(), *template["Outputs"].items()]:
        assert refs(body) <= known, (name, refs(body) - known)


def test_s3_gateway_endpoint_on_instance_route_tables(template):
    endpoints = resources_of(template, "AWS::EC2::VPCEndpoint")
    (ep,) = endpoints.values()
    assert ep["Properties"]["VpcEndpointType"] == "Gateway"
    tables = refs(ep["Properties"]["RouteTableIds"])
    assert tables <= set(template["Resources"])
    assert ("PrivateRouteTable" in tables) == ("PrivateRouteTable" in template["Resources"])


def test_alb_ingress_http_80_only(template):
    sg = template["Resources"]["AlbSecurityGroup"]
    rules = ingress_rules(sg)
    assert [(r["IpProtocol"], r["FromPort"], r["ToPort"]) for r in rules] == [("tcp", 80, 80)]
    assert "443" not in TEMPLATE.read_text()


def test_listener_http_80_forwards_to_target_group(template):
    listeners = resources_of(template, "AWS::ElasticLoadBalancingV2::Listener")
    (listener,) = listeners.values()
    props = listener["Properties"]
    assert props["Port"] == 80 and props["Protocol"] == "HTTP"
    (action,) = props["DefaultActions"]
    assert action["Type"] == "forward"
    assert action["TargetGroupArn"] == {"Ref": "ServingTargetGroup"}


def test_stack_owns_named_target_group(template):
    tg = template["Resources"]["ServingTargetGroup"]["Properties"]
    assert tg["Name"] == {"Ref": "TargetGroupName"}
    assert tg["Port"] == {"Ref": "ServingPort"}
    assert tg["TargetType"] == "instance"
    assert tg["HealthCheckPath"] == {"Ref": "HealthCheckPath"}
    assert template["Parameters"]["HealthCheckPath"]["Default"] == "/health"
    assert template["Outputs"]["TargetGroupName"]["Export"]


def test_serving_accepts_only_alb_security_group(template):
    sg = template["Resources"]["ServingSecurityGroup"]
    rules = ingress_rules(sg)
    assert rules
    for r in rules:
        assert "CidrIp" not in r and "CidrIpv6" not in r
        assert r["SourceSecurityGroupId"] == {"Ref": "AlbSecurityGroup"}
    assert not resources_of(template, "AWS::EC2::SecurityGroupIngress")


def test_build_host_has_no_inbound(template):
    assert ingress_rules(template["Resources"]["BuildHostSecurityGroup"]) == []


def test_no_ssh_anywhere(template):
    def port(value):
        if isinstance(value, dict):  # {"Ref": "<Parameter>"}: use its default
            return int(template["Parameters"][value["Ref"]]["Default"])
        return int(value)

    for sg in resources_of(template, "AWS::EC2::SecurityGroup").values():
        for r in ingress_rules(sg):
            assert not (port(r["FromPort"]) <= 22 <= port(r["ToPort"]))


def test_launch_templates_imdsv2_al2023_no_key_pair(template):
    assert template["Parameters"]["LatestAmiId"]["Default"] == AL2023_PARAM
    assert template["Parameters"]["LatestAmiId"]["Type"] == (
        "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>"
    )
    lts = resources_of(template, "AWS::EC2::LaunchTemplate")
    assert len(lts) == 2
    for lt in lts.values():
        data = lt["Properties"]["LaunchTemplateData"]
        assert data["ImageId"] == {"Ref": "LatestAmiId"}
        assert data["MetadataOptions"]["HttpTokens"] == "required"
        assert "KeyName" not in data
        assert data["IamInstanceProfile"] == {"Name": {"Ref": "InstanceProfileName"}}


def test_no_s3_bucket_in_session_stack(template):
    assert not resources_of(template, "AWS::S3::Bucket")


def test_serving_port_is_a_parameter(template):
    assert "ServingPort" in template["Parameters"]
    assert "ServingPort" in PARAMS.read_text()


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
        "InstanceSubnetIds",
        "AlbArn",
        "AlbDnsName",
        "AlbListenerArn",
        "TargetGroupName",
        "TargetGroupArn",
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
    assert "driftgate-storage" not in result.stdout
    if target == "up":
        assert "network.yaml" in result.stdout
