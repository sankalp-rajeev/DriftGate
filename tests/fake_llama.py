"""A deterministic stand-in for llama-server, enough for the gate CLI (hook tests only).

Serves /health, /props, /apply-template and /completion. Outputs depend only on the prompt and
`shift`: shift 0 always answers the same, so a measurement compared with itself agrees fully;
a non-zero shift changes every generated token (a behaviour change the gate should see).
"""

import hashlib
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLlama:
    def __init__(self, shift=0, errors=None, delays=None):
        self.shift = shift
        self.errors = errors or {}
        self.delays = delays or {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass  # timeout regression: the client has already disconnected

            def do_GET(self):
                if self.path == "/health":
                    self._send({"status": "ok"})
                elif self.path == "/props":
                    if 0 in fake.errors:
                        status, message = fake.errors[0]
                        self._send({"error": {"message": message}}, status)
                    else:
                        self._send({"model_path": "models/fake.gguf", "build_info": "fake"})
                else:
                    self._send({"error": "not found"}, 404)

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/apply-template":
                    text = req["messages"][-1]["content"]
                    self._send({"prompt": f"<|user|>{text}<|assistant|>"})
                elif self.path == "/completion":
                    time.sleep(fake.delays.get(req.get("n_predict"), 0))
                    if req.get("n_predict") in fake.errors:
                        status, message = fake.errors[req["n_predict"]]
                        self._send({"error": {"message": message}}, status)
                        return
                    self._send(fake.completion(req))
                else:
                    self._send({"error": "not found"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def completion(self, req):
        seed = int(hashlib.sha256(req["prompt"].encode()).hexdigest()[:8], 16) + self.shift
        n = req.get("n_predict", 1)
        timings = {"prompt_ms": 20.0, "predicted_ms": 5.0 * n, "prompt_n": 10, "predicted_n": n}
        if n == 1:
            return {"content": " " + "ABCD"[seed % 4], "timings": timings}
        steps = []
        for i in range(n):
            tok = (seed * 31 + i) % 1000
            top = [{"id": tok, "logprob": math.log(0.9)}]
            top += [{"id": (tok + k) % 1000, "logprob": math.log(0.01)} for k in range(1, 10)]
            steps.append({"id": tok, "top_logprobs": top})
        return {"content": "", "completion_probabilities": steps, "timings": timings}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()
