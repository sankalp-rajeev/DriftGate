"""Minimal stdlib HTTP client for llama-server (used by detector data builds and the gate)."""

from __future__ import annotations

import json
import urllib.request


class LlamaServer:
    def __init__(self, base_url: str, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as r:
            return json.loads(r.read())

    def props(self) -> dict:
        return self._get("/props")

    def tokenize(self, text: str) -> list[int]:
        return self._post("/tokenize", {"content": text, "add_special": False})["tokens"]

    def detokenize(self, tokens: list[int]) -> str:
        return self._post("/detokenize", {"tokens": tokens})["content"]

    def apply_template(self, messages: list[dict]) -> str:
        return self._post("/apply-template", {"messages": messages})["prompt"]

    def completion(self, prompt: str, **params) -> dict:
        return self._post("/completion", {"prompt": prompt, **params})


def user_prompt(server: LlamaServer, text: str) -> str:
    """Render one user turn through the server's chat template, ready for generation."""
    return server.apply_template([{"role": "user", "content": text}])
