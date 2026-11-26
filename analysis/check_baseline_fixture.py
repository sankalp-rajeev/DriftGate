"""Check the test fixture against the approved model SHA and effective config hash."""

import argparse
import json
import sys
from pathlib import Path

from gate import reference
from gate.manifest import load_yaml


def fixture_config(directory: Path) -> dict:
    config = load_yaml(directory / "config.yaml")
    for key in ("chat_template", "llama_cpp_lock", "models"):
        config[key] = str(directory / Path(config[key]).relative_to("serving"))
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-dir", type=Path, default=Path("tests/fixtures/baseline-serving"))
    parser.add_argument("--baseline", type=Path, default=reference.DEFAULT_BASELINE)
    args = parser.parse_args(argv)
    try:
        config = fixture_config(args.fixture_dir)
        actual = {
            "model_sha256": reference.model_sha256(config, args.fixture_dir / "manifest.json"),
            "config_hash": reference.config_hash(config),
        }
        baseline = reference.load_baseline(args.baseline)
        expected = {key: baseline[key] for key in actual}
        result = {"ok": actual == expected, "actual": actual, "expected": expected}
    except (OSError, ValueError, KeyError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps({"schema": "driftgate.baseline-fixture/1", **result}, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
