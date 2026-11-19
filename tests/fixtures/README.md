`d5_panel_dev177.jsonl` is a **development fixture, not frozen data**. It is the 177-item
"sometimes right" panel from the earlier 2,000-question seed-0 MMLU sample (PRD v1.3, Section
6.2.1). The gate reads it through `gate/config.yaml` until T-30 commits the real
`gate/data/d5_panel.jsonl` from the full 14,042-question scoring run.
