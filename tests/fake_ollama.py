"""A tiny fake Ollama HTTP server for tests (real HTTP, scripted replies)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self, models=("qwen3:14b",)):
        self.models = list(models)
        self.chat_replies = []       # queue of dicts: {"message": {...}} or {"status": 500} or {"sleep": s}
        self.chat_requests = []
        self.pull_requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client timed out (expected in timeout tests)

            def do_GET(self):
                if self.path == "/api/version":
                    self._send(200, {"version": "0.99.0"})
                elif self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m} for m in outer.models]})
                else:
                    self._send(404, {"error": "nope"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/chat":
                    outer.chat_requests.append(body)
                    rep = outer.chat_replies.pop(0) if outer.chat_replies else {"message": {"role": "assistant", "content": "{}"}}
                    if rep.get("sleep"):
                        import time
                        time.sleep(rep["sleep"])
                    if rep.get("status"):
                        self._send(rep["status"], {"error": rep.get("error", "boom")})
                    else:
                        self._send(200, rep)
                elif self.path == "/api/pull":
                    outer.pull_requests.append(body)
                    self.send_response(200)
                    self.end_headers()
                    for st in ("pulling manifest", "downloading", "success"):
                        self.wfile.write((json.dumps({"status": st}) + "\n").encode())
                    outer.models.append(body["model"])
                else:
                    self._send(404, {"error": "nope"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


def msg(content="", tool_calls=None):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return {"message": m}
