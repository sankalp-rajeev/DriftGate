"""Build the frozen detector data files (PRD Section 6.2.1, T-30).

Subcommands, each deterministic given its inputs and `--seed` (default 0, as the PRD fixes):

  d2         first 2,048 reference-tokenizer tokens of WikiText-2 raw test -> d2_corpus.txt
  d3         256 Dolly instructions (empty context, 20..300 chars)       -> d3_prompts.jsonl
  d5-score   score a seeded sample of 2,000 MMLU test questions on the reference model
  d5-select  "sometimes right" items -> d5_panel.jsonl (256) and d5_hidden_pool.jsonl (<=512)
  hashes     record SHA-256 of every data file in gate/data/manifest.json

`d2` and `d5-score` need a running llama-server on the reference model (`make serve`).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from gate.llama_client import LlamaServer, user_prompt
from gate.manifest import load_yaml
from gate.prompts import mmlu_prompt

SOURCES = Path("gate/data/sources.yaml")
DATA_DIR = Path("gate/data")


# ---------- downloads (stdlib only) ----------


def _download(url: str, cache: Path) -> bytes:
    if cache.exists():
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=600) as r:
        data = r.read()
    cache.write_bytes(data)
    return data


def _hf_file_url(src: dict) -> str:
    return f"https://huggingface.co/datasets/{src['repo']}/resolve/{src['commit']}/{src['file']}"


# ---------- D2 ----------


def wikitext_rows(src: dict, cache_dir: Path) -> list[str]:
    """All rows of the WikiText-2 raw test split via the HF dataset viewer rows API."""
    rows: list[str] = []
    offset, page = 0, 100
    while True:
        q = urllib.parse.urlencode(
            {
                "dataset": src["repo"],
                "config": src["config"],
                "split": src["split"],
                "offset": offset,
                "length": page,
            }
        )
        cache = cache_dir / f"wikitext-{src['config']}-{src['split']}-{offset}.json"
        body = json.loads(_download(f"{src['rows_api']}?{q}", cache))
        batch = [r["row"]["text"] for r in body["rows"]]
        rows.extend(batch)
        offset += len(batch)
        if not batch or offset >= body["num_rows_total"] or offset >= src["max_rows"]:
            return rows


def wikitext_text(rows: list[str]) -> str:
    """Rows are the lines of the raw file; an empty row is a blank line."""
    return "".join(r if r else "\n" for r in rows)


def first_n_tokens(server: LlamaServer, text: str, n: int) -> str:
    tokens = server.tokenize(text)
    if len(tokens) < n:
        raise ValueError(f"source text has only {len(tokens)} tokens, need {n}")
    out = server.detokenize(tokens[:n])
    if len(server.tokenize(out)) != n:
        raise ValueError("first-N-token text does not re-tokenize to N tokens")
    return out


# ---------- D3 ----------


def select_d3(records: list[dict], n: int, seed: int, min_len: int, max_len: int) -> list[dict]:
    """Records with empty context and instruction length in [min_len, max_len], seeded sample.

    `random.Random(seed).sample` over the eligible records in source-file order; the output
    order is the sample order, so budgets are its first N records.
    """
    eligible = [
        r for r in records if r["context"] == "" and min_len <= len(r["instruction"]) <= max_len
    ]
    picked = random.Random(seed).sample(eligible, n)
    return [
        {"id": r["_line"], "category": r["category"], "instruction": r["instruction"]}
        for r in picked
    ]


def load_dolly(data: bytes) -> list[dict]:
    out = []
    for i, line in enumerate(data.decode().splitlines()):
        if line.strip():
            rec = json.loads(line)
            rec["_line"] = i
            out.append(rec)
    return out


# ---------- D5 ----------


def load_mmlu_test(tar_bytes: bytes) -> list[dict]:
    """MMLU test questions in canonical order: subject files sorted by name, rows in file order."""
    items = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        members = sorted(
            (
                m
                for m in tf.getmembers()
                if m.isfile() and "/test/" in m.name and m.name.endswith("_test.csv")
            ),
            key=lambda m: m.name,
        )
        for m in members:
            subject = Path(m.name).name.removesuffix("_test.csv")
            text = tf.extractfile(m).read().decode()
            for row_i, row in enumerate(csv.reader(io.StringIO(text))):
                question, a, b, c, d, answer = row
                items.append(
                    {
                        "id": f"{subject}/{row_i}",
                        "subject": subject,
                        "question": question,
                        "choices": [a, b, c, d],
                        "answer": answer.strip(),
                    }
                )
    return items


def gold_letter_prob(top: list[dict], gold: str) -> float:
    """Probability mass of the gold letter among the first token's top-10 (absent = 0).

    A top-10 token counts as the gold letter when its text, stripped of whitespace, equals it.
    """
    return sum(math.exp(t["logprob"]) for t in top if t["token"].strip() == gold)


def score_item(server: LlamaServer, item: dict, n_probs: int, seed: int) -> dict:
    prompt = user_prompt(server, mmlu_prompt(item))
    resp = server.completion(
        prompt,
        n_predict=1,
        temperature=0,
        n_probs=n_probs,
        seed=seed,
        cache_prompt=False,
    )
    first = resp["completion_probabilities"][0]
    return {
        "id": item["id"],
        "answer": item["answer"],
        "first_token": first["token"],
        "gold_prob": gold_letter_prob(first["top_logprobs"], item["answer"]),
    }


def select_d5(
    items_by_id: dict,
    scores: list[dict],
    lo: float,
    hi: float,
    seed: int,
    n_panel: int,
    n_pool: int,
):
    eligible = [s for s in scores if lo <= s["gold_prob"] <= hi]
    random.Random(seed).shuffle(eligible)
    rows = [items_by_id[s["id"]] | {"reference_gold_prob": s["gold_prob"]} for s in eligible]
    return rows[:n_panel], rows[n_panel : n_panel + n_pool]


# ---------- io ----------


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------- guards ----------


def check_revision(src: dict) -> None:
    """The rows API serves the dataset's current revision: refuse if it is not the pinned one."""
    url = f"https://huggingface.co/api/datasets/{src['repo']}"
    with urllib.request.urlopen(url, timeout=60) as r:
        sha = json.loads(r.read())["sha"]
    if sha != src["commit"]:
        raise SystemExit(f"{src['repo']} is at {sha}, sources.yaml pins {src['commit']}")


