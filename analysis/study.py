"""Overnight study runner (T-15): resumable, time-boxed, and refuses to start before the
pre-registration is merged into main (FR-11). The latest dated deviation merged into main
supersedes its fault hash and baseline pins; that deviation must be in this checkout too.

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
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

from gate.llama_client import detector_error
from gate.manifest import load_yaml
from gate.reference import DEFAULT_BASELINE, load_baseline

PREREG = "docs/prereg/preregistration.md"
DEVIATIONS = "docs/deviations"
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


def prereg_check(
    faults_file: Path = FAULTS_FILE,
    run=subprocess.run,
    baseline_file: Path = DEFAULT_BASELINE,
) -> str | None:
    """Validate merged pre-registration/deviation pins without starting any measurements."""
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
    try:
        text = git("show", f"{commit}:{PREREG}", run=run)
        paths = git("ls-tree", "-r", "--name-only", "origin/main", "--", DEVIATIONS, run=run)
        deviations = sorted(
            p
            for p in paths.splitlines()
            if re.fullmatch(r"docs/deviations/\d{4}-\d{2}-\d{2}-.+\.md", p)
        )
        source = "the pre-registration"
        if deviations:
            source = deviations[-1]
            revision = git("log", "-1", "--format=%H", "origin/main", "--", source, run=run)
            ancestor = run(
                ["git", "merge-base", "--is-ancestor", revision, "HEAD"], capture_output=True
            )
            if not revision or ancestor.returncode != 0:
                return f"this checkout does not include the latest deviation {source}"
            text = git("show", f"origin/main:{source}", run=run)
            front = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.DOTALL)
            if not front:
                return f"{source} must have YAML front matter with faults_sha256 and baseline"
            doc = yaml.safe_load(front.group(1))
            cited_hash, cited_baseline = doc["faults_sha256"], doc["baseline"]
        else:
            cited = re.search(r"faults/faults\.yaml[^\n]*?([0-9a-f]{64})", text)
            if not cited:
                return "the pre-registration does not cite the SHA-256 of faults/faults.yaml"
            cited_hash = cited.group(1)
            row = re.search(r"Reference serving config[^\n]+", text)
            hashes = re.findall(r"[0-9a-f]{64}", row.group() if row else "")
            if len(hashes) != 2:
                return "the pre-registration does not cite the baseline model and config hashes"
            cited_baseline = dict(zip(("model_sha256", "config_hash"), hashes, strict=True))
        if not isinstance(cited_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", cited_hash):
            return f"{source}: faults_sha256 must be a SHA-256 hex digest"
        if sha256(faults_file) != cited_hash:
            return f"{faults_file} differs from the version {source} cites"
        baseline = load_baseline(baseline_file)
        if any(
            baseline[field] != cited_baseline[field] for field in ("model_sha256", "config_hash")
        ):
            return f"{baseline_file} differs from the baseline {source} cites"
    except (
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        yaml.YAMLError,
    ) as e:
        return f"cannot validate the pre-registration/deviation: {e}"
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


def study_started_at(out: Path) -> dt.datetime:
    """The original, possibly pre-timezone-log, timing start rather than a resume time."""
    with (out / "study.log").open() as log:
        for line in log:
            timestamp, _, event = line.strip().partition(" ")
            if event == "start timing":
                return dt.datetime.fromisoformat(timestamp).astimezone(dt.UTC)
    raise ValueError("study.log has no original timing start")


def select_f8_tag(src: Path, cutoff: dt.datetime) -> dict[str, str]:
    """Select the most recent numbered build tag, using Git's creator timestamp."""
    if cutoff.utcoffset() is None:
        raise ValueError("F8 cutoff needs a timezone")
    rows = git(
        "-C",
        str(src),
        "for-each-ref",
        "--sort=-version:refname",
        "--sort=-creatordate",
        "--format=%(refname:short) %(creatordate:iso-strict)",
        "refs/tags/b*",
    )
    for row in rows.splitlines():
        tag, _, stamp = row.partition(" ")
        if not re.fullmatch(r"b[0-9]+", tag):
            continue
        date = dt.datetime.fromisoformat(stamp).astimezone(dt.UTC)
        if date <= cutoff:
            commit = git("-C", str(src), "rev-parse", f"refs/tags/{tag}^{{commit}}")
            return {"tag": tag, "commit": commit, "date": date.isoformat()}
    raise ValueError(f"no numbered llama.cpp release tag at or before {cutoff.isoformat()}")


