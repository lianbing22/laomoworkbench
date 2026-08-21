"""Minimal OpenAI Responses API mock for provider E2E tests (stdlib only).

Serves just enough of the Responses wire protocol to validate custom
provider profiles end-to-end without any real API key:

    POST /v1/responses   (alias: POST /responses)
        - Authorization must be "Bearer <non-empty>" -> else 401
          {"error": {"message": "Invalid API key"}}
        - body must be JSON with a non-empty "model" -> else 400
        - model == "missing-model" -> 404 {"error": {"message": "model not found"}}
        - otherwise 200 with a non-streaming Responses API shape, or an SSE
          event stream when run with --sse (or when the request has
          "stream": true)

    GET /__test_log
        - JSON summary of the API requests received so far
          ({"method", "path", "auth", "authOk", "model", "status"}) so test
          assertions can verify what the runtime actually sent.

Standalone:  python3 tests/mock_responses_server.py [port] [--sse] [--host H]
Default port: 18652. The Authorization token is never echoed into the log.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 18652
MAX_LOG_ENTRIES = 200

REQUEST_LOG: list[dict] = []
_LOG_LOCK = threading.Lock()

_RESPONSES_PATHS = ("/v1/responses", "/responses")


def clear_log() -> None:
    """Reset the request summary log (test isolation helper)."""
    with _LOG_LOCK:
        REQUEST_LOG.clear()


def _log_entry(entry: dict) -> None:
    with _LOG_LOCK:
        REQUEST_LOG.append(entry)
        del REQUEST_LOG[:-MAX_LOG_ENTRIES]


def _response_body(model: str) -> dict:
    """Non-streaming Responses API response shape."""
    return {
        "id": "resp_mock",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": model,
        "output": [{
            "type": "message",
            "id": "msg_mock",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "OK", "annotations": []}],
        }],
        "usage": {
            "input_tokens": 9,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 11,
        },
        "error": None,
    }


def _sse_events(model: str, resp: dict) -> list[tuple[str, dict]]:
    """Ordered Responses API streaming events ending in response.completed."""
    return [
        ("response.created", {
            "type": "response.created",
            "response": {"id": resp["id"], "object": "response", "status": "in_progress"},
        }),
        ("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_mock", "type": "message", "role": "assistant",
                     "status": "in_progress", "content": []},
        }),
        ("response.content_part.added", {
            "type": "response.content_part.added",
            "item_id": "msg_mock", "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        }),
        ("response.output_text.delta", {
            "type": "response.output_text.delta",
            "item_id": "msg_mock", "output_index": 0, "content_index": 0,
            "delta": "OK",
        }),
        ("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": "msg_mock", "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": "OK", "annotations": []},
        }),
        ("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": resp["output"][0],
        }),
        ("response.completed", {"type": "response.completed", "response": resp}),
    ]


class ResponsesMockHandler(BaseHTTPRequestHandler):
    server_version = "LaomoResponsesMock/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing --
    def log_message(self, fmt, *args):  # silence default stderr access log
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, model: str) -> None:
        resp = _response_body(model)
        stream = "".join(
            f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            for event, data in _sse_events(model, resp)
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(stream)))
        self.end_headers()
        self.wfile.write(stream)

    # -- routes --
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/__test_log", "/__test_log/"):
            with _LOG_LOCK:
                snapshot = list(REQUEST_LOG)
            self._send_json(200, {"ok": True, "count": len(snapshot), "requests": snapshot})
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in _RESPONSES_PATHS:
            self._send_json(404, {"error": {"message": "not found"}})
            return

        auth = self.headers.get("Authorization") or ""
        auth_ok = auth.startswith("Bearer ") and auth[len("Bearer "):].strip() != ""

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        model = body.get("model") if isinstance(body, dict) else None

        entry = {
            "method": "POST",
            "path": path,
            # redacted scheme summary only; the token itself is never logged
            "auth": (auth.split(" ", 1)[0] + " ********") if auth else None,
            "authOk": auth_ok,
            "model": model,
        }

        if not auth_ok:
            entry["status"] = 401
            _log_entry(entry)
            self._send_json(401, {"error": {"message": "Invalid API key"}})
            return
        if not isinstance(body, dict) or not model:
            entry["status"] = 400
            _log_entry(entry)
            self._send_json(400, {"error": {"message": "model required"}})
            return
        if model == "missing-model":
            entry["status"] = 404
            _log_entry(entry)
            self._send_json(404, {"error": {"message": "model not found"}})
            return

        entry["status"] = 200
        _log_entry(entry)
        if getattr(self.server, "sse_mode", False) or body.get("stream"):
            self._send_sse(str(model))
        else:
            self._send_json(200, _response_body(str(model)))


def make_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT, sse: bool = False,
                bind_and_activate: bool = True) -> ThreadingHTTPServer:
    """Build (but do not serve) the mock; port=0 picks an ephemeral port.
    SSE mode is per-server so a plain and an --sse instance can coexist."""
    httpd = ThreadingHTTPServer((host, port), ResponsesMockHandler, bind_and_activate)
    httpd.daemon_threads = True
    httpd.sse_mode = bool(sse)
    return httpd


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Minimal OpenAI Responses API mock server")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--sse", action="store_true",
                        help="always answer with an SSE event stream instead of JSON")
    args = parser.parse_args(argv)
    httpd = make_server(args.host, args.port, sse=args.sse)
    print(f"mock responses server listening on http://{args.host}:{args.port} "
          f"(sse={args.sse}, log: GET /__test_log)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
