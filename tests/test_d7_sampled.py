"""The sampled D7 study uses each bundle's defaults and a fixed reference."""

import math
from unittest.mock import Mock

import pytest

from analysis import d7_sampled


def test_sampling_request_inherits_serving_defaults():
    server = Mock()
    server.apply_template.return_value = "rendered"
    server.completion.return_value = {
        "content": "sample",
        "generation_settings": {"temperature": 1.3, "top_k": 40, "top_p": 0.95, "min_p": 0.05},
    }
    sampler = {"temperature": 1.3, "top_k": 40, "top_p": 0.95, "min_p": 0.05}
    assert d7_sampled.sample(server, [{"id": "p", "instruction": "question"}], 17, sampler) == [
        {"id": "p", "text": "sample"}
    ]
    server.completion.assert_called_once_with(
        "rendered", n_predict=128, seed=17, cache_prompt=False
    )


def test_centroid_and_exact_match_share_use_the_same_candidate_text():
    samples = [
        [{"id": "p", "text": str(i), "embedding": [1, 0] if i < 4 else [0, 1]}] for i in range(5)
    ]
    ref = d7_sampled.make_reference(samples)
    candidate = [{"id": "p", "text": "3", "embedding": [1, 0]}]
    result = d7_sampled.statistics(ref, candidate)
    assert ref[0]["embedding"] == [0.8, 0.2]
    assert result["exact_match_agreement"] == 1
    assert result["d7"] == pytest.approx(1 - 4 / math.sqrt(17))


def test_fit_uses_fit_maximum_and_tests_held_out_strictly_above_cutoff():
    low = {"d7": 0.2, "exact_match": 0.4}
    high = {"d7": 0.3, "exact_match": 0.5}
    held = {"d7": 0.4, "exact_match": 0.5}
    result = d7_sampled.calibration([low] * 19 + [high], [held] + [low] * 9)
    assert result["thresholds"] == high
    assert result["held_out_false_alarms"]["d7"]["k"] == 1
    assert result["held_out_false_alarms"]["exact_match"]["k"] == 0


def test_fault_generation_waits_for_a_committed_registration(tmp_path, monkeypatch):
    stages = []

    def generate(out, name, stage, seeds, source, prompts, config):
        assert stage != "faults"
        stages.append(stage)
        for seed in seeds:
            yield seed, [{"id": "p", "text": "a", "embedding": [1, 0]}]

    def failed_commit(*args):
        assert stages == ["reference", "aa-fit", "aa-held-out"]
        assert (tmp_path / "calibration.json").is_file()
        raise RuntimeError("pre-registration commit failed")

    monkeypatch.setattr(d7_sampled, "check_branch", lambda: "study-commit")
    monkeypatch.setattr(d7_sampled, "generate", generate)
    monkeypatch.setattr(d7_sampled, "commit_document", failed_commit)
    monkeypatch.setattr(d7_sampled, "PREREG", tmp_path / "prereg.md")
    monkeypatch.setattr(d7_sampled, "REPORT", tmp_path / "report.md")
    with pytest.raises(RuntimeError, match="pre-registration commit failed"):
        d7_sampled.run(tmp_path, {"AA": None, "F1": None}, {})
