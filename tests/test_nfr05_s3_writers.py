"""Every direct S3 write must send SSE explicitly, even with bucket default encryption."""

import ast
import json
import re
import shlex
from itertools import takewhile
from pathlib import Path

from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parents[1]
AWS_CP = ("aws", "s3", "cp")
SHELL_COPY = re.compile(r"\baws\s+s3\s+cp\b")
# Options used with downloads can precede the source and destination.
OPTIONS_WITH_VALUES = {"--region", "--profile", "--endpoint-url", "--include", "--exclude"}


def _copy_needs_sse(args):
    if "--sse" in args or any(arg.startswith("--sse=") and arg != "--sse=" for arg in args):
        return False

    operands = []
    words = iter(args)
    for word in words:
        if word in OPTIONS_WITH_VALUES:
            next(words, None)
        elif not word.startswith("-") or word == "-":
            operands.append(word)
    # Exempt only copies from S3 to a local destination (including stdout).
    return not (
        len(operands) == 2
        and operands[0].startswith("s3://")
        and not operands[1].startswith("s3://")
    )


def _shell_violations(script):
    continued = ""
    for number, physical_line in enumerate(script.splitlines(), 1):
        if physical_line.endswith("\\"):
            continued += physical_line[:-1] + " "
            continue
        line = continued + physical_line
        continued = ""
        if not SHELL_COPY.search(line):
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
        for offset in range(len(tokens) - 2):
            if tuple(tokens[offset : offset + 3]) == AWS_CP:
                args = list(takewhile(lambda token: token.strip(";&|<>"), tokens[offset + 3 :]))
                if _copy_needs_sse(args):
                    yield number, "aws s3 cp lacks --sse"


def _arg(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_arg(part) for part in node.values)
    return "$value"


def _python_violations(source):
    nodes = list(ast.walk(ast.parse(source)))
    fstring_parts = {
        part for node in nodes if isinstance(node, ast.JoinedStr) for part in node.values
    }
    for node in nodes:
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "put_object":
                if not any(
                    kw.arg == "ServerSideEncryption"
                    and (not isinstance(kw.value, ast.Constant) or kw.value.value)
                    for kw in node.keywords
                ):
                    yield node.lineno, "put_object lacks ServerSideEncryption"
        elif isinstance(node, (ast.List, ast.Tuple)):
            args = [_arg(item) for item in node.elts]
            if tuple(args[:3]) == AWS_CP and _copy_needs_sse(args[3:]):
                yield node.lineno, "aws s3 cp lacks --sse"
        elif isinstance(node, (ast.Constant, ast.JoinedStr)) and node not in fstring_parts:
            for line, reason in _shell_violations(_arg(node)):
                yield node.lineno + line - 1, reason


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for part in value.values():
            yield from _strings(part)
    elif isinstance(value, list):
        for part in value:
            yield from _strings(part)


def _violations(path):
    if path.suffix == ".py":
        yield from _python_violations(path.read_text())
    elif path.suffix == ".sh":
        yield from _shell_violations(path.read_text())
    elif path.suffix in {".json", ".yaml", ".yml"}:
        if path.suffix == ".json":
            config = json.loads(path.read_text())
        else:
            config, errors = decode(str(path))
            assert not errors, errors
        for script in _strings(config):
            for _, reason in _shell_violations(script):
                yield "embedded command", reason


def test_s3_writers_send_explicit_server_side_encryption():
    failures = [
        f"{path.relative_to(ROOT)}:{line}: {reason}"
        for directory in ("gate", "deploy", "orchestrator")
        for path in sorted((ROOT / directory).rglob("*"))
        if path.is_file()
        for line, reason in _violations(path)
    ]
    assert not failures, "\n".join(failures)
