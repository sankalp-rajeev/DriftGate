# Attribution

Third-party code, tools, models and datasets used by DriftGate. Licenses come from `PRD.md` Section 6.2.1 where stated; others are `TBD (verify)`.

| Name | Use | License | Link |
| --- | --- | --- | --- |
| llama.cpp | Serving engine (llama-server), `llama-perplexity` for D2 | TBD (verify) | https://github.com/ggml-org/llama.cpp |
| ggufdoctor | D1 check (a): lint of the GGUF chat template (dev dependency, run through `uv run`) | TBD (verify) | https://pypi.org/project/ggufdoctor/ |
| Jinja2 | D1 check (b): renders the served and pinned upstream chat templates for the rendered-template diff | TBD (verify) | https://pypi.org/project/Jinja2/ |
| Model Equality Testing | D4 MMD two-sample test | TBD (verify) | https://arxiv.org/abs/2410.20247 |
| infer-check | Prior art; upstream contribution target | TBD (verify) | https://pypi.org/project/infer-check/ |
| Qwen2.5-1.5B-Instruct-GGUF | Reference model (Q8_0), faults F1 (Q4_K_M) and F2 (Q2_K) | Apache-2.0 | https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF |
| Qwen2.5-0.5B-Instruct-GGUF | CI and fast-test model (Q8_0) | Apache-2.0 | https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF |
| WikiText-2 | D2 corpus (raw test split) | CC BY-SA | https://huggingface.co/datasets/Salesforce/wikitext |
| databricks-dolly-15k | D3 prompt set | CC BY-SA 3.0 | https://huggingface.co/datasets/databricks/databricks-dolly-15k |
| MMLU (`cais/mmlu`) | D5 panel and hidden pool | MIT | https://huggingface.co/datasets/cais/mmlu |
| cfn-lint | CloudFormation template validation (dev tool) | MIT-0 | https://github.com/aws-cloudformation/cfn-lint |
| Prometheus (`prom/prometheus:v3.16.0`) | Local scrape of llama-server `/metrics` (FR-22) | Apache-2.0 | https://github.com/prometheus/prometheus |
| Grafana (`grafana/grafana:13.2.6`) | Local dashboard (FR-23) | AGPL-3.0 (unmodified image, run locally) | https://github.com/grafana/grafana |
| OpenTelemetry Collector Contrib (`otel/opentelemetry-collector-contrib:0.164.0`) | Local trace and metric collector (FR-24) | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-collector-contrib |
| Jaeger (`jaegertracing/jaeger:2.22.0`, all-in-one) | Local trace viewer (FR-24, PRD 6.8) | Apache-2.0 | https://github.com/jaegertracing/jaeger |
| OpenTelemetry Python SDK, OTLP/HTTP exporter, semantic conventions (1.45.2, semconv 0.66b2 = schema 1.44.0) | Gate spans (FR-24) | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python |
