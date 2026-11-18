Detector data (PRD Section 6.2.1), built by `python -m analysis.detector_data` from the sources
pinned in `sources.yaml` and hashed in `manifest.json`. Frozen by the pre-registration (T-08).
Selection used the reference model only (`qwen2.5-1.5b-instruct-q8_0.gguf`, `serving/config.yaml`).

| File | Detector | Content | Source and licence |
| --- | --- | --- | --- |
| `d2_corpus.txt` | D2 | First 2,048 reference-tokenizer tokens of the WikiText-2 raw test split | Salesforce/wikitext, CC BY-SA |
| `d3_prompts.jsonl` | D3 | 256 instructions with empty context, 20 to 300 characters, `random.Random(0).sample` over eligible records in file order; `id` is the 0-based line in the source file. Budgets are the first N lines | databricks/databricks-dolly-15k, CC BY-SA 3.0 |
| `d5_reference_scores.jsonl` | D5 | The 2,000-question seed-0 sample of the MMLU test split (subjects in file-name order, rows in file order), each with the reference model's probability of the gold letter among the first generated token's top 10 (absent = 0; a token counts when its text without whitespace is the letter) | cais/mmlu, MIT |
| `d5_panel.jsonl` | D5 | First 256 items with gold-letter probability in [0.25, 0.75] after a seed-0 shuffle | cais/mmlu, MIT |
| `d5_hidden_pool.jsonl` | D5, FR-14 | The next up to 512 such items | cais/mmlu, MIT |

Rebuild (needs `make serve` on the reference model for `d2` and `d5-score`):

```
uv run python -m analysis.detector_data d3
uv run python -m analysis.detector_data d2
uv run python -m analysis.detector_data d5-score   # about 2,000 requests; run in tmux
uv run python -m analysis.detector_data d5-select
uv run python -m analysis.detector_data hashes
```
