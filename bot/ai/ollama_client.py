"""Minimal client for Ollama's native local API (/api/chat, /api/tags, /api/pull).

Local only. Uses the standard library so it has no extra dependencies, and takes
an injectable `http` callable so it can be tested without a server.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterator, List, Optional

from ..logging_config import get_logger

log = get_logger("bot.ai")


class OllamaError(Exception):
    pass


class OllamaUnavailable(OllamaError):
    """Server not reachable (not running / connection refused / server-side error)."""


class OllamaTimeout(OllamaUnavailable):
    pass


class OllamaHTTPError(OllamaError):
    """The server answered with an error status."""
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def _default_http(method: str, url: str, body: Optional[dict], timeout: float, stream: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        text = e.read()[:400].decode(errors="replace")
        try:
            text = json.loads(text).get("error", text)
        except ValueError:
            pass
        raise OllamaHTTPError(e.code, text) from e
    except (socket.timeout, TimeoutError) as e:
        raise OllamaTimeout("Ollama request timed out") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise OllamaTimeout("Ollama request timed out") from e
        raise OllamaUnavailable(f"cannot reach Ollama at {url}: {e.reason}") from e
    if stream:
        return (json.loads(line) for line in resp if line.strip())
    try:
        return json.loads(resp.read())
    except socket.timeout as e:
        raise OllamaTimeout("Ollama response timed out") from e


class OllamaClient:
    def __init__(self, host: str = "http://127.0.0.1:11434", timeout: float = 90.0,
                 max_retries: int = 1, http: Optional[Callable[..., Any]] = None,
                 sleep: Callable[[float], None] = time.sleep):
        host = host.rstrip("/")
        if not (host.startswith("http://127.0.0.1") or host.startswith("http://localhost")):
            raise OllamaError("Only a local Ollama server is allowed (127.0.0.1 / localhost)")
        self.host, self.timeout, self.max_retries = host, timeout, max_retries
        self._http = http or _default_http
        self._sleep = sleep

    # --- health ------------------------------------------------------------------
    def is_up(self) -> bool:
        try:
            self._http("GET", f"{self.host}/api/version", None, 5)
            return True
        except OllamaError:
            return False

    def list_models(self) -> List[str]:
        data = self._http("GET", f"{self.host}/api/tags", None, 10)
        return [m["name"] for m in data.get("models", [])]

    def has_model(self, name: str) -> bool:
        names = self.list_models()
        return any(n == name or n == f"{name}:latest" or n.split(":latest")[0] == name for n in names)

    def version(self) -> str:
        return str(self._http("GET", f"{self.host}/api/version", None, 5).get("version", "unknown"))

    def pull(self, name: str, progress: Optional[Callable[[str], None]] = None) -> None:
        """Download a model, reporting status text. Only called after explicit user confirmation in setup."""
        for evt in self._http("POST", f"{self.host}/api/pull", {"model": name, "stream": True}, 3600, True):
            if evt.get("error"):
                raise OllamaError(str(evt["error"]))
            if progress:
                progress(str(evt.get("status", "")))

    # --- chat ------------------------------------------------------------------------
    def chat(self, model: str, messages: List[Dict[str, Any]], *, schema: Optional[dict] = None,
             tools: Optional[List[dict]] = None, temperature: float = 0.1, num_ctx: int = 8192,
             think: bool = False, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Returns the assistant message dict ({'role','content','tool_calls'?})."""
        body: Dict[str, Any] = {
            "model": model, "messages": messages, "stream": False, "think": bool(think),
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        if schema:
            body["format"] = schema
        if tools:
            body["tools"] = tools
        delay = 1.0
        last: Optional[Exception] = None
        attempt = 0
        while attempt <= self.max_retries:
            try:
                data = self._http("POST", f"{self.host}/api/chat", body, timeout or self.timeout)
                if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
                    raise OllamaError("unexpected response shape from Ollama")
                return data["message"]
            except OllamaHTTPError as e:
                if "think" in body and "think" in e.message.lower() and e.status == 400:
                    body = {k: v for k, v in body.items() if k != "think"}   # model has no thinking mode
                    continue                                                 # not counted as a retry
                if e.status >= 500 or e.status == 429:
                    last = OllamaUnavailable(str(e))
                else:
                    raise
            except OllamaUnavailable as e:      # includes OllamaTimeout
                last = e
            log.warning("Ollama attempt %d failed: %s", attempt + 1, last)
            if attempt < self.max_retries:
                self._sleep(delay)
                delay = min(delay * 2, 30)
            attempt += 1
        assert last is not None
        raise last if isinstance(last, OllamaUnavailable) else OllamaUnavailable(str(last))
