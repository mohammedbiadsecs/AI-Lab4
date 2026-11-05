#!/usr/bin/env python3
"""proxy.py: the naive agent-traffic proxy for Lab 4.

A reverse proxy that prints every request and response and forwards them
unchanged. Standard library only; nothing to install.

    python3 proxy.py                       # listens on http://localhost:8041
    ANTHROPIC_BASE_URL=http://localhost:8041 claude

Everything Claude Code sends to the API now passes through this program and is
printed to the terminal. The proxy changes nothing: headers go through untouched
(a subscription login needs its bearer token and its `anthropic-beta` header),
and the reply is streamed back byte for byte as it arrives.

    python3 proxy.py --port 9000
    python3 proxy.py --upstream https://api.openai.com/v1   # for a Codex Responses provider
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from monitor import (
    MAX_CAPTURE_BYTES,
    EventStore,
    parse_body,
    parse_response_body,
    redact,
    redact_url,
    tool_calls,
    tool_results,
)

# Headers that describe one hop of the connection, not the message. They are
# never forwarded. `accept-encoding` is dropped too, so the API answers in plain
# text and what we print is readable.
SKIP = {
    "host", "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authorization", "proxy-authenticate", "accept-encoding",
    "content-length",
}

UPSTREAM = "https://api.anthropic.com"
MONITOR_PREFIX = "/_monitor"
STATIC_DIR = Path(__file__).with_name("dashboard")
EVENTS = EventStore()

# Browser-generated asset requests are still proxied, but they are not model
# traffic and should not add request/response noise to the monitor.
BROWSER_ASSET_PATHS = {
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    "/site.webmanifest",
    "/manifest.json",
}
BROWSER_ASSET_DESTINATIONS = {"font", "image", "manifest", "script", "style"}


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Tap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # we print our own lines

    def do_POST(self) -> None:
        self.relay()

    def do_GET(self) -> None:
        if self.monitor_get():
            return
        self.relay(capture=self.should_capture())

    def do_HEAD(self) -> None:
        if self.path.split("?", 1)[0] in {MONITOR_PREFIX, f"{MONITOR_PREFIX}/"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.relay(capture=self.should_capture())

    def do_DELETE(self) -> None:
        if self.path.split("?", 1)[0] == f"{MONITOR_PREFIX}/events":
            EVENTS.clear()
            self.send_json({"ok": True, "events": []})
            return
        self.send_error(501, "Unsupported method")

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, filename: str, content_type: str) -> None:
        try:
            body = (STATIC_DIR / filename).read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def monitor_get(self) -> bool:
        path = self.path.split("?", 1)[0]
        if path == f"{MONITOR_PREFIX}/events":
            self.send_json({"events": EVENTS.snapshot()})
        elif path in {MONITOR_PREFIX, f"{MONITOR_PREFIX}/"}:
            self.send_static("index.html", "text/html; charset=utf-8")
        elif path == f"{MONITOR_PREFIX}/app.js":
            self.send_static("app.js", "text/javascript; charset=utf-8")
        elif path == f"{MONITOR_PREFIX}/styles.css":
            self.send_static("styles.css", "text/css; charset=utf-8")
        else:
            return False
        return True

    def should_capture(self) -> bool:
        path = self.path.split("?", 1)[0].lower()
        destination = (self.headers.get("sec-fetch-dest") or "").lower()
        return path not in BROWSER_ASSET_PATHS and destination not in BROWSER_ASSET_DESTINATIONS

    def relay(self, *, capture: bool = True) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        target = urlsplit(UPSTREAM)
        upstream_path = target.path.rstrip("/") + self.path

        request_headers = dict(self.headers.items())
        request_id = self.headers.get("x-request-id") or self.headers.get("request-id")
        captured_body = None
        if capture:
            captured_body = parse_body(body, self.headers.get("content-type", "")) if body else None
            request_data = {
                "method": self.command,
                "path": redact_url(self.path),
                "upstreamPath": redact_url(upstream_path),
                "headers": redact(request_headers),
                "body": captured_body,
            }
            model = captured_body.get("model") if isinstance(captured_body, dict) else None
            EVENTS.add(
                "model_request",
                request_data,
                f"{self.command} {self.path.split('?', 1)[0]}",
                request_id=request_id,
                metadata={"model": model} if model else None,
            )
            if captured_body is not None:
                for result in tool_results(captured_body):
                    call_id = result.get("tool_use_id") or result.get("call_id")
                    result_metadata = {}
                    if call_id:
                        result_metadata["callId"] = call_id
                    if "is_error" in result:
                        result_metadata["isError"] = result.get("is_error")
                    EVENTS.add(
                        "tool_result",
                        result,
                        f"Tool result{f' · {call_id}' if call_id else ''}",
                        request_id=request_id,
                        metadata=result_metadata or None,
                    )

        # 1. Print what the agent sent.
        print(f"\n{'=' * 78}\n[{stamp()}] >>> {self.command} {self.path}", flush=True)
        print(f"    upstream path: {redact_url(upstream_path)}")
        for name, value in self.headers.items():
            if name.lower() in ("authorization", "x-api-key"):
                value = value[:16] + "..."
            print(f"    {name}: {value}")
        if body:
            try:
                print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
            except ValueError:
                print(body.decode("utf-8", "replace"))

        # 2. Forward it, headers intact.
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        # Connect with a short timeout (a lost SYN must not stall the agent for
        # minutes); the long timeout is for reading a slow stream.
        upstream = conn_cls(target.netloc, timeout=10)
        try:
            upstream.connect()
        except OSError as e:
            print(f"\n[{stamp()}] !!! cannot reach {UPSTREAM}: {e}", flush=True)
            if capture:
                EVENTS.add(
                    "error",
                    {"message": str(e), "upstream": UPSTREAM, "path": redact_url(self.path)},
                    "Could not reach upstream",
                    request_id=request_id,
                    metadata={"status": 502},
                )
            self.send_error(502, f"proxy: cannot reach {UPSTREAM}: {e}")
            self.close_connection = True
            return
        upstream.sock.settimeout(600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP}
        started = time.monotonic()
        try:
            upstream.request(self.command, upstream_path, body=body or None, headers=headers)
            reply = upstream.getresponse()
        except (OSError, http.client.HTTPException) as e:
            print(f"\n[{stamp()}] !!! upstream error: {e}", flush=True)
            if capture:
                EVENTS.add(
                    "error",
                    {"message": str(e), "upstream": UPSTREAM, "path": redact_url(self.path)},
                    "Upstream request failed",
                    request_id=request_id,
                    metadata={"status": 502},
                )
            self.send_error(502, f"proxy: upstream request failed: {e}")
            self.close_connection = True
            upstream.close()
            return

        # 3. Stream the reply back as it arrives, printing it on the way.
        print(f"\n[{stamp()}] <<< {reply.status} {reply.reason}", flush=True)
        self.send_response(reply.status, reply.reason)
        for name, value in reply.getheaders():
            if name.lower() not in SKIP and name.lower() != "content-encoding":
                self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            if capture:
                response_headers = dict(reply.getheaders())
                response_id = response_headers.get("request-id") or response_headers.get("x-request-id") or request_id
                EVENTS.add(
                    "model_response",
                    {"status": reply.status, "reason": reply.reason, "headers": redact(response_headers), "body": None},
                    f"{reply.status} {reply.reason}",
                    request_id=response_id,
                    metadata={"status": reply.status, "durationMs": round((time.monotonic() - started) * 1000)},
                )
            self.close_connection = True
            return
        captured = bytearray()
        capture_truncated = False
        while chunk := reply.read1(4096):  # read1: pass on each chunk as it lands, don't wait for 4096 bytes
            self.wfile.write(chunk)
            self.wfile.flush()
            sys.stdout.write(chunk.decode("utf-8", "replace"))
            sys.stdout.flush()
            if capture:
                remaining = MAX_CAPTURE_BYTES - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    capture_truncated = True
        self.close_connection = True
        print(f"\n[{stamp()}] --- done", flush=True)
        if not capture:
            return
        response_headers = dict(reply.getheaders())
        response_id = response_headers.get("request-id") or response_headers.get("x-request-id") or request_id
        response_body = parse_response_body(
            bytes(captured), response_headers.get("content-type", ""), capture_truncated
        )
        response_data = {
            "status": reply.status,
            "reason": reply.reason,
            "headers": redact(response_headers),
            "body": response_body,
        }
        metadata = {
            "status": reply.status,
            "durationMs": round((time.monotonic() - started) * 1000),
        }
        if isinstance(response_body, dict):
            usage = response_body.get("usage")
            if usage is not None:
                metadata["usage"] = usage
        EVENTS.add(
            "model_response",
            response_data,
            f"{reply.status} {reply.reason}",
            request_id=response_id,
            metadata=metadata,
        )
        for call in tool_calls(response_body):
            function = call.get("function")
            name = call.get("name") or (function.get("name") if isinstance(function, dict) else None)
            call_id = call.get("id") or call.get("call_id")
            EVENTS.add(
                "tool_call",
                call,
                f"Tool call{f' · {name}' if name else ''}",
                request_id=response_id,
                metadata={"callId": call_id} if call_id else None,
            )


def main() -> None:
    global UPSTREAM
    parser = argparse.ArgumentParser(description="Print and forward an agent's API traffic.")
    parser.add_argument("--port", type=int, default=8041)
    parser.add_argument("--upstream", default=UPSTREAM, help="where to forward (default: the Anthropic API)")
    args = parser.parse_args()
    UPSTREAM = args.upstream.rstrip("/")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Tap)
    print(f"proxy listening on http://localhost:{args.port}  ->  {UPSTREAM}")
    print(f"dashboard: http://localhost:{args.port}{MONITOR_PREFIX}/")
    print(f"run the agent with:  ANTHROPIC_BASE_URL=http://localhost:{args.port} claude")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
