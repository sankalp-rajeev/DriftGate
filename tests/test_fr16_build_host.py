"""PRD 6.8 build host start/stop (FR-16): the orchestrator starts the instance tagged
Role=build-host at the build stage if it is stopped, sends the build only after BOTH the EC2
"running" state-change event and SSM online status, and stops the host via the scheduled sweep
after the configured idle period.

moto models EC2 start/stop; it does not implement ssm:DescribeInstanceInformation, so SSM
online status is the fixture's `ssm_online` set (see tests/conftest.py), plus one test of the
real request shape through a stub client.
"""

import json

import boto3
import pytest
from orchestrator_helpers import (
    REGION,
    ec2_event,
    push_event,
    read_record,
    scheduled_event,
    ssm_event,
    stage,
)

from orchestrator.config import load_config
from orchestrator.handler import Orchestrator


def instance(pipeline):
    resp = pipeline["ec2"].describe_instances(InstanceIds=[pipeline["host_id"]])
    return resp["Reservations"][0]["Instances"][0]


def state(pipeline):
    return instance(pipeline)["State"]["Name"]


def commands():
    return boto3.client("ssm", region_name=REGION).list_commands()["Commands"]


def stop_host(pipeline):
    pipeline["ec2"].stop_instances(InstanceIds=[pipeline["host_id"]])
    pipeline["ssm_online"].discard(pipeline["host_id"])
    assert state(pipeline) == "stopped"


def build_details(pipeline, run_id):
    return stage(read_record(pipeline["s3"], run_id), "build")["details"]


def test_running_online_host_gets_the_build_at_once(pipeline):
    out = pipeline["orc"].handle(push_event())
    assert out["build"]["action"] == "build_started"
    d = build_details(pipeline, out["run_id"])
    assert d["instance_id"] == pipeline["host_id"]
    assert d["started_host"] is False
    assert len(commands()) == 1


def test_stopped_host_is_started_and_build_waits_for_both_signals(pipeline):
    stop_host(pipeline)
    orc, host = pipeline["orc"], pipeline["host_id"]
    out = orc.handle(push_event())
    run_id = out["run_id"]
    assert out["build"]["action"] == "waiting_for_host"
    assert state(pipeline) in ("pending", "running")
    d = build_details(pipeline, run_id)
    assert d["started_host"] is True and d["waiting_for_host"] is True
    assert commands() == []

    # EC2 running event, but SSM not online yet: still waiting.
    assert orc.handle(ec2_event(host, "running"))["action"] == "waiting_for_host"
    assert build_details(pipeline, run_id)["ec2_running"] is True
    assert commands() == []

    # SSM comes online; the sweep notices and sends the build.
    pipeline["ssm_online"].add(host)
    pipeline["clock"].advance(300)
    out = orc.handle(scheduled_event())
    assert out["build_host"] == {"action": "build_started", "run_id": run_id}
    d = build_details(pipeline, run_id)
    assert d["waiting_for_host"] is False and d["ssm_online"] is True
    [cmd] = commands()
    assert cmd["InstanceIds"] == [host]


def test_ssm_online_without_ec2_event_does_not_start_build(pipeline):
    stop_host(pipeline)
    orc = pipeline["orc"]
    run_id = orc.handle(push_event())["run_id"]
    pipeline["ssm_online"].add(pipeline["host_id"])
    out = orc.handle(scheduled_event())
    assert out["build_host"] == {"action": "waiting_for_host", "run_id": run_id}
    assert commands() == []
    assert orc.handle(ec2_event(pipeline["host_id"], "running"))["action"] == "build_started"


def test_stopping_host_is_started_when_stopped_event_arrives(pipeline, monkeypatch):
    orc, host = pipeline["orc"], pipeline["host_id"]
    real = orc._build_host

    def stopping():
        h = real()
        h["State"]["Name"] = "stopping"
        return h

    monkeypatch.setattr(orc, "_build_host", stopping)
    run_id = orc.handle(push_event())["run_id"]
    assert build_details(pipeline, run_id)["start_when_stopped"] is True
    monkeypatch.setattr(orc, "_build_host", real)
    pipeline["ec2"].stop_instances(InstanceIds=[host])
    assert orc.handle(ec2_event(host, "stopped"))["action"] == "host_starting"
    assert state(pipeline) in ("pending", "running")
    assert orc.handle(ec2_event(host, "running"))["action"] == "build_started"