def run_complete(
    path: Path,
    *,
    require_d1: bool = True,
    require_d2: bool = False,
    d5_only: bool = False,
    fault: str | None = None,
    run_seed: int | None = None,
) -> bool:
    """A complete study run has parseable, matching record, measurement and sidecars.

    A terminal request error is completed detector evidence, not an unfinished run.
    Historical successful measurements remain valid under the error-handling deviation.
    """
    try:
        record = json.loads(path.read_text())
        m = json.loads(path.with_suffix(".measurement.json").read_text())
        lv = load_yaml(Path("gate/config.yaml"))["levels"][LEVEL]
        if (
            record["schema"] != "driftgate.aa-run/1"
            or m["schema"] != "driftgate.measurement/1"
            or record["seed"] != 0
            or m["seed"] != 0
            or record["level"] != LEVEL
            or record["run"] != int(path.stem.removeprefix("run-"))
            or record["budget"] != lv["d3"]
            or record["d5_budget"] != lv["d5"]
            or m["budget"] != lv["d3"]
            or m["d5_budget"] != lv["d5"]
        ):
            return False
        if fault is not None and record["bundle"]["fault"] != fault:
            return False
        if run_seed is not None and record["run_seed"] != run_seed:
            return False
        if m["inputs"]["config_sha256"] != record["bundle"]["files"]["serving/config.yaml"]:
            return False
        for detector, data_key, n, fields in (
            ("d3", "d3_prompts", 0 if d5_only else lv["d3"], {"tokens", "top", "timings"}),
            ("d5", "d5_panel", lv["d5"], {"predicted", "correct"}),
        ):
            data = Path(m["inputs"][data_key])
            if sha256(data) != m["inputs"][data_key + "_sha256"]:
                return False
            expected_ids = [json.loads(line)["id"] for line in data.read_text().splitlines()][:n]
            rows = m[detector]
            if len(rows) > n or (n and not rows):
                return False
            if [r["id"] for r in rows] != expected_ids[: len(rows)]:
                return False
            for index, row in enumerate(rows):
                if "error" in row:
                    error = row["error"]
                    if (
                        index != len(rows) - 1
                        or not isinstance(error["message"], str)
                        or error["kind"] not in ("http", "timeout")
                        or not (
                            error["status_code"] is None or isinstance(error["status_code"], int)
                        )
                    ):
                        return False
                elif not fields <= row.keys():
                    return False
                elif detector == "d3" and (
                    len(row["tokens"]) != len(row["top"])
                    or not {"prompt_ms", "predicted_ms", "prompt_n", "predicted_n"}
                    <= row["timings"].keys()
                ):
                    return False
                elif detector == "d5" and (
                    not isinstance(row["predicted"], str) or not isinstance(row["correct"], bool)
                ):
                    return False
            if len(rows) < n and "error" not in rows[-1]:
                return False
        if not d5_only:
            verdict = record["verdict"]
            if (
                verdict["schema"] != "driftgate.verdict/2"
                or verdict["seed"] != 0
                or verdict["budget"] != lv["d3"]
                or verdict["decision"]["exit_code"] not in (0, 1)
                or verdict["candidate"]["inputs"] != m["inputs"]
                or not {"D3", "D5", "D6"} <= verdict["detectors"].keys()
            ):
                return False
            for detector, rows, metric in (
                ("D3", m["d3"], "mean_agreement_share"),
                ("D5", m["d5"], "accuracy_drop"),
                ("D6", m["d3"], "ratio"),
            ):
                result = verdict["detectors"][detector]
                error = detector_error(rows)
                if (error and result != error) or (not error and metric not in result):
                    return False
        if require_d1 and not isinstance(
            json.loads(path.with_suffix(".d1.json").read_text())["ok"], bool
        ):
            return False
        if require_d2:
            d2 = json.loads(path.with_suffix(".d2.json").read_text())
            if (
                d2["threads"] != 2
                or not {"mean_kld", "p99_kld", "same_top_p_pct"} <= d2["metrics"].keys()
            ):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return False


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
    validate: bool = False


