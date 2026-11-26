"""FR-11: the overnight study runner is resumable, time-boxed and refuses to start before the
pre-registration is merged into main. Fakes only: no model, no server, no git network."""

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from baseline_serving import CONFIG as BASELINE_CONFIG

from analysis import study
from gate.llama_client import detector_error
from gate.reference import load_baseline

BASELINE = load_baseline()


@pytest.fixture
def tagged_repo(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    study.git("-C", str(repo), "init", "--quiet")
    for tag, date, annotation in (
        ("b12560", "2026-11-20T03:00:00+00:00", None),
        ("b12558", "2026-11-19T22:00:00+00:00", None),
        ("b12557", "2026-11-19T23:21:24+01:00", None),
        ("b12556", "2026-11-19T20:00:00+00:00", "2026-11-20T02:30:00+00:00"),
        ("v1.0.0", "2026-11-20T02:00:00+00:00", None),
        ("b12561-preview", "2026-11-20T01:00:00+00:00", None),
    ):
        cmd = ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org"]
        subprocess.run(
            [*cmd, "commit", "--quiet", "--allow-empty", "--date", date, "-m", tag],
            env={"GIT_COMMITTER_DATE": date},
            check=True,
        )
        args = ["-m", tag] if annotation else []
        subprocess.run(
            [*cmd, "tag", *args, tag], env={"GIT_COMMITTER_DATE": annotation or date}, check=True
        )
    return repo


def test_f8_uses_newest_numbered_tag_created_by_study_start(tagged_repo):
    cutoff = dt.datetime.fromisoformat("2026-11-19T21:29:00-05:00")
    assert study.select_f8_tag(tagged_repo, cutoff) == {
        "tag": "b12557",
        "commit": study.git("-C", str(tagged_repo), "rev-parse", "b12557^{commit}"),
        "date": "2026-11-19T22:21:24+00:00",
    }


def test_f8_does_not_fall_back_to_an_ineligible_release(tagged_repo):
    with pytest.raises(ValueError, match="no numbered llama.cpp release tag"):
        study.select_f8_tag(tagged_repo, dt.datetime(2026, 11, 19, 19, tzinfo=dt.UTC))


def test_study_resume_uses_original_timing_start(tmp_path):
    (tmp_path / "study.log").write_text(
        "2026-11-19T21:29:45-05:00 start timing\n2026-11-20T11:00:00-05:00 start F7-1\n"
    )
    assert study.study_started_at(tmp_path) == dt.datetime.fromisoformat(
        "2026-11-20T02:29:45+00:00"
    )


def test_f8_resume_requires_candidate_version_to_match_the_selected_commit(tmp_path):
    runner = study.Study(tmp_path)
    (tmp_path / "f8.json").write_text(
        json.dumps(
            {
                "tag": "b12557",
                "commit": "b94959a64d011915d77a5381c7d4846dcda99530",
                "date": "2026-11-19T22:21:24+00:00",
                "study_start": "2026-11-19T21:29:00-05:00",
            }
        )
    )
    record = tmp_path / "run-00.json"
    record.write_text("{}")
    assert not runner.f8_run_matches(record)
    binary = {
        "version": "version: 1.0.0-dev (build 12359, commit b1b770d90)",
        "commit": "b1b770d90",
        "build_info": "b12359-b1b770d90",
        "baseline_build_info": "b12491-8c16babba",
    }
    record.write_text(json.dumps({"server_binary": binary}))
    assert not runner.f8_run_matches(record)
    binary.update(
        version="version: 1.0.0-dev (build 12557, commit b94959a64)",
        commit="b94959a64",
        build_info="b12557-b94959a64",
    )
    record.write_text(json.dumps({"server_binary": binary}))
    assert runner.f8_run_matches(record)


class FakeGit:
    """Stands in for subprocess.run for git: `log` result, `show` text and ancestor result."""

    def __init__(
        self,
        commit="abc123",
        text="",
        ancestor=True,
        fail_fetch=False,
        deviations=None,
        missing_deviation=False,
    ):
        self.commit, self.text, self.ancestor, self.fail_fetch = commit, text, ancestor, fail_fetch
        self.deviations = deviations or {}
        self.missing_deviation = missing_deviation

    def __call__(self, cmd, capture_output=True, text=True, check=False):
        sub = cmd[1]
        if sub == "fetch" and self.fail_fetch:
            raise subprocess.CalledProcessError(128, cmd)
        out = {"log": self.commit, "show": self.text}.get(sub, "")
        if sub == "ls-tree":
            out = "\n".join(self.deviations)
        if sub == "log" and cmd[-1] in self.deviations:
            out = "deviation123"
        if sub == "show":
            path = cmd[2].split(":", 1)[1]
            out = self.deviations.get(path, self.text)
            if path == study.PREREG:
                out += (
                    f"\n| Reference serving config | model SHA-256 `{BASELINE['model_sha256']}`, "
                    f"config hash `{BASELINE['config_hash']}` |\n"
                )
        code = 0 if sub != "merge-base" or self.ancestor else 1
        if sub == "merge-base" and cmd[3] == "deviation123" and self.missing_deviation:
            code = 1
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")


def _faults_file(tmp_path, body="faults: {}\n"):
    f = tmp_path / "faults.yaml"
    f.write_text(body)
    return f, hashlib.sha256(body.encode()).hexdigest()


def test_refuses_before_the_preregistration_is_on_main(tmp_path):
    f, _ = _faults_file(tmp_path)
    assert "not merged" in study.prereg_check(f, run=FakeGit(commit=""))
    assert "origin/main" in study.prereg_check(f, run=FakeGit(fail_fetch=True))


def test_refuses_when_the_checkout_lacks_the_preregistration_commit(tmp_path):
    f, _ = _faults_file(tmp_path)
    assert "does not include" in study.prereg_check(f, run=FakeGit(ancestor=False))


def test_refuses_when_faults_yaml_differs_from_the_cited_hash(tmp_path):
    f, digest = _faults_file(tmp_path)
    assert (
        study.prereg_check(f, run=FakeGit(text=f"`faults/faults.yaml` SHA-256: {digest}")) is None
    )
    other = "0" * 64
    msg = study.prereg_check(f, run=FakeGit(text=f"faults/faults.yaml SHA-256 {other}"))
    assert "differs" in msg
    assert "does not cite" in study.prereg_check(f, run=FakeGit(text="nothing"))


def _deviation(digest, baseline=BASELINE):
    return (
        "---\n"
        f"faults_sha256: {digest}\n"
        "baseline:\n"
        f"  model_sha256: {baseline['model_sha256']}\n"
        f"  config_hash: {baseline['config_hash']}\n"
        "---\n# Thread-count deviation\n"
    )


def test_latest_merged_deviation_supersedes_prereg_fault_hash_and_baseline(tmp_path):
    f, digest = _faults_file(tmp_path)
    baseline = dict(BASELINE, config_hash="a" * 64)
    baseline_file = tmp_path / "baseline.yaml"
    baseline_file.write_text(json.dumps(baseline))
    deviations = {
        "docs/deviations/2026-11-19-thread-count.md": _deviation(digest, baseline),
        "docs/deviations/2026-11-18-old.md": "superseded",
    }
    run = FakeGit(text=f"faults/faults.yaml SHA-256 {'0' * 64}", deviations=deviations)
    assert study.prereg_check(f, run=run, baseline_file=baseline_file) is None


def test_checkout_must_include_the_latest_merged_deviation(tmp_path):
    f, digest = _faults_file(tmp_path)
    run = FakeGit(
        deviations={"docs/deviations/2026-11-19-thread-count.md": _deviation(digest)},
        missing_deviation=True,
    )
    assert "does not include the latest deviation" in study.prereg_check(f, run=run)


def test_refuses_a_baseline_not_cited_by_the_latest_deviation(tmp_path):
    f, digest = _faults_file(tmp_path)
    doc = _deviation(digest, dict(BASELINE, config_hash="a" * 64))
    run = FakeGit(deviations={"docs/deviations/2026-11-19-thread-count.md": doc})
    assert "baseline" in study.prereg_check(f, run=run)


def test_refuses_a_baseline_not_cited_by_the_preregistration(tmp_path):
    f, digest = _faults_file(tmp_path)
    baseline_file = tmp_path / "baseline.yaml"
    baseline_file.write_text(json.dumps(dict(BASELINE, config_hash="a" * 64)))
    run = FakeGit(text=f"faults/faults.yaml SHA-256 {digest}")
    assert "baseline" in study.prereg_check(f, run=run, baseline_file=baseline_file)


def test_latest_deviation_does_not_allow_a_changed_fault_file(tmp_path):
    f, _ = _faults_file(tmp_path)
    run = FakeGit(deviations={"docs/deviations/2026-11-19-thread-count.md": _deviation("a" * 64)})
    assert "differs" in study.prereg_check(f, run=run)


def test_malformed_latest_deviation_does_not_fall_back_to_prereg(tmp_path):
    f, digest = _faults_file(tmp_path)
    run = FakeGit(
        text=f"faults/faults.yaml SHA-256 {digest}",
        deviations={"docs/deviations/2026-11-19-thread-count.md": "no pins"},
    )
    assert "front matter" in study.prereg_check(f, run=run)


def test_main_refuses_to_start_without_the_preregistration(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(study, "prereg_check", lambda: "not merged")
    assert study.main(["--stop-at", "07:00", "--out", str(tmp_path)]) == 2
    assert "refusing to start" in capsys.readouterr().err


def test_deadline_is_the_next_occurrence_of_the_clock_time():
    at = dt.datetime
    assert study.deadline(at(2026, 11, 20, 22, 0), "07:00") == at(2026, 11, 21, 7, 0)
    assert study.deadline(at(2026, 11, 21, 2, 0), "07:00") == at(2026, 11, 21, 7, 0)
    assert study.deadline(at(2026, 11, 21, 7, 0), "07:00") == at(2026, 11, 22, 7, 0)


def test_study_dir_resumes_the_newest_existing_directory(tmp_path):
    assert study.study_dir(tmp_path, dt.date(2026, 11, 20)) == tmp_path / "2026-11-20-study"
    (tmp_path / "2026-11-20-study").mkdir()
    (tmp_path / "2026-11-21-study").mkdir()
    assert study.study_dir(tmp_path, dt.date(2026, 11, 22)) == tmp_path / "2026-11-21-study"


def _m(tokens=(1, 2), correct=True):
    return {
        "d3": [{"id": 0, "tokens": list(tokens), "top": [[[1, -0.1]]], "timings": {"x": 1}}],
        "d5": [{"id": 0, "predicted": "A", "correct": correct}],
    }


def test_determinism_check_sets_two_or_five_repetitions():
    same = [_m() for _ in range(20)]
    assert study.deterministic(same) and study.repetitions(True) == 2
    differs = same[:19] + [_m(tokens=(1, 3))]
    assert not study.deterministic(differs) and study.repetitions(False) == 5
    assert not study.deterministic(same[:19] + [_m(correct=False)])
    timing_only = _m()
    timing_only["d3"][0]["timings"]["x"] = 99  # latency does not count
    assert study.deterministic([_m(), timing_only])


class FakePlan(study.Study):
    def __init__(self, out, names, stop_after=None):
        super().__init__(out)
        self.names, self.ran = names, []

    def plan(self):
        for n in self.names:
            f = self.out / f"{n}.done"
            yield study.Unit(n, f.exists, lambda f=f, n=n: (f.write_text("x"), self.ran.append(n)))


def test_resumes_by_skipping_finished_runs(tmp_path):
    (tmp_path / "a.done").write_text("x")
    s = FakePlan(tmp_path, ["a", "b", "c"])
    far = dt.datetime(2099, 1, 1)
    assert study.execute(s, far) == "complete"
    assert s.ran == ["b", "c"]
    s2 = FakePlan(tmp_path, ["a", "b", "c"])
    assert study.execute(s2, far) == "complete" and s2.ran == []


def test_stops_at_the_deadline_after_finishing_the_current_run(tmp_path):
    clock = iter([dt.datetime(2026, 11, 21, 6, 59), dt.datetime(2026, 11, 21, 7, 0)])
    s = FakePlan(tmp_path, ["a", "b", "c"])
    stop = dt.datetime(2026, 11, 21, 7, 0)
    assert study.execute(s, stop, now=lambda: next(clock)) == "stopped"
    assert s.ran == ["a"]  # a ran to completion; b was not started
    assert "deadline 07:00 reached; next unit is b" in (tmp_path / "study.log").read_text()


def test_failed_run_is_saved_with_traceback_and_next_run_still_executes(tmp_path):
    class FailingPlan(study.Study):
        def plan(self):
            def fail():
                raise RuntimeError("candidate process exited 2")

            yield study.Unit("F7-1", lambda: False, fail)
            yield study.Unit("F7-2", lambda: False, lambda: (self.out / "next.done").touch())

    runner = FailingPlan(tmp_path)
    assert study.execute(runner, dt.datetime(2099, 1, 1)) == "incomplete"
    assert (tmp_path / "next.done").exists()
    failure = json.loads((tmp_path / "failures/F7-1.json").read_text())
    assert failure["status"] == "failed"
    assert "RuntimeError: candidate process exited 2" in failure["attempts"][0]["traceback"]
    assert "F7-1" in study.status(tmp_path)["failed_runs"]
    log = (tmp_path / "study.log").read_text()
    for event in ("start F7-1", "failed F7-1", "done F7-1", "start F7-2", "done F7-2"):
        assert event in log


def test_partial_fault_run_is_not_skipped_on_resume(tmp_path):
    directory = tmp_path / "faults/F7"
    directory.mkdir(parents=True)
    (directory / "run-00.json").write_text('{"schema": "driftgate.aa-run/1"}')
    runner = study.Study(tmp_path)
    unit = runner.aa(
        "F7-1", Path("artifacts/bundles/F7"), directory, 0, ["--d1", "--run-seed", "1"]
    )
    assert unit.done() is False
    assert study.status(tmp_path)["fault_runs"]["F7"] == 0
    assert "faults/F7/run-00.json" in study.status(tmp_path)["invalid_runs"]


def _complete_fault(directory, fault="F7", error=False):
    """A known complete L3 record without any model execution."""
    directory.mkdir(parents=True)
    path = directory / "run-00.json"
    inputs = {"config_sha256": study.sha256(BASELINE_CONFIG)}
    m = {
        "schema": "driftgate.measurement/1",
        "seed": 0,
        "budget": 64,
        "d5_budget": 256,
        "inputs": inputs,
    }
    for detector, key, n in (("d3", "d3_prompts", 64), ("d5", "d5_panel", 256)):
        file = Path("gate/data") / ("d3_prompts.jsonl" if detector == "d3" else "d5_panel.jsonl")
        inputs[key], inputs[key + "_sha256"] = str(file), study.sha256(file)
        ids = [json.loads(line)["id"] for line in file.read_text().splitlines()][:n]
        row = (
            {
                "tokens": [1],
                "top": [[[1, 0.0]]],
                "timings": {"prompt_ms": 10, "predicted_ms": 10, "prompt_n": 1, "predicted_n": 1},
            }
            if detector == "d3"
            else {"predicted": "A", "correct": True}
        )
        m[detector] = [{"id": item_id, **row} for item_id in ids]
    if error:
        m["d5"] = [
            {
                "id": m["d5"][0]["id"],
                "error": {"status_code": 400, "message": "context", "kind": "http"},
            }
        ]
    record = {
        "schema": "driftgate.aa-run/1",
        "seed": 0,
        "run_seed": 1,
        "run": 0,
        "level": "L3",
        "budget": 64,
        "d5_budget": 256,
        "bundle": {"fault": fault, "files": {"serving/config.yaml": inputs["config_sha256"]}},
        "verdict": {
            "schema": "driftgate.verdict/2",
            "seed": 0,
            "budget": 64,
            "candidate": {"inputs": inputs},
            "decision": {"exit_code": 1 if error else 0},
            "detectors": {
                "D3": {"mean_agreement_share": 1.0},
                "D5": detector_error(m["d5"]) or {"accuracy_drop": 0.0},
                "D6": {"ratio": {"p95_itl_ms": 1.0}},
            },
        },
    }
    path.write_text(json.dumps(record))
    path.with_suffix(".measurement.json").write_text(json.dumps(m))
    path.with_suffix(".d1.json").write_text('{"ok": true}')
    return path


@pytest.mark.parametrize("error", [False, True])
def test_complete_faults_including_error_detections_are_skipped(tmp_path, error):
    directory = tmp_path / "faults/F7"
    path = _complete_fault(directory, error=error)
    runner = study.Study(tmp_path)
    unit = runner.aa(
        "F7-1", Path("artifacts/bundles/F7"), directory, 0, ["--d1", "--run-seed", "1"]
    )
    assert unit.done() is True
    assert study.status(tmp_path)["fault_runs"]["F7"] == 1
    path.with_suffix(".d1.json").unlink()
    assert unit.done() is False
    path.with_suffix(".d1.json").write_text("broken JSON")
    assert unit.done() is False


def test_failure_does_not_block_later_runs_when_fault_bundle_is_missing(tmp_path):
    class MissingBundle(study.Study):
        def plan(self):
            def missing():
                raise FileNotFoundError("fault bundle is unavailable")

            yield self.aa("F7-1", missing, self.out / "faults/F7", 0, ["--d1"])
            yield study.Unit("F9-1", lambda: False, lambda: (self.out / "next.done").touch())

    assert study.execute(MissingBundle(tmp_path), dt.datetime(2099, 1, 1)) == "incomplete"
    assert (tmp_path / "next.done").exists()


def test_failed_attempt_history_survives_a_successful_resume(tmp_path):
    class RetryPlan(study.Study):
        broken = True

        def plan(self):
            def run():
                if self.broken:
                    raise SystemExit("candidate failed")
                (self.out / "complete").touch()

            yield study.Unit("F7-1", (self.out / "complete").exists, run)

    runner = RetryPlan(tmp_path)
    assert study.execute(runner, dt.datetime(2099, 1, 1)) == "incomplete"
    assert study.execute(runner, dt.datetime(2099, 1, 1)) == "incomplete"
    runner.broken = False
    assert study.execute(runner, dt.datetime(2099, 1, 1)) == "complete"
    assert study.status(tmp_path)["failed_runs"] == {}
    history = json.loads((tmp_path / "failures/F7-1.json").read_text())
    assert history["status"] == "recovered" and len(history["attempts"]) == 2
    assert "SystemExit: candidate failed" in history["attempts"][0]["traceback"]


def test_closed_console_does_not_lose_study_log_event(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise BrokenPipeError("console closed")

    monkeypatch.setattr("builtins.print", broken)
    study.Study(tmp_path).log("start F7-1")
    assert "start F7-1" in (tmp_path / "study.log").read_text()


def test_missing_determinism_does_not_abort_planning_independent_sweep_runs(tmp_path):
    runner = study.Study(tmp_path)
    names = [unit.name for unit in runner.plan()]
    assert "fault-plan" in names
    assert "sweep-Q6_K" in names
    assert names[-1] == "analysis"


def test_plan_order_and_repetition_count_follow_the_prereg(tmp_path, monkeypatch):
    s = study.Study(tmp_path)
    monkeypatch.setattr(s, "bundle", lambda f: tmp_path / f)
    (tmp_path / "determinism.json").write_text(json.dumps({"repetitions": 2}))
    names = [u.name for u in s.plan()]
    assert names[:2] == ["timing", "timing-report"]
    assert names[2:12] == [f"aa-{i:02d}-calibration" for i in range(10)]
    assert (
        names.index("aa-29-heldout")
        < names.index("determinism")
        < names.index("f8-record-and-build")
    )
    faults = [n for n in names if n[:1] == "F"]
    assert faults[0] == "F1-1" and faults[1] == "F1-2" and faults[-1] == "F9-2"
    assert names.index("F9-2") < names.index("sweep-Q6_K") < names.index("analysis")
    assert names[-1] == "analysis"


def test_status_counts_finished_runs(tmp_path):
    (tmp_path / "aa").mkdir()
    (tmp_path / "aa/run-00.json").write_text("{}")
    (tmp_path / "aa/run-00.d2.json").write_text("{}")  # not a finished run on its own
    (tmp_path / "faults/F1").mkdir(parents=True)
    (tmp_path / "faults/F1/run-00.json").write_text("{}")
    st = study.status(tmp_path)
    assert st["aa_runs"] == "0/30" and st["fault_runs"]["F1"] == 0 and st["fault_runs"]["F2"] == 0
    assert st["invalid_runs"] == ["aa/run-00.json", "faults/F1/run-00.json"]
