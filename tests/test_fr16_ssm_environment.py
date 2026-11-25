"""SSM starts with the root home and the tools installed by bootstrap on PATH."""

import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("stage", ["build", "reference"])
def test_ssm_commands_start_with_bootstrap_environment(stage):
    config = json.loads(Path("orchestrator/config.json").read_text())
    commands = config[stage]["commands"]
    assert commands[0] == "export HOME=/root PATH=/usr/local/bin:$PATH"
    result = subprocess.run(
        ["bash", "-c", commands[0] + '\n[[ "$HOME" = /root && "$PATH" = /usr/local/bin:* ]]'],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
