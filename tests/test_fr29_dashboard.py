"""FR-29: CloudWatch dashboard with gate score, alarm state, ALB target health and deployment
status. The body lives in observability/cloudwatch-dashboard.json and is embedded verbatim in
deploy/cloudformation/codedeploy.yaml (Fn::Sub); this keeps the two identical."""

import json
import re
from pathlib import Path

import pytest
from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parents[1]
DASH_FILE = ROOT / "observability" / "cloudwatch-dashboard.json"
TEMPLATE = ROOT / "deploy" / "cloudformation" / "codedeploy.yaml"


@pytest.fixture(scope="module")
def tpl():
    t, errors = decode(str(TEMPLATE))
    assert not errors
    return t


@pytest.fixture(scope="module")
def dash():
    return json.loads(DASH_FILE.read_text())


def body_and_vars(tpl):
    sub = tpl["Resources"]["Dashboard"]["Properties"]["DashboardBody"]["Fn::Sub"]
    return sub[0], sub[1]


def test_template_body_is_the_committed_file(tpl, dash):
    body, _ = body_and_vars(tpl)
    assert json.loads(body) == dash


def test_every_placeholder_is_bound(tpl):
    body, variables = body_and_vars(tpl)
    names = set(re.findall(r"\$\{([^}]+)\}", body))
    assert names - {"AWS::Region"} == set(variables)


def _widgets(dash, type_):
    return [w["properties"] for w in dash["widgets"] if w["type"] == type_]


def test_gate_score_widget(dash):
    metrics = [m for p in _widgets(dash, "metric") for m in p["metrics"]]
    assert ["DriftGate", "GateScore", "DeploymentGroup", "${DeploymentGroupName}"] in metrics
    gate = next(p for p in _widgets(dash, "metric") if "Gate score" in p["title"])
    assert gate["annotations"]["horizontal"][0]["value"] == 1


def test_alarm_state_widget(dash):
    [alarm] = _widgets(dash, "alarm")
    assert alarm["alarms"] == ["${GateScoreAlarmArn}"]


def test_alb_target_health_widget(dash):
    names = {
        m[1] for p in _widgets(dash, "metric") for m in p["metrics"] if m[0] == "AWS/ApplicationELB"
    }
    assert names == {"HealthyHostCount", "UnHealthyHostCount"}


def test_deployment_status_widget_reads_codedeploy_events(tpl, dash):
    [log] = _widgets(dash, "log")
    assert "${DeploymentEventsLogGroup}" in log["query"] and "detail.state" in log["query"]
    rule = tpl["Resources"]["DeploymentEventsRule"]["Properties"]
    assert rule["EventPattern"]["source"] == ["aws.codedeploy"]
    assert rule["Targets"][0]["Arn"] == {"Fn::GetAtt": ["DeploymentEventsLogGroup", "Arn"]}
    group = tpl["Resources"]["DeploymentEventsLogGroup"]["Properties"]["LogGroupName"]
    assert group["Fn::Sub"].startswith("/aws/events/")


def test_raw_detector_metrics_on_dashboard_are_published(dash):
    published = {m["name"] for m in json.loads(
        (ROOT / "deploy" / "cloudwatch-metrics.json").read_text())["raw"]}  # fmt: skip
    shown = {m[1] for p in _widgets(dash, "metric") for m in p["metrics"] if m[0] == "DriftGate"}
    assert shown - {"GateScore"} <= published
