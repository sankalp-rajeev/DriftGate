"""PRD 6.8 / T-33: when no reference measurement exists under the reference key, the
orchestrator runs `gate reference` on the build host (SSM) before the build.

The key is an explicit input (orchestrator config `reference.key`, or the function argument):
how candidates obtain the reference revision's key is an open owner decision, so nothing here
derives it from the candidate's config.
"""

import dataclasses

import boto3
import pytest
from orchestrator_helpers import BUCKET, REGION, push_event, read_record, ssm_event, stage

from orchestrator.config import ReferenceConfig, load_config
from orchestrator.handler import Orchestrator

KEY = "reference/" + "a" * 64 + "/" + "b" * 64 + "/t3.large/"


@pytest.fixture
def orc(pipeline):
    cfg = dataclasses.replace(
        pipeline["cfg"],
        reference=dataclasses.replace(pipeline["cfg"].reference, key=KEY, revision="reference"),
    )
    o = Orchestrator(cfg, clock=pipeline["clock"], sns=pipeline["sns"])
    pipeline["orc"] = o
    return o


def commands():
    return boto3.client("ssm", region_name=REGION).list_commands()["Commands"]


def details(pipeline, run_id):
    return stage(read_record(pipeline["s3"], run_id), "build")["details"]


def store_reference(pipeline, key=KEY):
    pipeline["s3"].put_object(Bucket=BUCKET, Key=f"{key}measurement.json", Body=b"{}")


def test_missing_reference_runs_gate_reference_first(pipeline, orc):
    out = orc.handle(push_event())
    assert out["build"]["action"] == "reference_started"
    [cmd] = commands()
    script = "\n".join(cmd["Parameters"]["commands"])
    assert f"--reference-key {KEY}" in script
    assert "git checkout reference" in script
    assert "python -m gate reference --seed 0" in script
    assert f"--s3-uri s3://{BUCKET} --upload" in script
    ref = details(pipeline, out["run_id"])["reference"]
    assert ref["status"] == "running" and ref["key"] == KEY
    assert ref["command_id"] == cmd["CommandId"]
    assert "command_id" not in details(pipeline, out["run_id"])  # build not sent yet


def test_reference_success_then_build_runs(pipeline, orc):
    run_id = orc.handle(push_event())["run_id"]
    ref_cmd = details(pipeline, run_id)["reference"]["command_id"]
    pipeline["clock"].advance(900)
    out = orc.handle(ssm_event(ref_cmd, "Success"))
    assert out == {"action": "build_started", "run_id": run_id}
    d = details(pipeline, run_id)
    assert d["reference"]["status"] == "succeeded"
    assert d["reference"]["ended_at"] == "2026-11-24T12:15:00+00:00"
    assert len(commands()) == 2
    assert any("deploy/build.sh" in c for x in commands() for c in x["Parameters"]["commands"])
    # The repeat of the reference event changes nothing.
    assert orc.handle(ssm_event(ref_cmd, "Success"))["action"] == "ignored"


def test_reference_failure_fails_build_and_stops_host(pipeline, orc):
    run_id = orc.handle(push_event())["run_id"]
    ref_cmd = details(pipeline, run_id)["reference"]["command_id"]
    out = orc.handle(ssm_event(ref_cmd, "Failed"))
    assert out["status"] == "failed"
    rec = read_record(pipeline["s3"], run_id)
    assert stage(rec, "build")["outcome"] == "reference run Failed"
    assert "host_stopped_at" in stage(rec, "build")["details"]
    assert len(commands()) == 1
    assert orc.store.lock_holder() is None


def test_existing_reference_goes_straight_to_build(pipeline, orc):
    store_reference(pipeline)
    out = orc.handle(push_event())
    assert out["build"]["action"] == "build_started"
    assert details(pipeline, out["run_id"])["reference"] == {"status": "exists", "key": KEY}
    [cmd] = commands()
    assert any("deploy/build.sh" in c for c in cmd["Parameters"]["commands"])


def test_no_key_configured_skips_reference_check(pipeline):
    out = pipeline["orc"].handle(push_event())
    assert out["build"]["action"] == "build_started"
    assert details(pipeline, out["run_id"])["reference"]["status"] == "skipped"


def test_reference_exists_takes_the_key_as_input(pipeline):
    orc = pipeline["orc"]
    assert orc.reference_exists(KEY) is False
    store_reference(pipeline)
    assert orc.reference_exists(KEY) is True
    assert orc.reference_exists("reference/other/") is False


def test_reference_config_defaults_are_empty_until_decided():
    ref = load_config().reference
    assert isinstance(ref, ReferenceConfig)
    assert ref.key == "" and ref.revision == ""
    assert any("{reference_key}" in c for c in ref.commands)
