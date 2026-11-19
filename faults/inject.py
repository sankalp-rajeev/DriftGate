"""Fault injector (FR-10): A/A and F1 to F4 as reproducible config or artifact diffs.

`python -m faults.inject F4 [--out artifacts/bundles]` copies the reference serving bundle
(`serving/config.yaml`, the files it names, the model manifest), applies the fault's diff, and
writes a deployable bundle directory plus `diff.log` (unified diff against the reference) and
`bundle.json` (fault id, description, SHA-256 of every bundle file). Faults are defined in
`faults/faults.yaml`; the same fault always yields byte-identical bundle files.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
import sys
from pathlib import Path

import yaml

from gate.manifest import load_yaml

FAULTS = Path("faults/faults.yaml")
REFERENCE_CONFIG = Path("serving/config.yaml")
BUNDLE_SCHEMA = "driftgate.bundle/1"


def bundle_files(config: dict, config_path: Path) -> dict[str, Path]:
    """Files a deploy needs besides the weights, as {path in bundle: source path}.

    The config always lands at serving/config.yaml; the files it names keep their paths.
    """
    files = {str(REFERENCE_CONFIG): config_path}
    for rel in (config["chat_template"], config["llama_cpp_lock"], config["models"]):
        files[rel] = Path(rel)
    files["serving/manifest.json"] = Path("serving/manifest.json")
    return dict(sorted(files.items()))


def set_yaml_value(text: str, dotted: str, value) -> str:
    """Change one scalar in a block-style YAML file, keeping comments and layout.

    Supports top-level keys and keys one level down (`section.key`), which is all the serving
    config uses. The result is re-parsed to check that exactly that value changed.
    """
    *section, key = dotted.split(".")
    if len(section) > 1:
        raise ValueError(f"only one level of nesting is supported: {dotted}")
    lines = text.splitlines(keepends=True)
    in_section = not section
    rendered = yaml.safe_dump(value, default_flow_style=True).strip().removesuffix("...").strip()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if section and indent == 0 and stripped.startswith(section[0] + ":"):
            in_section = True
            continue
        if section and indent == 0 and stripped and not stripped.startswith("#"):
            in_section = False
        want_indent = 2 if section else 0
        if in_section and indent == want_indent and stripped.startswith(key + ":"):
            head, sep, rest = line.partition(":")
            comment = ""
            if "#" in rest:
                comment = "  #" + rest.split("#", 1)[1].rstrip("\n")
            lines[i] = f"{head}{sep} {rendered}{comment}\n"
            new = "".join(lines)
            expected = yaml.safe_load(text)
            node = expected
            for k in section:
                node = node[k]
            node[key] = value
            if yaml.safe_load(new) != expected:
                raise ValueError(f"editing {dotted} changed more than that value")
            return new
    raise KeyError(f"config has no key {dotted!r}")


def apply_fault(fault: dict, config_path: str, files: dict[str, str]) -> None:
    """Apply one fault in place: `set` edits config values, `template` edits the template text."""
    for dotted, value in (fault.get("set") or {}).items():
        files[config_path] = set_yaml_value(files[config_path], dotted, value)
    template = yaml.safe_load(files[config_path])["chat_template"]
    for edit in fault.get("template") or []:
        text = files[template]
        if text.count(edit["find"]) != 1:
            raise ValueError(f"template edit target must occur exactly once: {edit['find']!r}")
        files[template] = text.replace(edit["find"], edit["replace"])


def build(
    fault_id: str,
    out_root: Path,
    faults_path: Path = FAULTS,
    base_config: Path = REFERENCE_CONFIG,
) -> Path:
    faults = load_yaml(faults_path)["faults"]
    if fault_id not in faults:
        raise SystemExit(f"unknown fault {fault_id}; known: {', '.join(faults)}")
    fault = faults[fault_id]
    ref_config = load_yaml(base_config)
    ref_files = {rel: src.read_text() for rel, src in bundle_files(ref_config, base_config).items()}

    files = dict(ref_files)
    apply_fault(fault, str(REFERENCE_CONFIG), files)

    out = out_root / fault_id
    if out.exists():
        shutil.rmtree(out)
    for rel, text in files.items():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)

    diff = []
    for rel in sorted(files):
        diff += difflib.unified_diff(
            ref_files[rel].splitlines(keepends=True),
            files[rel].splitlines(keepends=True),
            fromfile=f"reference/{rel}",
            tofile=f"{fault_id}/{rel}",
        )
    (out / "diff.log").write_text("".join(diff))
    meta = {
        "schema": BUNDLE_SCHEMA,
        "fault": fault_id,
        "layer": fault["layer"],
        "description": fault["description"],
        "files": {rel: hashlib.sha256(files[rel].encode()).hexdigest() for rel in sorted(files)},
        "diff_lines": sum(
            1 for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---")
        ),
    }
    (out / "bundle.json").write_text(json.dumps(meta, indent=2) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="faults.inject", description=__doc__.splitlines()[0])
    p.add_argument("fault", help="fault id from faults/faults.yaml, e.g. AA or F4")
    p.add_argument("--out", type=Path, default=Path("artifacts/bundles"))
    p.add_argument(
        "--base-config",
        type=Path,
        default=REFERENCE_CONFIG,
        help="serving config the fault is applied to (default: the reference config)",
    )
    args = p.parse_args(argv)
    out = build(args.fault, args.out, base_config=args.base_config)
    sys.stdout.write((out / "diff.log").read_text())
    print(json.dumps({"bundle": str(out), **json.loads((out / "bundle.json").read_text())}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
