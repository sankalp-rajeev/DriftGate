"""Download pinned model artifacts and record their SHA-256 (PRD Section 6.2.1, FR-02).

Each file in `serving/models.yaml` is fetched from
`https://huggingface.co/<repo>/resolve/<commit>/<file>` into `models_dir`. On first download its
SHA-256 and size are added to `serving/manifest.json`; when an entry already exists the file must
match it, otherwise the command fails and the file is left in place for inspection.

CLI: `python -m gate.models [--role reference --role ci ...]`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from gate.manifest import (
    DEFAULT_MANIFEST,
    DEFAULT_MODELS,
    load_manifest,
    load_yaml,
    save_manifest,
    sha256_file,
)


def artifact_url(models_cfg: dict, entry: dict) -> str:
    return models_cfg["url_template"].format(
        repo=entry["repo"], commit=entry["commit"], file=entry["file"]
    )


def _fetch(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    os.replace(tmp, dest)


def ensure(models_cfg: dict, manifest: dict, entry: dict, fetch=_fetch) -> dict:
    """Download one artifact if missing and check or record its hash."""
    models_dir = Path(models_cfg["models_dir"])
    models_dir.mkdir(parents=True, exist_ok=True)
    name = entry["file"]
    path = models_dir / name
    url = artifact_url(models_cfg, entry)
    downloaded = False
    if not path.exists():
        fetch(url, path)
        downloaded = True
    digest = sha256_file(path)
    recorded = manifest["artifacts"].get(name)
    if recorded is None:
        manifest["artifacts"][name] = {
            "sha256": digest,
            "size": path.stat().st_size,
            "source": url,
        }
        status = "recorded"
    elif recorded["sha256"] == digest:
        status = "verified"
    else:
        status = "mismatch"
    return {
        "artifact": name,
        "role": entry["role"],
        "downloaded": downloaded,
        "sha256": digest,
        "status": status,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gate.models", description=__doc__.splitlines()[0])
    p.add_argument("--models", type=Path, default=DEFAULT_MODELS)
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--role", action="append", help="only these roles (default: all)")
    args = p.parse_args(argv)

    models_cfg = load_yaml(args.models)
    manifest = load_manifest(args.manifest)
    results = []
    for entry in models_cfg["models"]:
        if args.role and entry["role"] not in args.role:
            continue
        results.append(ensure(models_cfg, manifest, entry))
        save_manifest(manifest, args.manifest)
    ok = all(r["status"] != "mismatch" for r in results)
    json.dump({"ok": ok, "artifacts": results}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
