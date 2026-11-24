"""FR-17: CodeDeploy EC2 deployment group, in-place OneAtATime, behind the ALB target group, with
LabRole as the service role. Instances deregister during deploy and re-register after
ValidateService (CodeDeploy traffic control; the live check needs the Lab).

PRD 6.8: the network stack owns the ALB and the target group; this stack refers to it by name.
"""

import subprocess
from pathlib import Path

import pytest
import yaml
from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy" / "cloudformation" / "codedeploy.yaml"
NETWORK = ROOT / "deploy" / "cloudformation" / "network.yaml"


def load(path):
    tpl, errors = decode(str(path))
    assert not errors
    return tpl


@pytest.fixture(scope="module")
def tpl():
    return load(TEMPLATE)


def by_type(tpl, type_):
    return {k: v for k, v in tpl["Resources"].items() if v["Type"] == type_}


def group(tpl):
    [g] = by_type(tpl, "AWS::CodeDeploy::DeploymentGroup").values()
    return g["Properties"]


def test_template_lints_clean():
    from cfnlint.api import lint_file

    assert [str(m) for m in lint_file(TEMPLATE)] == []


def test_in_place_one_at_a_time_with_labrole(tpl):
    g = group(tpl)
    assert g["DeploymentConfigName"] == "CodeDeployDefault.OneAtATime"
    assert g["DeploymentStyle"] == {
        "DeploymentType": "IN_PLACE",
        "DeploymentOption": "WITH_TRAFFIC_CONTROL",
    }
    assert g["ServiceRoleArn"] == {"Ref": "LabRoleArn"}
    [app] = by_type(tpl, "AWS::CodeDeploy::Application").values()
    assert app["Properties"]["ComputePlatform"] == "Server"


def test_target_group_referenced_by_name_from_network_stack(tpl):
    [info] = group(tpl)["LoadBalancerInfo"]["TargetGroupInfoList"]
    assert info == {"Name": {"Fn::ImportValue": {"Fn::Sub": "${NetworkStackName}-TargetGroupName"}}}
    network = load(NETWORK)
    export = network["Outputs"]["TargetGroupName"]["Export"]["Name"]
    assert export == {"Fn::Sub": "${AWS::StackName}-TargetGroupName"}


def test_no_target_group_or_listener_created_here(tpl):
    types = {v["Type"] for v in tpl["Resources"].values()}
    assert not {t for t in types if t.startswith("AWS::ElasticLoadBalancingV2::")}
    assert "AWS::S3::Bucket" not in types


def test_names_match_orchestrator_config(tpl):
    import json

    cfg = json.loads((ROOT / "orchestrator" / "config.json").read_text())["deploy"]
    params = tpl["Parameters"]
    assert params["ApplicationName"]["Default"] == cfg["application_name"]
    assert params["DeploymentGroupName"]["Default"] == cfg["deployment_group_name"]


def test_rollback_on_deployment_failure(tpl):
    rb = group(tpl)["AutoRollbackConfiguration"]
    assert rb["Enabled"] is True
    assert "DEPLOYMENT_FAILURE" in rb["Events"]


def test_two_serving_instances_matched_by_the_group_tags(tpl):
    instances = by_type(tpl, "AWS::EC2::Instance")
    assert {name for name in instances if name != "BuildHost"} == {
        "ServingInstanceA",
        "ServingInstanceB",
    }
    for name in ("ServingInstanceA", "ServingInstanceB"):
        inst = instances[name]
        lt = inst["Properties"]["LaunchTemplate"]["LaunchTemplateId"]
        assert lt == {"Fn::ImportValue": {"Fn::Sub": "${NetworkStackName}-ServingLaunchTemplateId"}}
    # The launch template tags instances Role=serving and Project; the group requires both.
    tag_groups = [tg["Ec2TagGroup"][0] for tg in group(tpl)["Ec2TagSet"]["Ec2TagSetList"]]
    assert {"Key": "Role", "Value": "serving", "Type": "KEY_AND_VALUE"} in tag_groups
    network = load(NETWORK)
    lt_tags = network["Resources"]["ServingLaunchTemplate"]["Properties"]["LaunchTemplateData"][
        "TagSpecifications"
    ][0]["Tags"]
    assert {"Key": "Role", "Value": "serving"} in lt_tags


def test_user_data_installs_codedeploy_agent_and_uv_on_al2023(tpl):
    for name in ("ServingInstanceA", "ServingInstanceB"):
        inst = tpl["Resources"][name]
        script = inst["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
        assert "dnf install" in script
        assert "codedeploy-agent" in script
        assert "astral.sh/uv" in script
        assert "/etc/driftgate/deploy.env" in script


def test_build_host_uses_network_exports_and_bootstraps_from_codecommit(tpl):
    host = by_type(tpl, "AWS::EC2::Instance")["BuildHost"]["Properties"]
    assert host["LaunchTemplate"] == {
        "LaunchTemplateId": {
            "Fn::ImportValue": {"Fn::Sub": "${NetworkStackName}-BuildHostLaunchTemplateId"}
        },
        "Version": "1",
    }
    assert host["SubnetId"] == {
        "Fn::Select": [
            0,
            {
                "Fn::Split": [
                    ",",
                    {"Fn::ImportValue": {"Fn::Sub": "${NetworkStackName}-InstanceSubnetIds"}},
                ]
            },
        ]
    }
    assert host["Tags"] == [{"Key": "Name", "Value": "driftgate-build-host"}]
    script = host["UserData"]["Fn::Base64"]["Fn::Sub"]
    for value in (
        "/var/log/driftgate-bootstrap.log",
        "dnf install -y git cmake gcc-c++ make python3-pip",
        "UV_INSTALL_DIR=/usr/local/bin",
        "pip install git-remote-codecommit",
        'git clone "codecommit::us-east-1://${RepositoryName}" /opt/driftgate',
        "/etc/driftgate/build.env",
        "DRIFTGATE_BUNDLE_BUCKET=${BucketName}",
        "DRIFTGATE_UPLOAD=1",
        "deploy/bootstrap_build_host.sh",
    ):
        assert value in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_serving_port_matches_network_stack_default(tpl):
    network = load(NETWORK)
    assert (
        tpl["Parameters"]["ServingPort"]["Default"]
        == network["Parameters"]["ServingPort"]["Default"]
    )


def test_appspec_destination_is_the_hooks_app_dir():
    spec = yaml.safe_load((ROOT / "deploy" / "appspec.yml").read_text())
    assert spec["files"][0]["destination"] == "/opt/driftgate"
