# Attribution

Third-party code, tools, models and datasets used by DriftGate. Licenses come from `PRD.md` Section 6.2.1 where stated; others are `TBD (verify)`.

| Name | Use | License | Link |
| --- | --- | --- | --- |
| llama.cpp | Serving engine (llama-server), `llama-perplexity` for D2 | TBD (verify) | https://github.com/ggml-org/llama.cpp |
| ggufdoctor | D1 chat-template lint | TBD (verify) | https://pypi.org/project/ggufdoctor/ |
| Model Equality Testing | D4 MMD two-sample test | TBD (verify) | https://arxiv.org/abs/2410.20247 |
| infer-check | Prior art; upstream contribution target | TBD (verify) | https://pypi.org/project/infer-check/ |
| Qwen2.5-1.5B-Instruct-GGUF | Reference model (Q8_0), faults F1 (Q4_K_M) and F2 (Q2_K) | Apache-2.0 | https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF |
| Qwen2.5-0.5B-Instruct-GGUF | CI and fast-test model (Q8_0) | Apache-2.0 | https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF |
| WikiText-2 | D2 corpus (raw test split) | CC BY-SA | https://huggingface.co/datasets/Salesforce/wikitext |
| databricks-dolly-15k | D3 prompt set | CC BY-SA 3.0 | https://huggingface.co/datasets/databricks/databricks-dolly-15k |
| MMLU (`cais/mmlu`) | D5 panel and hidden pool | MIT | https://huggingface.co/datasets/cais/mmlu |
| cfn-lint | CloudFormation template validation (dev tool) | MIT-0 | https://github.com/aws-cloudformation/cfn-lint |
