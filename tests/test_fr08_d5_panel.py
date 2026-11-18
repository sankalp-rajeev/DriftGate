"""FR-08 and FR-14 data (PRD Section 6.2.1): D5 public panel and hidden pool from MMLU."""

import json
import math
import random
from pathlib import Path

from analysis import detector_data as dd
from gate.manifest import load_yaml
from gate.prompts import mmlu_prompt

DATA = Path("gate/data")
SRC = load_yaml(dd.SOURCES)["d5"]


def _read(name):
    return [json.loads(line) for line in (DATA / name).read_text().splitlines()]


def test_prompt_is_zero_shot_and_ends_with_the_instruction():
    item = {"question": "2+2?", "choices": ["1", "2", "3", "4"], "answer": "D"}
    prompt = mmlu_prompt(item)
    assert prompt.endswith("Answer with A, B, C or D.")
    assert "A. 1\nB. 2\nC. 3\nD. 4" in prompt


def test_gold_letter_probability_from_top10_absent_is_zero():
    top = [
        {"token": "B", "logprob": math.log(0.5)},
        {"token": " B", "logprob": math.log(0.1)},
        {"token": "A", "logprob": math.log(0.3)},
    ]
    assert math.isclose(dd.gold_letter_prob(top, "B"), 0.6)
    assert dd.gold_letter_prob(top, "D") == 0.0


def test_selection_keeps_sometimes_right_items_after_seeded_shuffle():
    scores = [{"id": str(i), "gold_prob": i / 100} for i in range(101)]
    items = {str(i): {"id": str(i)} for i in range(101)}
    panel, pool = dd.select_d5(items, scores, 0.25, 0.75, seed=0, n_panel=10, n_pool=20)
    eligible = [s["id"] for s in scores if 0.25 <= s["gold_prob"] <= 0.75]
    random.Random(0).shuffle(eligible)
    assert [r["id"] for r in panel] == eligible[:10]
    assert [r["id"] for r in pool] == eligible[10:30]


def test_reference_scores_cover_the_seeded_sample():
    scores = _read("d5_reference_scores.jsonl")
    assert len(scores) == SRC["sample"] == 2000
    assert len({s["id"] for s in scores}) == 2000
    assert all(0.0 <= s["gold_prob"] <= 1.0 + 1e-9 for s in scores)


def test_committed_panel_and_pool_follow_the_selection_rule():
    scores = _read("d5_reference_scores.jsonl")
    panel, pool = _read("d5_panel.jsonl"), _read("d5_hidden_pool.jsonl")
    eligible = [s["id"] for s in scores if SRC["lo"] <= s["gold_prob"] <= SRC["hi"]]
    random.Random(0).shuffle(eligible)
    assert len(panel) == SRC["panel"] == 256
    assert len(pool) == min(SRC["hidden_pool"], len(eligible) - 256)
    assert [r["id"] for r in panel] == eligible[:256]
    assert [r["id"] for r in pool] == eligible[256 : 256 + len(pool)]
    for r in panel + pool:
        assert r["answer"] in "ABCD" and len(r["choices"]) == 4
        assert 0.25 <= r["reference_gold_prob"] <= 0.75


def test_data_manifest_matches_committed_files():
    recorded = json.loads((DATA / "manifest.json").read_text())
    assert recorded == dd.data_manifest(DATA)
