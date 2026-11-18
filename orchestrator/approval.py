"""Approval handler behind a Lambda function URL (FR-27, ADR-010).

The SNS email carries approve and reject links with a one-time token. A GET only renders a
confirmation page (mail scanners that prefetch links must not consume the token); the POST
from that page applies the decision through `Orchestrator.decide`, which checks the token
hash, the deadline and that the approval is still pending, and on approve starts Deploy.
"""

from __future__ import annotations

import base64
import html
import json
import logging
from typing import Any
from urllib.parse import parse_qs

from orchestrator.config import load_config
from orchestrator.handler import DECISIONS, Orchestrator

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

STATUS_BY_RESULT = {
    "approved": 200,
    "rejected": 200,
    "timeout": 410,
    "unknown_run": 404,
    "not_pending": 409,
    "bad_token": 403,
    "bad_decision": 400,
}

MESSAGES = {
    "approved": "Approved. Deploy has started.",
    "rejected": "Rejected. The run has ended.",
    "timeout": "The approval window has passed; the run was rejected.",
    "unknown_run": "Unknown run.",
    "not_pending": "This run is not waiting for approval (already decided or timed out).",
    "bad_token": "Invalid or already used link.",
    "bad_decision": "Unknown decision.",
}


def _page(status: int, title: str, body: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
        "body": (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title></head><body>{body}</body></html>"
        ),
    }


def _params(event: dict[str, Any]) -> dict[str, str]:
    params = dict(event.get("queryStringParameters") or {})
    raw = event.get("body") or ""
    if raw:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode()
        params.update({k: v[0] for k, v in parse_qs(raw).items()})
    return params


def confirm_page(run_id: str, decision: str, token: str) -> dict[str, Any]:
    e = html.escape
    form = (
        f"<h1>DriftGate: {e(decision)} run {e(run_id)}?</h1>"
        "<form method='post'>"
        f"<input type='hidden' name='run_id' value='{e(run_id, quote=True)}'>"
        f"<input type='hidden' name='decision' value='{e(decision, quote=True)}'>"
        f"<input type='hidden' name='token' value='{e(token, quote=True)}'>"
        f"<button type='submit'>Confirm {e(decision)}</button></form>"
    )
    return _page(200, f"Confirm {decision}", form)


def handle(event: dict[str, Any], orchestrator: Orchestrator) -> dict[str, Any]:
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    p = _params(event)
    run_id, decision, token = p.get("run_id", ""), p.get("decision", ""), p.get("token", "")
    if method == "GET":
        if decision not in DECISIONS or not run_id or not token:
            return _page(400, "Bad request", "<p>Malformed approval link.</p>")
        return confirm_page(run_id, decision, token)
    if method != "POST":
        return _page(405, "Method not allowed", "<p>Method not allowed.</p>")
    actor = event.get("requestContext", {}).get("http", {}).get("sourceIp")
    out = orchestrator.decide(run_id, decision, token, actor=actor)
    # Never log the token.
    log.info(json.dumps({"approval": out["result"], "run_id": run_id, "source_ip": actor}))
    result = out["result"]
    return _page(STATUS_BY_RESULT[result], result, f"<p>{html.escape(MESSAGES[result])}</p>")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return handle(event, Orchestrator(load_config()))
