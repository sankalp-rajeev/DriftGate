"""Package the CodeDeploy revision bundle for one commit (FR-16, T-11; used by deploy/build.sh).

The bundle is the repository tree at <commit> (`git archive`) with `deploy/appspec.yml` copied
to the bundle root, written as <out>/bundles/<commit>.zip: the same relative key the
orchestrator expects in S3 (`bundles/{commit_id}.zip`). Uploading it is T-28's job.

usage: python deploy/package_bundle.py --commit <commit> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path

APPSPEC = "deploy/appspec.yml"


def package(commit: str, out: Path) -> Path:
    full = subprocess.run(
        ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dest = out / "bundles" / f"{commit}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "archive", "--format=zip", "-o", str(dest), full], check=True)
    appspec = subprocess.run(
        ["git", "show", f"{full}:{APPSPEC}"], capture_output=True, check=True
    ).stdout
    with zipfile.ZipFile(dest, "a", compression=zipfile.ZIP_DEFLATED) as z:
        if "appspec.yml" in z.namelist():
            raise SystemExit("repository root already has an appspec.yml")
        z.writestr("appspec.yml", appspec)
    return dest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--commit", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    dest = package(args.commit, args.out)
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
    print(json.dumps({"bundle": str(dest), "files": len(names), "appspec": "appspec.yml" in names}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
