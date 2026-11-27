"""Post-demo D7 semantic-drift acceptance tests."""

from unittest.mock import Mock

import pytest

from gate import d7
from gate.llama_client import RequestError


def test_identical_continuation_has_unit_similarity():
    embed = Mock(return_value=[0.4, 0.2])
    rows = [{"id": "p", "text": "A short answer."}]
    reference = d7.measure(rows, embed)
    candidate = d7.measure(rows, embed)

    result = d7.metrics(reference, candidate)

    assert result["mean_similarity"] == result["p5_similarity"] == 1
    assert result["semantic_drift"] == 0


def test_pairing_and_lower_tail_use_prompt_ids():
    reference = d7.measure([{"id": "a", "text": "x"}, {"id": "b", "text": "x"}], lambda _: [1, 0])
    candidate = d7.measure(
        [{"id": "b", "text": "y"}, {"id": "a", "text": "x"}],
        lambda text: {"x": [1, 0], "y": [0, 1]}[text],
    )

    result = d7.metrics(reference, candidate)

    assert result["mean_similarity"] == 0.5
    assert result["p5_similarity"] == pytest.approx(0.05)
    assert result["semantic_drift"] == pytest.approx(0.95)


def test_timeout_is_an_item_scoped_detector_error():
    embed = Mock(side_effect=RequestError(None, "request timed out", "timeout"))
    rows = [{"id": "p", "text": "A short answer."}]
    reference = d7.measure(rows, lambda _: [1, 0])

    result = d7.metrics(reference, d7.measure(rows, embed))

    assert result["status"] == "error"
    assert result["error"]["kind"] == "timeout"
    assert result["error"]["item_id"] == "p"


def test_empty_continuations_are_compared_without_an_embedding_request():
    embed = Mock(return_value=[1, 0])
    empty = d7.measure([{"id": "p", "text": " \n"}], embed)
    embed.assert_not_called()
    answer = d7.measure([{"id": "p", "text": "A short answer."}], embed)

    assert d7.metrics(empty, empty)["p5_similarity"] == 1
    assert d7.metrics(answer, empty)["p5_similarity"] == 0
    assert d7.metrics(empty, answer)["p5_similarity"] == 0


def test_weighted_d7_rule_contributes_to_the_existing_maximum():
    from gate.score import score

    thresholds = {
        "schema": "driftgate.thresholds/2",
        "rules": [],
        "D7": {"metric": "semantic_drift", "kind": "upper", "value": 0.1, "weight": 2.0},
    }
    result = score({"D7": {"semantic_drift": 0.06}}, thresholds)

    assert result["scores"]["D7"] == result["gate_score"] == pytest.approx(1.2)
    assert result["passed"] is False


def test_gate_reuses_the_text_from_each_d3_request(tmp_path, monkeypatch):
    import json
    from contextlib import nullcontext
    from pathlib import Path

    from fake_llama import FakeLlama

    from gate import cli
    from gate.manifest import load_yaml

    config = load_yaml(Path("gate/config.yaml"))
    reference = tmp_path / "reference.json"
    config["reference"] = str(reference)
    config["thresholds"] = str(tmp_path / "thresholds.json")
    Path(config["thresholds"]).write_text(
        json.dumps(
            {
                "schema": "driftgate.thresholds/2",
                "rules": [],
                "D7": {"metric": "semantic_drift", "kind": "upper", "weight": 1.0, "value": 0.0},
            }
        )
    )
    config_file = tmp_path / "gate.json"
    config_file.write_text(json.dumps(config))
    prompts = Path(config["data"]["d3_prompts"])
    panel = Path(config["data"]["d5_panel"])
    embed = Mock(return_value=[1, 0])
    monkeypatch.setattr(d7, "embedding_server", lambda _: nullcontext(embed))

    class AnswerServer(FakeLlama):
        def completion(self, request):
            completions.append(request["n_predict"])
            result = super().completion(request)
            if request["n_predict"] == 32:
                result["content"] = "Greedy continuation"
            return result

    completions = []
    with AnswerServer() as source:
        url = f"http://127.0.0.1:{source.port}"
        measurement = cli.measure(
            cli.LlamaServer(url), 2, 0, prompts, panel, embedding_config=config["d7"]
        )
        reference.write_text(json.dumps(measurement))
        completions.clear()
        embed.reset_mock()
        out = tmp_path / "verdict.json"
        code = cli.main(
            [
                "run",
                "--gate-config",
                str(config_file),
                "--server",
                url,
                "--budget",
                "2",
                "--seed",
                "0",
                "--out",
                str(out),
            ]  # fmt: skip
        )

    assert code == 0
    assert completions == [32, 32, 1, 1]
    assert [call.args for call in embed.call_args_list] == [("Greedy continuation",)] * 2
    verdict = json.loads(out.read_text())
    assert verdict["detectors"]["D7"]["p5_similarity"] == 1.0
    assert verdict["decision"]["scores"]["D7"] == 0.0


def test_lab_calibration_uses_the_max_of_the_first_twenty_only():
    from analysis.d7_runs import lab_calibration

    def measurement(vector):
        return {
            "d3": [{"id": "p"}],
            "d7": {
                "model_sha256": "model",
                "rows": [{"id": "p", "embedding": vector}],
                "seconds": 0.25,
            },
        }

    reference = measurement([1, 0])
    calibration = [reference] * 19 + [measurement([4, 3])]
    held_out = [reference] * 9 + [measurement([0, 1])]
    profile = {
        "schema": "driftgate.thresholds/2",
        "rules": [{"detector": "D5", "metric": "accuracy_drop", "kind": "upper", "value": 0.0}],
        "D7": {"metric": "semantic_drift", "kind": "upper", "weight": 1.0, "value": None},
    }

    report, installed = lab_calibration(reference, calibration + held_out, profile)

    assert report["D7"]["k"] == 1 and report["D7"]["n"] == 10
    assert report["embedding_seconds_max"] == 0.25
    assert installed["D7"]["value"] == pytest.approx(0.2)
    assert installed["rules"] == profile["rules"]