def require_reference(server: LlamaServer, models_yaml: Path = Path("serving/models.yaml")) -> None:
    """Selection uses the reference model only (PRD Section 6.2.1)."""
    reference = next(
        m["file"] for m in load_yaml(models_yaml)["models"] if m["role"] == "reference"
    )
    served = Path(server.props()["model_path"]).name
    if served != reference:
        raise SystemExit(f"server runs {served}; detector data must be built on {reference}")


DATA_FILES = [
    "sources.yaml",
    "d2_corpus.txt",
    "d3_prompts.jsonl",
    "d5_reference_scores.jsonl",
    "d5_panel.jsonl",
    "d5_hidden_pool.jsonl",
]


def data_manifest(data_dir: Path = DATA_DIR) -> dict:
    return {
        "schema_version": 1,
        "files": {name: sha256(data_dir / name) for name in DATA_FILES},
    }


# ---------- CLI ----------


def cmd_d2(args, sources) -> int:
    src = sources["d2"]
    server = LlamaServer(args.server)
    require_reference(server)
    check_revision(src)
    rows = wikitext_rows(src, args.cache)
    text = first_n_tokens(server, wikitext_text(rows), src["tokens"])
    (DATA_DIR / "d2_corpus.txt").write_text(text)
    print(json.dumps({"file": "gate/data/d2_corpus.txt", "tokens": src["tokens"]}))
    return 0


def cmd_d3(args, sources) -> int:
    src = sources["d3"]
    records = load_dolly(_download(_hf_file_url(src), args.cache / src["file"]))
    rows = select_d3(records, src["n"], args.seed, src["min_chars"], src["max_chars"])
    write_jsonl(DATA_DIR / "d3_prompts.jsonl", rows)
    print(json.dumps({"file": "gate/data/d3_prompts.jsonl", "records": len(rows)}))
    return 0


def _mmlu(args, src) -> list[dict]:
    return load_mmlu_test(_download(_hf_file_url(src), args.cache / src["file"]))


def cmd_d5_score(args, sources) -> int:
    src = sources["d5"]
    items = _mmlu(args, src)
    sample = random.Random(args.seed).sample(items, src["sample"])
    server = LlamaServer(args.server)
    require_reference(server)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {r["id"] for r in read_jsonl(out)} if out.exists() else set()
    t0 = time.monotonic()
    with open(out, "a") as f:
        for i, item in enumerate(sample):
            if item["id"] in done:
                continue
            f.write(json.dumps(score_item(server, item, src["n_probs"], args.seed)) + "\n")
            f.flush()
            if i % 100 == 0:
                print(f"{i}/{len(sample)} {time.monotonic() - t0:.0f}s", file=sys.stderr)
    # Keep the file in sample order so it is reproducible byte for byte.
    by_id = {r["id"]: r for r in read_jsonl(out)}
    write_jsonl(out, [by_id[it["id"]] for it in sample])
    print(json.dumps({"file": str(out), "scored": len(sample)}))
    return 0


def cmd_d5_select(args, sources) -> int:
    src = sources["d5"]
    items_by_id = {it["id"]: it for it in _mmlu(args, src)}
    scores = read_jsonl(Path(args.scores))
    panel, pool = select_d5(
        items_by_id, scores, src["lo"], src["hi"], args.seed, src["panel"], src["hidden_pool"]
    )
    write_jsonl(DATA_DIR / "d5_panel.jsonl", panel)
    write_jsonl(DATA_DIR / "d5_hidden_pool.jsonl", pool)
    print(json.dumps({"panel": len(panel), "hidden_pool": len(pool)}))
    return 0


def cmd_hashes(args, sources) -> int:
    path = DATA_DIR / "manifest.json"
    path.write_text(json.dumps(data_manifest(), indent=2) + "\n")
    print(path.read_text(), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="analysis.detector_data", description=__doc__.splitlines()[0])
    p.add_argument("cmd", choices=["d2", "d3", "d5-score", "d5-select", "hashes"])
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--config", type=Path, default=Path("serving/config.yaml"))
    p.add_argument("--server", help="llama-server base URL (default: host and port from --config)")
    p.add_argument("--cache", type=Path, default=Path("artifacts/sources"))
    p.add_argument("--out", default="gate/data/d5_reference_scores.jsonl")
    p.add_argument("--scores", default="gate/data/d5_reference_scores.jsonl")
    args = p.parse_args(argv)
    sources = load_yaml(SOURCES)
    if args.server is None:
        server = load_yaml(args.config)["server"]
        args.server = f"http://{server['host']}:{server['port']}"
    cmds = {
        "d2": cmd_d2,
        "d3": cmd_d3,
        "d5-score": cmd_d5_score,
        "d5-select": cmd_d5_select,
        "hashes": cmd_hashes,
    }
    return cmds[args.cmd](args, sources)


if __name__ == "__main__":
    sys.exit(main())
