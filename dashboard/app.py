"""Local web dashboard (standard library only). Binds to 127.0.0.1 by default.

Security: no credentials are ever exposed; state-changing requests need a custom header and a
local Host/Origin (blocks cross-site requests); emergency/resume need explicit confirmation outside PAPER mode.
"""
from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from bot.logging_config import get_logger

log = get_logger("bot.dashboard")
HERE = Path(__file__).resolve().parent
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
CONFIRM_ACTIONS = ("emergency_close", "resume")


def _host_ok(value: str) -> bool:
    host = (value or "").split("://")[-1].rsplit(":", 1)[0] if ":" in (value or "") else (value or "")
    return host.strip("/") in LOCAL_HOSTS


class DashboardServer:
    def __init__(self, engine: Any, host: str = "127.0.0.1", port: int = 8080):
        self.engine = engine
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AITraderDashboard"

            def log_message(self, fmt: str, *args: Any) -> None:   # keep console quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any) -> None:
                self._send(code, json.dumps(obj, default=str).encode())

            def do_GET(self) -> None:
                if not _host_ok(self.headers.get("Host", "")):
                    return self._json(403, {"error": "forbidden host"})
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    return self._send(200, (HERE / "templates" / "index.html").read_bytes(), "text/html; charset=utf-8")
                if path == "/api/state":
                    try:
                        return self._json(200, outer.engine.get_state())
                    except Exception as e:
                        log.exception("state error")
                        return self._json(500, {"error": f"{type(e).__name__}"})
                if path.startswith("/static/"):
                    f = (HERE / "static" / path[len("/static/"):]).resolve()
                    if (HERE / "static").resolve() in f.parents and f.is_file():
                        return self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream")
                return self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not _host_ok(self.headers.get("Host", "")):
                    return self._json(403, {"error": "forbidden host"})
                origin = self.headers.get("Origin")
                if origin and not _host_ok(origin):
                    return self._json(403, {"error": "forbidden origin"})
                if self.headers.get("X-Requested-By") != "ai-trader":
                    return self._json(403, {"error": "missing X-Requested-By header"})
                if self.path != "/api/control":
                    return self._json(404, {"error": "not found"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    body: Dict[str, Any] = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "bad json"})
                code, out = outer.control(str(body.get("action", "")), body.get("confirm"))
                self._json(code, out)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self.host = host
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="dashboard")

    # ---------------------------------------------------------------- control actions
    def control(self, action: str, confirm: Any):
        e = self.engine
        mode = getattr(e.cfg, "mode", "PAPER")
        if action in CONFIRM_ACTIONS and mode != "PAPER" and confirm != mode:
            return 400, {"ok": False, "error": f"confirmation required: send confirm={mode!r}"}
        log.warning("dashboard control: %s", action)
        try:
            if action == "pause":
                e.pause()
                return 200, {"ok": True}
            if action == "resume":
                return 200, e.resume()
            if action == "emergency_close":
                return 200, e.emergency("dashboard emergency close")
            if action == "test_ai":
                return 200, e.test_ai()
            if action == "refresh":
                return 200, {"ok": True, **e.refresh_connection()}
        except Exception as ex:
            log.exception("control error")
            return 500, {"ok": False, "error": type(ex).__name__}
        return 400, {"ok": False, "error": "unknown action"}

    def start(self) -> "DashboardServer":
        self.thread.start()
        log.info("dashboard listening on http://%s:%s", self.host, self.port)
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