class Study:
    def __init__(
        self,
        out: Path,
        python: str = sys.executable,
        env: dict | None = None,
        reference: Path | None = None,
        include_rq2: bool = True,
    ):
        self.out, self.python, self.env = out, python, env or dict(os.environ)
        self.reference = reference
        self.include_rq2 = include_rq2
        (out / "logs").mkdir(parents=True, exist_ok=True)

    def log(self, msg: str) -> None:
        line = f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {msg}"
        with (self.out / "study.log").open("a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            print(line, flush=True)
        except BrokenPipeError:
            pass  # a closed console must not lose the durable study event

    def failure(self, name: str, error: BaseException) -> None:
        path = self.out / "failures" / f"{name}.json"
        path.parent.mkdir(exist_ok=True)
        doc = json.loads(path.read_text()) if path.exists() else {"attempts": []}
        attempt = {
            "at": dt.datetime.now().isoformat(),
            "error": str(error),
            "traceback": traceback.format_exc(),
        }
        log = self.out / "logs" / f"{name}.log"
        if log.exists():
            attempt["subprocess_log"] = str(log)
            attempt["subprocess_log_tail"] = log.read_text(errors="replace")[-16000:]
        doc["attempts"].append(attempt)
        doc.update(unit=name, status="failed")
        path.write_text(json.dumps(doc, indent=2) + "\n")

    def recovered(self, name: str) -> None:
        path = self.out / "failures" / f"{name}.json"
        if path.exists():
            doc = json.loads(path.read_text())
            doc["status"] = "recovered"
            path.write_text(json.dumps(doc, indent=2) + "\n")

    def sh(self, name: str, cmd: list[str], env: dict | None = None) -> None:
        with (self.out / "logs" / f"{name}.log").open("ab") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env or self.env)
        if proc.returncode != 0:
            raise RuntimeError(f"{name} exited {proc.returncode}; see logs/{name}.log")

    def aa(
        self,
        name: str,
        bundle: Path | Callable[[], Path],
        out: Path,
        i: int,
        extra: list[str],
        env=None,
        fault: str | None = None,
    ) -> Unit:
        def run():
            resolved = bundle() if callable(bundle) else bundle
            cmd = [
            self.python, "-m", "faults.aa", "--runs", "1", "--start", str(i), "--seed", "0",
            "--level", LEVEL, "--bundle", str(resolved), "--out", str(out), *extra,
            ]  # fmt: skip
            if self.reference is not None:
                cmd.extend(["--reference", str(self.reference)])
            self.sh(name, cmd, env() if callable(env) else env)

        seed = int(extra[extra.index("--run-seed") + 1]) if "--run-seed" in extra else i
        record = out / f"run-{i:02d}.json"

        def done():
            return run_complete(
                record,
                require_d1="--d1" in extra,
                require_d2="--d2" in extra,
                d5_only="--d5-only" in extra,
                fault=fault or (bundle.name if isinstance(bundle, Path) else None),
                run_seed=seed,
            ) and (fault != "F8" or self.f8_run_matches(record))

        return Unit(
            name,
            done,
            run,
            True,
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
            yield self.aa(
                f"aa-{i:02d}-{kind}",
                lambda: self.bundle("AA"),
                aa_dir,
                i,
                ["--d2", "--d1"],
                fault="AA",
            )
        yield self.determinism_unit()
        yield self.f8_unit()
        try:
            reps = self.repetition_count()
        except (OSError, ValueError, KeyError, TypeError) as error:

            def unavailable(error=error):
                raise ValueError(
                    f"fault repetitions require a successful determinism check: {error}"
                )

            yield Unit("fault-plan", lambda: False, unavailable)
            reps = 0
        for fault in FAULTS:
            for k in range(reps):
                bundle = (
                    (lambda fault=fault: self.bundle(fault))
                    if fault != "F8"
                    else self.out / "bundles/F8"
                )
                extra = ["--d1", "--run-seed", str(k + 1)] + (
                    ["--d2"] if fault in D2_FAULTS else []
                )
                env = self.f8_env if fault == "F8" else None
                yield self.aa(
                    f"{fault}-{k + 1}", bundle, self.out / "faults" / fault, k, extra, env, fault
                )
        if self.include_rq2:
            for model, fault in SWEEP_NEW.items():
                extra = ["--d2", "--d5-only"]
                yield self.aa(
                    f"sweep-{model}",
                    lambda fault=fault: self.bundle(fault),
                    self.out / "sweep" / model,
                    0,
                    extra,
                    fault=fault,
                )
        yield self.analysis_unit()

    def timing(self) -> Iterator[Unit]:
        unit = self.aa(
            "timing",
            lambda: self.bundle("AA"),
            self.out / "timing",
            0,
            ["--d2", "--d1"],
            fault="AA",
        )
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
            if not all(
                run_complete(self.out / "aa" / f"run-{i:02d}.json", require_d2=True, fault="AA")
                for i in range(N_CALIBRATION)
            ):
                raise ValueError("determinism requires all 20 complete calibration A/A runs")
            runs = load_runs(self.out / "aa")[:N_CALIBRATION]
            same = deterministic(runs)
            doc = {"identical": same, "repetitions": repetitions(same), "runs_compared": len(runs)}
            (self.out / "determinism.json").write_text(json.dumps(doc, indent=2) + "\n")
            self.log(f"determinism: {json.dumps(doc)}")

        return Unit("determinism", lambda: (self.out / "determinism.json").exists(), run)

    def repetition_count(self) -> int:
        count = json.loads((self.out / "determinism.json").read_text())["repetitions"]
        if not isinstance(count, int) or count not in (2, 5):
            raise ValueError(f"invalid repetition count {count!r}")
        return count

    def f8_info(self) -> dict:
        info = json.loads((self.out / "f8.json").read_text())
        date = dt.datetime.fromisoformat(info["date"])
        start = dt.datetime.fromisoformat(info["study_start"])
        if (
            not re.fullmatch(r"b[0-9]+", info["tag"])
            or date.utcoffset() is None
            or start.utcoffset() is None
            or date > start
            or not re.fullmatch(r"[0-9a-f]{40}", info["commit"])
        ):
            raise ValueError("invalid F8 build metadata")
        return info

    def f8_env(self) -> dict:
        info = self.f8_info()
        from gate.serve import llama_bin_dir

        return {
            "DRIFTGATE_EMBEDDING_BIN": str(llama_bin_dir()),
            **self.env,
            "LLAMA_CPP_BIN": info["bin_dir"],
        }

    def f8_run_matches(self, record: Path) -> bool:
        from gate.serve import engine_matches, version_commit

        try:
            info = self.f8_info()
            binary = json.loads(record.read_text())["server_binary"]
            commit = version_commit(binary["version"])
            return (
                engine_matches(info, commit)
                and commit == binary["commit"]
                and binary["build_info"].endswith(f"-{commit}")
                and binary["build_info"] != binary["baseline_build_info"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def f8_unit(self, cutoff: dt.datetime | None = None) -> Unit:
        def done():
            try:
                info = self.f8_info()
                if cutoff is not None and dt.datetime.fromisoformat(info["study_start"]) != cutoff:
                    return False
                lock = load_yaml(self.out / "bundles/F8/serving/llama_cpp.lock")
                return info["commit"] == lock["commit"] and all(
                    (Path(info["bin_dir"]) / name).is_file()
                    for name in ("llama-server", "llama-perplexity")
                )
            except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
                return False

        def run():
            src = Path(os.environ.get("DRIFTGATE_LLAMA_CPP_DIR", "~/llama.cpp")).expanduser()
            git("-C", str(src), "fetch", "--quiet", "--tags", "origin")
            start = cutoff or study_started_at(self.out)
            selected = select_f8_tag(src, start)
            tag, commit = selected["tag"], selected["commit"]
            self.log(f"F8 tag: {tag} ({commit}, {selected['date']})")
            work = Path("artifacts/llama.cpp-f8").resolve() / tag
            if not work.exists():
                work.parent.mkdir(parents=True, exist_ok=True)
                git("clone", "--shared", "--no-checkout", str(src), str(work))
                git("-C", str(work), "checkout", "--detach", commit)
            if git("-C", str(work), "rev-parse", "HEAD") != commit:
                raise ValueError(f"{work} does not contain the selected F8 commit {commit}")
            self.sh("f8-cmake", ["cmake", "-S", str(work), "-B", str(work / "build"),
                                 "-DCMAKE_BUILD_TYPE=Release", "-DLLAMA_CURL=OFF",
                                 "-DLLAMA_USE_PREBUILT_UI=OFF"])  # fmt: skip
            self.sh("f8-build", ["cmake", "--build", str(work / "build"), "-j",
                                 str(min(os.cpu_count() or 1, 12)),
                                 "--target", "llama-server", "llama-perplexity"])  # fmt: skip
            bundle = self.bundle("AA")
            f8 = self.out / "bundles/F8"
            if f8.exists():
                shutil.rmtree(f8)
            shutil.copytree(bundle, f8, ignore=shutil.ignore_patterns("server.log"))
            lock = f8 / "serving/llama_cpp.lock"
            lock.write_text(
                yaml.safe_dump(
                    {"repo": load_yaml(lock)["repo"], "commit": commit, "describe": tag},
                    sort_keys=False,
                )
            )
            meta = json.loads((f8 / "bundle.json").read_text())
            text = f"llama.cpp build bump to release {tag} ({commit})"
            meta |= {"fault": "F8", "layer": "engine", "description": text}
            meta["files"]["serving/llama_cpp.lock"] = sha256(lock)
            (f8 / "bundle.json").write_text(json.dumps(meta, indent=2) + "\n")
            bin_dir = str(work / "build/bin")
            info = {**selected, "study_start": start.isoformat(), "bin_dir": bin_dir}
            (self.out / "f8.json").write_text(json.dumps(info, indent=2) + "\n")

        return Unit("f8-record-and-build", done, run, validate=True)

    def analysis_unit(self) -> Unit:
        matrix_path = self.out / "matrix.csv"

        def run():
            missing = []
            for unit in self.plan():
                if unit.name == "analysis":
                    break
                if not unit.done():
                    missing.append(unit.name)
            if missing:
                raise ValueError(f"analysis requires complete runs: {', '.join(missing)}")
            from analysis import calibrate, matrix, rq2
            from gate import reference as ref_store
            from gate.manifest import load_yaml

            gate_config = load_yaml(Path("gate/config.yaml"))
            key = ref_store.resolve_key(gate_config, {})
            ref_path = self.reference or (
                ref_store.local_dir(gate_config, key) / ref_store.MEASUREMENT_FILE
            )
            cal_dir = self.out / "calibration"
            args = ["--runs", str(self.out / "aa"), "--reference", str(ref_path)]
            calibrate.main(args + ["--out-dir", str(cal_dir)])
            fdirs = [self.out / "faults" / f for f in FAULTS]
            faults = [str(d) for d in fdirs if d.is_dir()]
            pending_matrix = matrix_path.with_suffix(".csv.tmp")
            matrix.main(
                [
                    "--reference",
                    str(ref_path),
                    "--thresholds-dir",
                    str(cal_dir),
                    "--faults",
                    *faults,
                ]
                + ["--out", str(pending_matrix)]
            )
            pending_matrix.replace(matrix_path)
            if not self.include_rq2:
                return
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

        result = self.out / "rq2.json" if self.include_rq2 else matrix_path
        return Unit("analysis", result.exists, run)


# ---------- main loop and status ----------


def execute(study: Study, stop: dt.datetime, now=dt.datetime.now, sleep=time.sleep) -> str:
    """Run units until done or the deadline; returns 'complete' or 'stopped'."""
    failed = False
    for unit in study.plan():
        if unit.done():
            study.recovered(unit.name)
            continue
        if now() >= stop:
            study.log(f"deadline {stop:%H:%M} reached; next unit is {unit.name}")
            return "stopped"
        study.log(f"start {unit.name}")
        t0 = time.monotonic()
        outcome = "ok"
        try:
            unit.run()
            if unit.validate and not unit.done():
                raise ValueError(f"{unit.name} did not produce a complete, valid run")
        except (Exception, SystemExit) as e:
            failed = True
            outcome = "failed"
            study.failure(unit.name, e)
            study.log(f"failed {unit.name}: {type(e).__name__}: {e}")
        else:
            study.recovered(unit.name)
        finally:
            study.log(f"done {unit.name} in {time.monotonic() - t0:.0f}s ({outcome})")
    result = "incomplete" if failed else "complete"
    study.log(f"study {result}")
    return result


def status(out: Path) -> dict:
    invalid = set()

    def complete(path: Path) -> bool:
        sweep = path.parent.parent.name == "sweep"
        fault = (
            path.parent.name
            if path.parent.parent.name == "faults"
            else (SWEEP_NEW.get(path.parent.name) if sweep else "AA")
        )
        ok = run_complete(
            path,
            require_d1=not sweep,
            require_d2=sweep or fault == "AA" or fault in D2_FAULTS,
            d5_only=sweep,
            fault=fault,
        )
        if not ok:
            invalid.add(str(path.relative_to(out)))
        return ok

    def n(pattern: str) -> int:
        return sum(
            1 for f in out.glob(pattern) if re.fullmatch(r"run-\d+\.json", f.name) and complete(f)
        )

    failures = {
        p.stem: {"traceback_file": str(p), "error": doc["attempts"][-1]["error"]}
        for p in sorted((out / "failures").glob("*.json"))
        if (doc := json.loads(p.read_text())).get("status") == "failed"
    }
    # Preserve and expose failures from the original runner, which wrote only child logs.
    for log in (out / "logs").glob("F*-*.log"):
        if not re.fullmatch(r"F[1-9]-\d+", log.stem):
            continue
        fault, repetition = log.stem.split("-")
        record = out / "faults" / fault / f"run-{int(repetition) - 1:02d}.json"
        text = log.read_text(errors="replace")
        if "Traceback (most recent call last)" in text and not (
            record.exists() and complete(record)
        ):
            failures.setdefault(
                log.stem, {"traceback_file": str(log), "error": text.splitlines()[-1]}
            )

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
        "failed_runs": failures,
        "invalid_runs": sorted(invalid),
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
