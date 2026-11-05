# Agent Traffic Monitor

A local, standard-library reverse proxy with a browser dashboard for inspecting agent/model traffic. Captures exist only in bounded process memory and disappear when the proxy stops.

## Setup

Requirements: Python 3.10 or newer. There are no packages to install.

From this directory, start the proxy:

```bash
python3 proxy.py
```

The defaults are:

- proxy: `http://localhost:8041`
- dashboard: `http://localhost:8041/_monitor/`
- upstream: `https://api.anthropic.com`

Point Claude Code at it in another terminal:

```bash
ANTHROPIC_BASE_URL=http://localhost:8041 claude
```

For another local port or upstream, use the proxy's existing options. For a Codex Responses provider backed by the OpenAI API, include the API's `/v1` base path:

```bash
python3 proxy.py --port 9000 --upstream https://api.openai.com/v1
```

Then open `http://localhost:9000/_monitor/` and configure the Codex Responses provider to use `http://localhost:9000` as its base URL. Codex appends `/responses`, which the proxy forwards to `https://api.openai.com/v1/responses`. Keep the provider's `wire_api` set to `responses` and disable WebSockets so traffic uses this HTTP proxy.

For an API-key-backed Codex CLI session, one complete example is:

```bash
codex \
  -c 'model_provider="local_monitor"' \
  -c 'model_providers.local_monitor.name="Local monitor"' \
  -c 'model_providers.local_monitor.base_url="http://localhost:9000"' \
  -c 'model_providers.local_monitor.env_key="OPENAI_API_KEY"' \
  -c 'model_providers.local_monitor.wire_api="responses"' \
  -c 'model_providers.local_monitor.requires_openai_auth=false' \
  -c 'model_providers.local_monitor.supports_websockets=false'
```

Set `OPENAI_API_KEY` in the environment before running that command. Do not put the key in the command or configuration file.

The proxy prints its effective upstream at startup and the effective upstream path for every relayed request. Check both lines during troubleshooting. `/live` is not a local monitor route; `POST /live` is forwarded to `<upstream>/live`, and any 404 is preserved. The first live test's launch command was not retained, so its exact effective upstream cannot be reconstructed from this repository.

## What was integrated

The supplied proxy is a `ThreadingHTTPServer` reverse proxy. All ordinary GET, POST, and HEAD requests enter `Tap.relay()`. Request bodies are bytes and are normally JSON for model calls. Responses arrive from `http.client` and are copied to the agent chunk by chunk; they can be ordinary JSON or `text/event-stream`. Tool calls and tool results are not separate proxy operations: when present, Anthropic-style `tool_use` blocks are embedded in response content and `tool_result` blocks are embedded in later request messages.

Monitoring observes request bytes just before the existing upstream connection and copies at most 256 KB of response bytes while the unchanged bytes are streamed to the client. Each stored event is also capped at 256 KB after redaction. The dashboard adds a small reserved local namespace:

- `GET /_monitor/` — dashboard page
- `GET /_monitor/events` — current in-memory buffer
- `DELETE /_monitor/events` — clear the buffer

The dashboard polls the events endpoint every two seconds. It reloads the current buffer on every poll and de-duplicates by monitor event ID. No SSE route is claimed or used.

The ring buffer holds the newest 200 events. Stored request/response data is redacted recursively for authorization, API-key, cookie, token, and proxy-credential fields. Common Bearer/Basic and `sk-...` values in free text are also redacted. The monitor binds with the proxy to `127.0.0.1` and does not use files, a database, analytics, `localStorage`, or `sessionStorage`.

## Dashboard behavior

The timeline is newest first and includes model requests/responses plus extracted tool calls/results. Matching tool call/result IDs receive a paired indicator. Expand any event for a structured view and the complete redacted raw captured item. Type filters and case-insensitive search combine; search covers summaries, request IDs, prompts, tool names, and serialized content. The status is `Polling` after successful updates and `Disconnected` after a failed update, with automatic retry and a manual retry button. Successful recovery clears the prior error; stale overlapping poll results are ignored.

Browser-generated asset requests such as `/favicon.ico` are still forwarded normally but are excluded from model-event capture.

## Tests

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
node --check dashboard/app.js
```

## Limitations

- Captures are process-local and intentionally disappear on restart.
- Each event is capped at 256 KB; larger responses continue proxying normally but the dashboard marks the captured body as truncated.
- Provider-specific extraction is intentionally small. The raw redacted request/response remains available even if a provider uses an unfamiliar shape.
- Streaming response events appear after the upstream response completes; request events appear before forwarding.
- The proxy prints traffic to its terminal as it did originally. Header credentials there remain shortened rather than fully removed, so treat the terminal as sensitive too.
