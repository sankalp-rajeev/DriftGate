"""Overnight study runner (T-15): resumable, time-boxed, and refuses to start before the
pre-registration is merged into main (FR-11).

  make study STOP_AT=07:00          # run until the next 07:00, finishing the current run
  python -m analysis.study --status # progress

Order (PRD Section 6.8):
  1. one timed A/A run at L3, reporting the time per detector
  2. 20 calibration A/A runs, 3. 10 held-out A/A runs (each a fresh llama-server)
  4. determinism check: bitwise-identical D3 and D5 outputs across the 20 calibration runs
     give 2 repetitions per fault (run seeds 1-2), otherwise 5 (run seeds 1-5)
  5. record the F8 tag (latest llama.cpp release) and build it
  6. fault runs F1 to F9, 7. quantization sweep (D2 and D5 only, once per model)
  8. analysis: calibrate, matrix, RQ2

A unit of work is one run. A run whose result files already exist is skipped, so the study can be
started on consecutive nights. The deadline is checked before each run; a run in progress is
finished. Results and logs go to runs/<date>-study/ (the newest existing one is resumed).
The measurement seed is 0 (the reference's seed; greedy decoding); the run seed (1..R) is recorded
and drives hidden-pool sampling.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

PREREG = "docs/prereg/preregistration.md"
FAULTS_FILE = Path("faults/faults.yaml")
N_CALIBRATION, N_HELD_OUT = 20, 10
FAULTS = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9"]
D2_FAULTS = {"F1", "F2", "F3", "F8"}  # D2 runs only for A/A, these faults and the sweep
SWEEP_NEW = {"Q6_K": "S-Q6_K", "Q5_K_M": "S-Q5_K_M", "Q3_K_M": "S-Q3_K_M"}
SWEEP_REUSED = {"Q8_0": "aa/run-00", "Q4_K_M": "F1/run-00", "Q2_K": "F2/run-00", "F3": "F3/run-00"}
LEVEL = "L3"


# ---------- self-check ----------


def git(*args: str, run=subprocess.run) -> str:
    return run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def prereg_check(faults_file: Path = FAULTS_FILE, run=subprocess.run) -> str | None:
    """None when the pre-registration is merged into origin/main and this checkout includes it
    and matches what it cites; otherwise the reason to refuse."""
    try:
        git("fetch", "--quiet", "origin", "main", run=run)
        commit = git("log", "-1", "--format=%H", "origin/main", "--", PREREG, run=run)
    except (subprocess.CalledProcessError, OSError) as e:
        return f"cannot check the pre-registration against origin/main: {e}"
    if not commit:
        return f"{PREREG} is not on origin/main: the pre-registration is not merged (FR-11)"
    ancestor = run(["git", "merge-base", "--is-ancestor", commit, "HEAD"], capture_output=True)
    if ancestor.returncode != 0:
        return f"this checkout does not include the pre-registration commit {commit[:10]}"
    text = git("show", f"{commit}:{PREREG}", run=run)
    cited = re.search(r"faults/faults\.yaml[^\n]*?([0-9a-f]{64})", text)
    if not cited:
        return "the pre-registration does not cite the SHA-256 of faults/faults.yaml"
    if sha256(faults_file) != cited.group(1):
        return f"{faults_file} differs from the version the pre-registration cites"
    return None


# ---------- time box ----------


def deadline(now: dt.datetime, stop_at: str) -> dt.datetime:
    """The next occurrence of HH:MM after `now`."""
    h, m = (int(x) for x in stop_at.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + dt.timedelta(days=1)


# ---------- state ----------


def study_dir(root: Path = Path("runs"), today: dt.date | None = None) -> Path:
    """Resume the newest runs/<date>-study directory, else start runs/<today>-study."""
    existing = sorted(root.glob("*-study"))
    if existing:
        return existing[-1]
    return root / f"{(today or dt.date.today()).isoformat()}-study"


def deterministic(runs: list[dict]) -> bool:
    """Whether every run gives bitwise-identical D3 (tokens, top logprobs) and D5 outputs."""

    def key(m: dict):
        d3 = [(r["id"], r["tokens"], r["top"]) for r in m["d3"]]
        d5 = [(r["id"], r["predicted"], r["correct"]) for r in m["d5"]]
        return json.dumps([d3, d5], sort_keys=True)

    return len({key(m) for m in runs}) == 1


def repetitions(identical: bool) -> int:
    return 2 if identical else 5


@dataclass
class Unit:
    name: str
    done: Callable[[], bool]
    run: Callable[[], None]


class Study:
    def __init__(self, out: Path, python: str = sys.executable, env: dict | None = None):
        self.out, self.python, self.env = out, python, env or dict(os.environ)
        (out / "logs").mkdir(parents=True, exist_ok=True)

    def log(self, msg: str) -> None:
        line = f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}"
        print(line, flush=True)
        with (self.out / "study.log").open("a") as f:
            f.write(line + "\n")

    def sh(self, name: str, cmd: list[str], env: dict | None = None) -> None:
        with (self.out / "logs" / f"{name}.log").open("ab") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env or self.env)
        if proc.returncode != 0:
            raise RuntimeError(f"{name} exited {proc.returncode}; see logs/{name}.log")

    def aa(self, name: str, bundle: Path, out: Path, i: int, extra: list[str], env=None) -> Unit:
        cmd = [
            self.python, "-m", "faults.aa", "--runs", "1", "--start", str(i), "--seed", "0",
            "--level", LEVEL, "--bundle", str(bundle), "--out", str(out), *extra,
        ]  # fmt: skip
        return Unit(
            name,
            lambda: (out / f"run-{i:02d}.json").exists(),
            lambda: self.sh(name, cmd, env() if callable(env) else env),
        )

    def bundle(self, fault: str) -> Path:
        path = Path("artifacts/bundles") / fault
        if not (path / "bundle.json").exists():
            self.sh(f"bundle-{fault}", [self.python, "-m", "faults.inject", fault])
        return path

    # ---- the plan, in the pre-registered order ----

    def plan(self) -> Iterator[Unit]:
        aa_dir = self.out / "aa"
        yield from self.timing()
        for i in range(N_CALIBRATION + N_HELD_OUT):
            kind = "calibration" if i < N_CALIBRATION else "heldout"
            yield self.aa(f"aa-{i:02d}-{kind}", self.bundle("AA"), aa_dir, i, ["--d2", "--d1"])
        yield self.determinism_unit()
        yield self.f8_unit()
        reps = self.repetition_count()
        for fault in FAULTS:
            for k in range(reps):
                bundle = self.bundle(fault) if fault != "F8" else Path("artifacts/bundles/F8")
                extra = ["--d1", "--run-seed", str(k + 1)] + (
                    ["--d2"] if fault in D2_FAULTS else []
                )
                env = self.f8_env if fault == "F8" else None
                yield self.aa(
                    f"{fault}-{k + 1}", bundle, self.out / "faults" / fault, k, extra, env
                )
        for model, fault in SWEEP_NEW.items():
            extra = ["--d2", "--d5-only"]
            yield self.aa(
                f"sweep-{model}", self.bundle(fault), self.out / "sweep" / model, 0, extra
            )
        yield self.analysis_unit()

    def timing(self) -> Iterator[Unit]:
        unit = self.aa("timing", self.bundle("AA"), self.out / "timing", 0, ["--d2", "--d1"])
        yield unit

        def report():
            rec = json.loads((self.out / "timing/run-00.json").read_text())
            sec = rec["seconds"]
            doc = {"level": LEVEL, "seconds_per_detector": sec, "total_s": sum(sec.values())}
            (self.out / "timing.json").write_text(json.dumps(doc, indent=2) + "\n")
            self.log(f"timed A/A run at {LEVEL}: {json.dumps(sec)}")

        yield Unit("timing-report", lambda: (self.out / "timing.json").exists(), report)

    def determinism_unit(self) -> Unit:
        from analysis.calibrate import load_runs

        def run():
            runs = load_runs(self.out / "aa")[:N_CALIBRATION]
            same = deterministic(runs)
            doc = {"identical": same, "repetitions": repetitions(same), "runs_compared": len(runs)}
            (self.out / "determinism.json").write_text(json.dumps(doc, indent=2) + "\n")
            self.log(f"determinism: {json.dumps(doc)}")

        return Unit("determinism", lambda: (self.out / "determinism.json").exists(), run)

    def repetition_count(self) -> int:
        return json.loads((self.out / "determinism.json").read_text())["repetitions"]

    def f8_env(self) -> dict:
        info = json.loads((self.out / "f8.json").read_text())
        return {**self.env, "LLAMA_CPP_BIN": info["bin_dir"]}

    def f8_unit(self) -> Unit:
        def run():
            src = Path(os.environ.get("DRIFTGATE_LLAMA_CPP_DIR", "~/llama.cpp")).expanduser()
            work = src.with_name(src.name + "-f8")
            tag = subprocess.run(
                ["gh", "api", "repos/ggml-org/llama.cpp/releases/latest", "-q", ".tag_name"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()  # fmt: skip
            self.log(f"F8 tag: {tag}")
            git("-C", str(src), "fetch", "--quiet", "--tags", "origin")
            commit = git("-C", str(src), "rev-list", "-n", "1", tag)
            if not work.exists():
                git("-C", str(src), "worktree", "add", "--detach", str(work), commit)
            self.sh("f8-cmake", ["cmake", "-S", str(work), "-B", str(work / "build"),
                                 "-DCMAKE_BUILD_TYPE=Release", "-DLLAMA_CURL=OFF"])  # fmt: skip
            self.sh("f8-build", ["cmake", "--build", str(work / "build"), "-j", str(os.cpu_count()),
                                 "--target", "llama-server", "llama-perplexity"])  # fmt: skip
            bundle = self.bundle("AA")
            f8 = Path("artifacts/bundles/F8")
            if f8.exists():
                shutil.rmtree(f8)
            shutil.copytree(bundle, f8)
            lock = f8 / "serving/llama_cpp.lock"
            lines = [
                f"commit: {commit}" if x.startswith("commit:") else x
                for x in lock.read_text().splitlines()
            ]
            lock.write_text("\n".join(lines) + "\n")
            meta = json.loads((f8 / "bundle.json").read_text())
            text = f"llama.cpp build bump to release {tag} ({commit})"
            meta |= {"fault": "F8", "layer": "engine", "description": text}
            (f8 / "bundle.json").write_text(json.dumps(meta, indent=2) + "\n")
            bin_dir = str(work / "build/bin")
            info = {"tag": tag, "commit": commit, "bin_dir": bin_dir}
            (self.out / "f8.json").write_text(json.dumps(info, indent=2) + "\n")

        return Unit("f8-record-and-build", lambda: (self.out / "f8.json").exists(), run)

    def analysis_unit(self) -> Unit:
        def run():
            from analysis import calibrate, matrix, rq2
            from gate import reference as ref_store
            from gate.manifest import load_yaml

            gate_config = load_yaml(Path("gate/config.yaml"))
            key = ref_store.resolve_key(gate_config, {})
            ref_path = ref_store.local_dir(gate_config, key) / ref_store.MEASUREMENT_FILE
            cal_dir = self.out / "calibration"
            args = ["--runs", str(self.out / "aa"), "--reference", str(ref_path)]
            calibrate.main(args + ["--out-dir", str(cal_dir)])
            fdirs = [self.out / "faults" / f for f in FAULTS]
            faults = [str(d) for d in fdirs if d.is_dir()]
            matrix.main(
                [
                    "--reference",
                    str(ref_path),
                    "--thresholds-dir",
                    str(cal_dir),
                    "--faults",
                    *faults,
                ]
                + ["--out", str(self.out / "matrix.csv")]
            )
            reference = json.loads(ref_path.read_text())
            sweep = {}
            for name, rel in SWEEP_REUSED.items():
                d, run_name = rel.split("/")
                sweep[name] = calibrate.load_runs(
                    self.out / ("aa" if d == "aa" else f"faults/{d}")
                )[0]
            for name in SWEEP_NEW:
                sweep[name] = calibrate.load_runs(self.out / "sweep" / name)[0]
            points = rq2.points_from_runs(reference, sweep)
            (self.out / "rq2-points.json").write_text(json.dumps(points, indent=2) + "\n")
            rq2.main(["--points", str(self.out / "rq2-points.json"),
                      "--out", str(self.out / "rq2.json")])  # fmt: skip

        return Unit("analysis", lambda: (self.out / "rq2.json").exists(), run)


# ---------- main loop and status ----------


def execute(study: Study, stop: dt.datetime, now=dt.datetime.now, sleep=time.sleep) -> str:
    """Run units until done or the deadline; returns 'complete' or 'stopped'."""
    for unit in study.plan():
        if unit.done():
            continue
        if now() >= stop:
            study.log(f"deadline {stop:%H:%M} reached; next unit is {unit.name}")
            return "stopped"
        study.log(f"start {unit.name}")
        t0 = time.monotonic()
        unit.run()
        study.log(f"done {unit.name} in {time.monotonic() - t0:.0f}s")
    study.log("study complete")
    return "complete"


def status(out: Path) -> dict:
    def n(pattern: str) -> int:
        return sum(1 for f in out.glob(pattern) if re.fullmatch(r"run-\d+\.json", f.name))

    reps = None
    if (out / "determinism.json").exists():
        reps = json.loads((out / "determinism.json").read_text())["repetitions"]
    return {
        "dir": str(out),
        "timing": (out / "timing.json").exists(),
        "aa_runs": f"{n('aa/run-*.json')}/{N_CALIBRATION + N_HELD_OUT}",
        "determinism": reps,
        "f8": (out / "f8.json").exists(),
        "fault_runs": {f: n(f"faults/{f}/run-*.json") for f in FAULTS},
        "repetitions_per_fault": reps,
        "sweep_runs": n("sweep/*/run-00.json"),
        "analysis": (out / "rq2.json").exists(),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="analysis.study", description=__doc__.splitlines()[0])
    p.add_argument("--stop-at", help="HH:MM, the next occurrence is the deadline")
    p.add_argument("--status", action="store_true")
    p.add_argument("--out", type=Path, help="default: newest runs/*-study, else runs/<today>-study")
    args = p.parse_args(argv)
    out = args.out or study_dir()
    if args.status:
        print(json.dumps(status(out), indent=2))
        return 0
    if not args.stop_at:
        p.error("--stop-at HH:MM is required (make study STOP_AT=07:00)")
    reason = prereg_check()
    if reason:
        print(f"refusing to start: {reason}", file=sys.stderr)
        return 2
    study = Study(out)
    result = execute(study, deadline(dt.datetime.now(), args.stop_at))
    print(json.dumps({"result": result, **status(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
