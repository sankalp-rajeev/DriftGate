"""FR-06 data (PRD Section 6.2.1): D3 prompt set of 256 Dolly instructions."""

import json
from pathlib import Path

from analysis import detector_data as dd

PROMPTS = Path("gate/data/d3_prompts.jsonl")


def _records(n):
    out = []
    for i in range(n):
        ctx = "" if i % 3 else "some context"
        instr = "x" * (10 + (i * 7) % 320)
        out.append({"instruction": instr, "context": ctx, "category": "c", "_line": i})
    return out


def test_selection_filters_empty_context_and_length():
    rows = dd.select_d3(_records(400), 50, seed=0, min_len=20, max_len=300)
    recs = {r["_line"]: r for r in _records(400)}
    for row in rows:
        src = recs[row["id"]]
        assert src["context"] == ""
        assert 20 <= len(src["instruction"]) <= 300


def test_selection_is_deterministic_given_seed():
    a = dd.select_d3(_records(400), 50, seed=0, min_len=20, max_len=300)
    b = dd.select_d3(_records(400), 50, seed=0, min_len=20, max_len=300)
    c = dd.select_d3(_records(400), 50, seed=1, min_len=20, max_len=300)
    assert a == b
    assert a != c


def test_committed_prompt_set():
    rows = [json.loads(line) for line in PROMPTS.read_text().splitlines()]
    assert len(rows) == 256
    assert len({r["id"] for r in rows}) == 256
    assert all(20 <= len(r["instruction"]) <= 300 for r in rows)
    # Budgets 16, 64 and 256 are the first N records in file order.
    for n in (16, 64, 256):
        assert len(rows[:n]) == n
