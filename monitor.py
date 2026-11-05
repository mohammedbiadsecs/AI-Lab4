"""In-memory capture and redaction helpers for the local traffic monitor."""

from __future__ import annotations

import itertools
import json
import re
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_EVENTS = 200
MAX_CAPTURE_BYTES = 256_000
REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "apikey",
    "api_key",
    "access-token",
    "access_token",
    "refresh-token",
    "refresh_token",
    "client-secret",
    "client_secret",
    "proxy-credentials",
    "proxy_credentials",
}
_TOKEN_VALUE = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;]+")
_API_KEY_VALUE = re.compile(r"\b(sk-(?:proj-)?[A-Za-z0-9_-]{8,})\b")


def _is_sensitive(key: object) -> bool:
    normalized = str(key).strip().lower().replace("_", "-")
    return normalized in _SENSITIVE_KEYS or normalized.endswith("-api-key")


def redact_text(value: str) -> str:
    """Redact common credential shapes when they occur in free-form text."""
    value = _TOKEN_VALUE.sub(lambda match: f"{match.group(1)} {REDACTED}", value)
    return _API_KEY_VALUE.sub(REDACTED, value)


def redact(value: Any, key: object | None = None) -> Any:
    """Return a JSON-safe, recursively redacted copy of *value*."""
    if key is not None and _is_sensitive(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", "replace"))
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(repr(value))


def redact_url(path: str) -> str:
    """Redact credentials in URL query parameters without changing the route."""
    try:
        parts = urlsplit(path)
        query = [
            (name, REDACTED if _is_sensitive(name) else redact_text(value))
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except ValueError:
        return redact_text(path)


def bounded(value: Any) -> Any:
    """Redact and cap one stored value so the ring buffer has a byte bound."""
    clean = redact(value)
    try:
        serialized = json.dumps(clean, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = repr(clean)
    encoded = serialized.encode("utf-8")
    if len(encoded) <= MAX_CAPTURE_BYTES:
        return clean
    clipped = encoded[:MAX_CAPTURE_BYTES].decode("utf-8", "ignore")
    return {"text": clipped, "truncated": True, "unparsed": True}


def parse_body(raw: bytes, content_type: str = "") -> Any:
    """Keep JSON structured; otherwise retain safe, inspectable text."""
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"text": redact_text(text), "unparsed": True}
    return redact(parsed)


def parse_response_body(raw: bytes, content_type: str = "", truncated: bool = False) -> Any:
    """Parse JSON or an SSE response while preserving unrecognized stream lines."""
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" not in content_type.lower():
        result = parse_body(raw, content_type)
        if truncated and isinstance(result, dict):
            result["truncated"] = True
        return result

    events: list[Any] = []
    for block in re.split(r"\r?\n\r?\n", text):
        if not block.strip():
            continue
        event_name = None
        data_lines: list[str] = []
        other_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line.startswith(":"):
                other_lines.append(line)
        data_text = "\n".join(data_lines)
        if data_text:
            try:
                data: Any = redact(json.loads(data_text))
            except (json.JSONDecodeError, ValueError):
                data = {"text": redact_text(data_text), "unparsed": True}
        else:
            data = None
        item: dict[str, Any] = {}
        if event_name:
            item["event"] = event_name
        if data is not None:
            item["data"] = data
        if other_lines:
            item["unparsedLines"] = redact(other_lines)
        events.append(item)
    result = {"stream": True, "events": events}
    if truncated:
        result["truncated"] = True
    return result


def _walk_objects(value: Any) -> Iterable[dict[str, Any]]:
    """Yield nested JSON objects without assuming a provider envelope."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_objects(child)


def tool_results(value: Any) -> Iterable[dict[str, Any]]:
    for block in _walk_objects(value):
        if block.get("type") in {"tool_result", "function_call_output"}:
            yield block


def tool_calls(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict) and value.get("stream") and isinstance(value.get("events"), list):
        active: dict[object, dict[str, Any]] = {}
        partial_inputs: dict[object, list[str]] = {}
        for event in value["events"]:
            data = event.get("data") if isinstance(event, dict) else None
            if not isinstance(data, dict):
                continue
            index = data.get("index")
            block = data.get("content_block")
            if data.get("type") == "content_block_start" and isinstance(block, dict) and block.get("type") == "tool_use":
                active[index] = dict(block)
                partial_inputs[index] = []
            delta = data.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "input_json_delta" and index in active:
                partial_inputs[index].append(str(delta.get("partial_json", "")))
            if data.get("type") == "content_block_stop" and index in active:
                call = active.pop(index)
                pieces = "".join(partial_inputs.pop(index, []))
                if pieces:
                    try:
                        call["input"] = redact(json.loads(pieces))
                    except (json.JSONDecodeError, ValueError):
                        call["input"] = {"text": redact_text(pieces), "unparsed": True}
                yield call
        for index, call in active.items():
            pieces = "".join(partial_inputs.get(index, []))
            if pieces:
                call["input"] = {"text": redact_text(pieces), "unparsed": True}
            yield call

    for block in _walk_objects(value):
        block_type = block.get("type")
        if block_type == "function_call" or (
            block_type == "function" and isinstance(block.get("function"), dict)
        ):
            yield block
        elif block_type == "tool_use" and not (isinstance(value, dict) and value.get("stream")):
            yield block


class EventStore:
    """Thread-safe, process-local ring buffer."""

    def __init__(self, limit: int = MAX_EVENTS) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=limit)
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def add(
        self,
        event_type: str,
        data: Any,
        summary: str,
        *,
        request_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "id": str(next(self._ids)),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "data": bounded(data),
            "summary": redact_text(summary)[:500],
        }
        if request_id:
            event["requestId"] = redact_text(request_id)[:500]
        clean_metadata = bounded(metadata or {})
        if clean_metadata:
            event["metadata"] = clean_metadata
        with self._lock:
            self._events.append(event)
        return event

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

