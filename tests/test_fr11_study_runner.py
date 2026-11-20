"""FR-11: the overnight study runner is resumable, time-boxed and refuses to start before the
pre-registration is merged into main. Fakes only: no model, no server, no git network."""

import datetime as dt
import hashlib
import json
import subprocess

from analysis import study
from gate.reference import load_baseline

BASELINE = load_baseline()


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
    assert st["aa_runs"] == "1/30" and st["fault_runs"]["F1"] == 1 and st["fault_runs"]["F2"] == 0
