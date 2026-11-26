"""FR-05 data (PRD Section 6.2.1): D2 corpus is the first 2,048 reference tokens of WikiText-2."""

import json
import urllib.request
from pathlib import Path

import pytest
from baseline_serving import serving_config

from analysis import detector_data as dd
from gate.manifest import load_yaml

CORPUS = Path("gate/data/d2_corpus.txt")


class FakeTokenizer:
    """One token per character, enough to test the cut logic."""

    def tokenize(self, text):
        return [ord(c) for c in text]

    def detokenize(self, tokens):
        return "".join(chr(t) for t in tokens)


def test_rows_join_as_raw_lines_with_blank_rows_as_newlines():
    assert dd.wikitext_text(["", " = A = \n", "", " text .\n"]) == "\n = A = \n\n text .\n"


def test_first_n_tokens_cuts_by_tokens():
    assert dd.first_n_tokens(FakeTokenizer(), "abcdef", 4) == "abcd"
    with pytest.raises(ValueError):
        dd.first_n_tokens(FakeTokenizer(), "abc", 4)


def test_committed_corpus_is_wikitext2_test_start():
    text = CORPUS.read_text()
    assert text.startswith("\n = Robert Boulter = \n")
    assert 4000 < len(text) < 20000  # about 4 characters per token for 2,048 tokens


def _reference_server():
    cfg = serving_config()
    base = f"http://{cfg['server']['host']}:{cfg['server']['port']}"
    try:
        with urllib.request.urlopen(base + "/props", timeout=2) as r:
            model = Path(json.loads(r.read())["model_path"]).name
    except OSError:
        return None
    return dd.LlamaServer(base) if model == cfg["model"] else None


def test_corpus_is_exactly_2048_reference_tokens():
    server = _reference_server()
    if server is None:
        pytest.skip("needs llama-server on the reference model at the configured port")
    n = load_yaml(dd.SOURCES)["d2"]["tokens"]
    assert len(server.tokenize(CORPUS.read_text())) == n == 2048
