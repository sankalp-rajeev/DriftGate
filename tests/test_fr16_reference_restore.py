"""Restore the baseline reference on build and serving hosts without redundant downloads."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("missing", [None, "measurement.json", "d2_base.kld", "both"])
def test_ensure_reference_restores_either_missing_file(tmp_path, missing):
    ref = tmp_path / "reference with spaces"
    ref.mkdir()
    for name in ("measurement.json", "d2_base.kld"):
        if missing not in (name, "both"):
            (ref / name).write_text("cached")
    commands = tmp_path / "commands"
    commands.mkdir()
    py = commands / "python"
    py.write_text(
        '#!/bin/bash\nif [[ "$*" = "-m gate reference --print-key" ]]; then\n'
        + "cat <<'JSON'\n"
        + json.dumps({"key": "reference/baseline/t3.large/", "local_dir": str(ref)})
        + f'\nJSON\nelse\nexec {sys.executable} "$@"\nfi\n'
    )
    aws = commands / "aws"
    aws.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" >> "$CALLS"\n'
        'dest="${@: -1}"\nprintf restored > "$dest/measurement.json"\n'
        'printf restored > "$dest/d2_base.kld"\n'
    )
    py.chmod(0o755)
    aws.chmod(0o755)
    log = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "-euc", f'source "{ROOT}/deploy/lib.sh"; ensure_reference; ensure_reference'],
        cwd=ROOT,
        env=os.environ
        | {
            "PATH": f"{commands}:{os.environ['PATH']}",
            "PY": str(py),
            "DRIFTGATE_BUNDLE_BUCKET": "lab-bucket",
            "DRIFTGATE_BUCKET": "",
            "CALLS": str(log),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    if missing is None:
        assert not log.exists()
    else:
        assert log.read_text().splitlines() == [
            "s3",
            "cp",
            "--only-show-errors",
            "--recursive",
            "s3://lab-bucket/reference/baseline/t3.large/",
            str(ref),
        ]
        assert (ref / "measurement.json").read_text() == "restored"
        assert (ref / "d2_base.kld").read_text() == "restored"
