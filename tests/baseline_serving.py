"""Pinned serving data for tests, independent of the candidate checkout's serving/ files."""

import shutil
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from analysis.check_baseline_fixture import fixture_config

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/baseline-serving"
CONFIG = BASELINE / "config.yaml"
MANIFEST = BASELINE / "manifest.json"
MODELS = BASELINE / "models.yaml"


def fixture_path(path: str) -> Path:
    return BASELINE / Path(path).relative_to("serving")


def serving_config() -> dict:
    return fixture_config(BASELINE)


def write_config(path: Path) -> Path:
    path.write_text(yaml.safe_dump(serving_config(), sort_keys=False))
    return path


def copy_serving(destination: Path) -> None:
    shutil.copytree(BASELINE, destination / "serving", dirs_exist_ok=True)


def workspace(destination: Path) -> Path:
    """Give relative-path bundle/CLI tests their own serving tree and project tools."""
    destination.mkdir(parents=True, exist_ok=True)
    copy_serving(destination)
    for name in (
        "gate",
        "faults",
        "analysis",
        "orchestrator",
        "observability",
        "models",
        ".venv",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "tests",
    ):
        (destination / name).symlink_to(ROOT / name, target_is_directory=(ROOT / name).is_dir())
    return destination


def run_gate(args, **kwargs):
    """Exercise the real CLI with baseline serving defaults in an isolated workspace."""
    from gate import cli

    with TemporaryDirectory() as directory:
        with chdir(workspace(Path(directory))):
            return cli.main(args, **kwargs)
