"""Minimal stdlib HTTP client for llama-server (used by detector data builds and the gate)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class RequestError(OSError):
    """An HTTP response error or timeout, preserved as JSON detector evidence."""

    def __init__(self, status_code: int | None, message: str, kind: str):
        super().__init__(message)
        self.details = {"status_code": status_code, "message": message, "kind": kind}


def detector_error(rows: list[dict]) -> dict | None:
    """The first failed request in this detector's budget, or None."""
    for row in rows:
        if "error" in row:
            return {"status": "error", "error": {**row["error"], "item_id": row["id"]}}
    return None


class LlamaServer:
    def __init__(self, base_url: str, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, req) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RequestError(e.code, body or str(e), "http") from e
        except (TimeoutError, urllib.error.URLError) as e:
            if isinstance(e, urllib.error.URLError) and not isinstance(e.reason, TimeoutError):
                raise
            raise RequestError(None, str(e) or "request timed out", "timeout") from e

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._request(req)

    def _get(self, path: str) -> dict:
        return self._request(self.base_url + path)

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
