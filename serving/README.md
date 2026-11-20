Serving config, chat templates and model manifest (FR-01 to FR-03).

| File | What it is |
| --- | --- |
| `config.yaml` | The serving config (FR-03): model file, chat template, context, KV cache, threads, sampler defaults, host and port. Every fault in PRD Section 6.3 is a diff to this file or to a file it names |
| `models.yaml` | Model artifacts from PRD Section 6.2.1, with the Hugging Face repo commit pinned |
| `manifest.json` | SHA-256 and size of each artifact, recorded at first download (FR-02) |
| `llama_cpp.lock` | Pinned llama.cpp commit; `gate.serve` refuses a binary built from another commit |
| `templates/qwen2.5-instruct.jinja` | Reference chat template: the `tokenizer.chat_template` string embedded in `qwen2.5-1.5b-instruct-q8_0.gguf`, byte for byte (checked by `tests/test_fr03_serving_config.py`) |

Commands (from the repo root):

```
make models   # download into models/ (gitignored) and record or check SHA-256
make verify   # FR-02: exit 1 if the served model differs from the manifest
make serve    # llama-server from serving/config.yaml (CONFIG=path to use another config)
```

`llama-server` is found through `LLAMA_CPP_BIN` (default `~/llama.cpp/build/bin`).

Reference key (PRD Section 6.8): the reference measurement (`make reference SEED=0`) and the D2
reference logits are stored under `reference/<model-sha256>/<config-hash>/<instance-type>/`.
`<config-hash>` is the SHA-256 of the effective serving config; `gate/reference.py` defines
exactly which fields count (host, port and comments do not; template bytes, KV cache, context,
threads, sampler and the llama.cpp commit do).

`threads` maps the reference key's instance type to a count: `local-Sankalps-Razer` uses 8,
`t3.large` uses 2, and other machines use `default` (2). The launcher uses the same machine
resolution as the reference key, including `DRIFTGATE_INSTANCE_TYPE`. The whole map is hashed;
F9 overrides it with the scalar 1. See `docs/deviations/2026-11-19-thread-count.md`.