@pytest.mark.parametrize("status", ["Success", "Failed"])
def test_build_end_keeps_host_running_until_idle_deadline(pipeline, status):
    orc, clock = pipeline["orc"], pipeline["clock"]
    # moto sets LaunchTime from wall time, independently of the event clock.
    clock.t = max(clock(), instance(pipeline)["LaunchTime"])
    run_id = orc.handle(push_event())["run_id"]
    clock.advance(3600)
    orc.handle(ssm_event(build_details(pipeline, run_id)["command_id"], status))
    assert state(pipeline) == "running"

    clock.advance(1799)
    assert orc.handle(scheduled_event())["build_host"] == {
        "action": "none",
        "idle_seconds": 1799,
    }
    assert state(pipeline) == "running"

    clock.advance(1)
    assert orc.handle(scheduled_event())["build_host"] == {
        "action": "stopped_idle_host",
        "instance_id": pipeline["host_id"],
    }
    assert state(pipeline) == "stopped"


def test_events_for_other_instances_are_ignored(pipeline):
    stop_host(pipeline)
    pipeline["orc"].handle(push_event())
    assert pipeline["orc"].handle(ec2_event("i-0000000000000000f", "running"))["action"] == (
        "ignored"
    )


def test_no_build_host_fails_the_build_and_releases_lock(pipeline):
    pipeline["ec2"].terminate_instances(InstanceIds=[pipeline["host_id"]])
    out = pipeline["orc"].handle(push_event())
    assert out["build"]["status"] == "failed"
    rec = read_record(pipeline["s3"], out["run_id"])
    assert stage(rec, "build")["outcome"] == "no build host"
    assert pipeline["orc"].store.lock_holder() is None


def test_idle_sweep_uses_configured_minutes_for_unused_host(pipeline, tmp_path):
    orc, clock = pipeline["orc"], pipeline["clock"]
    path = tmp_path / "config.json"
    raw = orc.cfg.raw
    raw["build"]["idle_stop_minutes"] = 5
    path.write_text(json.dumps(raw))
    orc.cfg = load_config(path)

    clock.t = instance(pipeline)["LaunchTime"]
    clock.advance(299)
    assert orc.handle(scheduled_event())["build_host"]["action"] == "none"
    assert state(pipeline) == "running"
    clock.advance(1)
    out = orc.handle(scheduled_event())["build_host"]
    assert out == {"action": "stopped_idle_host", "instance_id": pipeline["host_id"]}
    assert state(pipeline) == "stopped"


def test_idle_sweep_leaves_host_running_during_a_build(pipeline):
    orc, clock = pipeline["orc"], pipeline["clock"]
    orc.handle(push_event())
    clock.advance(3 * 3600)
    out = orc.handle(scheduled_event())["build_host"]
    assert out == {"action": "none", "reason": "build in progress"}
    assert state(pipeline) == "running"


def test_ssm_online_uses_describe_instance_information(monkeypatch):
    calls = []

    class FakeSSM:
        def describe_instance_information(self, **kwargs):
            calls.append(kwargs)
            return {"InstanceInformationList": [{"InstanceId": "i-1", "PingStatus": "Online"}]}

    orc = Orchestrator(load_config(), s3=object(), ssm=FakeSSM(), codedeploy=object(),
                       sns=object(), ec2=object())  # fmt: skip
    assert orc._ssm_online("i-1") is True
    assert calls == [{"Filters": [{"Key": "InstanceIds", "Values": ["i-1"]}]}]


def test_config_idle_limit_and_tag():
    cfg = load_config()
    assert cfg.build.idle_stop_minutes == 30
    assert cfg.build.host_tag == {"key": "Role", "value": "build-host"}
