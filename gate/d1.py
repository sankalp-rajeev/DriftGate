"""D1: chat-template lint against upstream (FR-04, PRD Section 6.2.1). Runs on the build host.

Two checks; either failing makes the command exit 1:

  (a) ggufdoctor on the served GGUF (default `--fail-on error`), with its JSON report kept;
  (b) rendered-template diff: three fixed conversations (a plain chat, one with a system turn,
      one with a tool call) are rendered with jinja2 through the served template file and
      through the pinned upstream copy `serving/templates/reference.jinja`; any difference fails.

The jinja2 environment mirrors the transformers `apply_chat_template` path: sandboxed,
`trim_blocks` and `lstrip_blocks`, the loop-controls extension, `raise_exception`, and a
`tojson` that does not escape non-ASCII. Output is JSON (`driftgate.d1/1`).

CLI: `python -m gate.d1 [--config serving/config.yaml] [--template PATH] [--out d1.json]`
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path

from jinja2.exceptions import TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from gate.manifest import DEFAULT_CONFIG, load_yaml

SCHEMA = "driftgate.d1/1"
REFERENCE_TEMPLATE = Path("serving/templates/reference.jinja")

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

CONVERSATIONS = {
    "plain_chat": {
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
    },
    "system_turn": {
        "messages": [
            {"role": "system", "content": "You answer in one short sentence."},
            {"role": "user", "content": "What is the capital of France?"},
        ],
    },
    "tool_call": {
        "tools": [WEATHER_TOOL],
        "messages": [
            {"role": "user", "content": "What is the weather in Paris?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": {"city": "Paris"}},
                    }
                ],
            },
            {"role": "tool", "content": '{"temperature_c": 18}'},
        ],
    },
}


def _raise(message):
    raise TemplateError(message)


def _tojson(x, ensure_ascii=False, indent=None, separators=None, sort_keys=False):
    return json.dumps(
        x, ensure_ascii=ensure_ascii, indent=indent, separators=separators, sort_keys=sort_keys
    )


def render(template_text: str, conversation: dict) -> str:
    env = ImmutableSandboxedEnvironment(
        trim_blocks=True, lstrip_blocks=True, extensions=["jinja2.ext.loopcontrols"]
    )
    env.filters["tojson"] = _tojson
    env.globals["raise_exception"] = _raise
    return env.from_string(template_text).render(
        messages=conversation["messages"],
        tools=conversation.get("tools"),
        add_generation_prompt=True,
        bos_token="",
        eos_token="<|im_end|>",
    )


def template_diff(served: Path, reference: Path = REFERENCE_TEMPLATE) -> dict:
    served_text, ref_text = served.read_text(), reference.read_text()
    cases = {}
    for name, conv in CONVERSATIONS.items():
        try:
            got, want = render(served_text, conv), render(ref_text, conv)
        except TemplateError as e:
            cases[name] = {"ok": False, "error": f"{type(e).__name__}: {e}", "diff": None}
            continue
        diff = "".join(
            difflib.unified_diff(
                want.splitlines(keepends=True),
                got.splitlines(keepends=True),
                fromfile=f"reference/{name}",
                tofile=f"served/{name}",
            )
        )
        cases[name] = {"ok": got == want, "error": None, "diff": diff or None}
    return {
        "served": str(served),
        "reference": str(reference),
        "ok": all(c["ok"] for c in cases.values()),
        "cases": cases,
    }


def ggufdoctor(gguf: Path, report: Path) -> dict:
    exe = Path(sys.executable).parent / "ggufdoctor"
    proc = subprocess.run(
        [str(exe), str(gguf), "--json", str(report)], capture_output=True, text=True
    )
    summary = None
    if report.exists():
        summary = json.loads(report.read_text()).get("summary")
    return {
        "gguf": str(gguf),
        "exit_code": proc.returncode,
        "ok": proc.returncode == 0,
        "summary": summary,
        "report": str(report),
        "stdout_tail": proc.stdout[-2000:],
    }


def run(config_path: Path, template: Path | None, out_dir: Path) -> dict:
    config = load_yaml(config_path)
    models_dir = Path(load_yaml(Path(config["models"]))["models_dir"])
    served = template or Path(config["chat_template"])
    out_dir.mkdir(parents=True, exist_ok=True)
    checks = {
        "ggufdoctor": ggufdoctor(models_dir / config["model"], out_dir / "ggufdoctor.json"),
        "template_diff": template_diff(served),
    }
    return {
        "schema": SCHEMA,
        "detector": "D1",
        "ok": all(c["ok"] for c in checks.values()),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gate.d1", description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--template", type=Path, help="served template (default: from --config)")
    p.add_argument("--out", type=Path, help="write the JSON result here (default: stdout)")
    p.add_argument("--report-dir", type=Path, help="where ggufdoctor's JSON report goes")
    args = p.parse_args(argv)
    out_dir = args.report_dir or (args.out.parent if args.out else Path("runs")) / "d1"
    result = run(args.config, args.template, out_dir)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
