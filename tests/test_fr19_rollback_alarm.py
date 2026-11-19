"""FR-19, FR-20 (PRD 6.8): automatic rollback on DEPLOYMENT_FAILURE and DEPLOYMENT_STOP_ON_ALARM,
with the GateScore alarm (> 1 for 1 datapoint) named on the deployment group. Offline template
checks; the forced hook-failure and forced-alarm drills need the Lab (issue #16)."""

from pathlib import Path

import pytest
from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy" / "cloudformation" / "codedeploy.yaml"


@pytest.fixture(scope="module")
def res():
    tpl, errors = decode(str(TEMPLATE))
    assert not errors
    return tpl["Resources"]


def test_rollback_on_hook_failure_and_on_alarm(res):
    rb = res["DeploymentGroup"]["Properties"]["AutoRollbackConfiguration"]
    assert rb["Enabled"] is True
    assert set(rb["Events"]) == {"DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_ALARM"}


def test_gate_score_alarm_is_named_on_the_group(res):
    ac = res["DeploymentGroup"]["Properties"]["AlarmConfiguration"]
    assert ac["Enabled"] is True
    assert ac["Alarms"] == [{"Name": {"Ref": "GateScoreAlarm"}}]


def test_alarm_fires_on_gate_score_above_one_for_one_datapoint(res):
    a = res["GateScoreAlarm"]["Properties"]
    assert (a["Namespace"], a["MetricName"]) == ("DriftGate", "GateScore")
    assert a["Threshold"] == 1
    assert a["ComparisonOperator"] == "GreaterThanThreshold"
    assert a["EvaluationPeriods"] == 1 and a["DatapointsToAlarm"] == 1
    assert a["Statistic"] == "Maximum"
    # Report-mode deploys publish no GateScore; missing data must not alarm.
    assert a["TreatMissingData"] == "notBreaching"
    assert a["Dimensions"] == [{"Name": "DeploymentGroup", "Value": {"Ref": "DeploymentGroupName"}}]


def test_hook_failure_path_is_validate_service_exit_code():
    """DEPLOYMENT_FAILURE comes from ValidateService exiting non-zero; DRIFTGATE_FORCE_FAIL=1
    forces it for the drill (exercised in tests/test_fr18_hooks.py)."""
    text = (ROOT / "deploy" / "hooks" / "validate_service.sh").read_text()
    assert 'exit "$rc"' in text
    assert "DRIFTGATE_FORCE_FAIL" in text
