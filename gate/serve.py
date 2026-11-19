"""Launch llama-server from the serving config (FR-01, FR-03).

`build_argv` turns `serving/config.yaml` into a llama-server command line, so every serving fault
is a diff to that file (or to a file it names). Before starting, the served model is checked
against the SHA-256 manifest (FR-02) and the binary against `serving/llama_cpp.lock`.

CLI: `python -m gate.serve [--config serving/config.yaml] [--dry-run]`
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from gate.manifest import DEFAULT_CONFIG, DEFAULT_MANIFEST, load_yaml, verify

DEFAULT_BIN = "~/llama.cpp/build/bin"


def llama_bin_dir() -> Path:
    return Path(os.environ.get("LLAMA_CPP_BIN", DEFAULT_BIN)).expanduser()


def build_argv(config: dict, models_dir: Path, bin_dir: Path) -> list[str]:
    server, cache, sampler = config["server"], config["cache"], config["sampler"]
    argv = [
        str(bin_dir / "llama-server"),
        "--model", str(models_dir / config["model"]),
        "--host", str(server["host"]),
        "--port", str(server["port"]),
        "--parallel", str(server["parallel"]),
        "--ctx-size", str(config["context"]["ctx_size"]),
        "--threads", str(config["threads"]),
        "--jinja",
        "--chat-template-file", str(config["chat_template"]),
        "--cache-type-k", str(cache["type_k"]),
        "--cache-type-v", str(cache["type_v"]),
        "--flash-attn", str(cache["flash_attn"]),
        "--temp", str(sampler["temperature"]),
        "--top-k", str(sampler["top_k"]),
        "--top-p", str(sampler["top_p"]),
        "--min-p", str(sampler["min_p"]),
        "--repeat-penalty", str(sampler["repeat_penalty"]),
        "--seed", str(sampler["seed"]),
    ]  # fmt: skip
    if server.get("metrics"):
        argv.append("--metrics")
    return argv


def binary_commit(bin_dir: Path) -> str | None:
    """Short commit hash reported by `llama-server --version`, or None if not found."""
    out = subprocess.run(
        [str(bin_dir / "llama-server"), "--version"], capture_output=True, text=True, check=False
    )
    m = re.search(r"commit ([0-9a-f]+)", out.stdout + out.stderr)
    return m.group(1) if m else None


def engine_matches(lock: dict, commit: str | None) -> bool:
    return commit is not None and len(commit) >= 7 and lock["commit"].startswith(commit)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gate.serve", description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--models-dir", type=Path, help="override models_dir from models.yaml")
    p.add_argument("--dry-run", action="store_true", help="print the command as JSON and exit")
    args = p.parse_args(argv)

    config = load_yaml(args.config)
    models_dir = args.models_dir or Path(load_yaml(Path(config["models"]))["models_dir"])
    bin_dir = llama_bin_dir()
    cmd = build_argv(config, models_dir, bin_dir)
    if args.dry_run:
        json.dump({"argv": cmd}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    check = verify([config["model"]], args.manifest, models_dir)
    if not check["ok"]:
        json.dump(check, sys.stderr, indent=2)
        sys.stderr.write("\nrefusing to serve: model does not match the manifest\n")
        return 1
    lock = load_yaml(Path(config["llama_cpp_lock"]))
    commit = binary_commit(bin_dir)
    if not engine_matches(lock, commit):
        sys.stderr.write(
            f"refusing to serve: llama-server commit {commit} does not match "
            f"{config['llama_cpp_lock']} ({lock['commit']})\n"
        )
        return 1
    os.execv(cmd[0], cmd)
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
