import contextlib
import http.client
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import proxy
from monitor import EventStore


class UpstreamHandler(BaseHTTPRequestHandler):
    seen = {}
    response = b""

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        type(self).seen = {"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body}
        if self.path == "/live":
            response = b"upstream route not found"
            type(self).response = response
            self.send_response(404)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        response = json.dumps({
            "id": "msg_1",
            "content": [{"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "README.md"}}],
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }).encode()
        type(self).response = response
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("request-id", "req_upstream")
        self.send_header("content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):
        type(self).seen = {"method": "GET", "path": self.path, "headers": dict(self.headers), "body": b""}
        response = b"asset not found"
        type(self).response = response
        self.send_response(404)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class ProxyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        proxy.UPSTREAM = f"http://127.0.0.1:{cls.upstream.server_port}"
        proxy.EVENTS = EventStore()
        cls.monitor = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Tap)
        cls.monitor_thread = threading.Thread(target=cls.monitor.serve_forever, daemon=True)
        cls.monitor_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.monitor.shutdown()
        cls.monitor.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.monitor.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def setUp(self):
        proxy.EVENTS.clear()
        UpstreamHandler.seen = {}

    def test_proxy_preserves_model_traffic_and_captures_embedded_tools(self):
        request_body = json.dumps({
            "model": "fixture-model",
            "system": "Be useful",
            "messages": [{
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "previous", "content": "done"}],
            }],
            "tools": [{"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}],
        }).encode()
        with contextlib.redirect_stdout(io.StringIO()):
            status, _, response_body = self.request(
                "POST", "/v1/messages", request_body,
                {"content-type": "application/json", "authorization": "Bearer top-secret"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(UpstreamHandler.seen["body"], request_body)
        self.assertEqual(UpstreamHandler.seen["headers"]["authorization"], "Bearer top-secret")
        self.assertEqual(response_body, UpstreamHandler.response)

        events = proxy.EVENTS.snapshot()
        self.assertEqual([event["type"] for event in events], [
            "model_request", "tool_result", "model_response", "tool_call",
        ])
        self.assertEqual(events[0]["data"]["headers"]["authorization"], "[REDACTED]")
        self.assertEqual(events[0]["data"]["body"]["system"], "Be useful")
        self.assertEqual(events[0]["data"]["upstreamPath"], "/v1/messages")
        self.assertEqual(events[1]["metadata"]["callId"], "previous")
        self.assertEqual(events[2]["requestId"], "req_upstream")
        self.assertEqual(events[3]["data"]["name"], "read_file")

    def test_live_is_not_a_local_route_and_preserves_upstream_404(self):
        with contextlib.redirect_stdout(io.StringIO()):
            status, _, body = self.request("POST", "/live", b"{}", {"content-type": "application/json"})
        self.assertEqual(status, 404)
        self.assertEqual(body, b"upstream route not found")
        self.assertEqual(UpstreamHandler.seen["path"], "/live")
        events = proxy.EVENTS.snapshot()
        self.assertEqual([event["type"] for event in events], ["model_request", "model_response"])
        self.assertEqual(events[0]["data"]["upstreamPath"], "/live")
        self.assertEqual(events[1]["metadata"]["status"], 404)

    def test_browser_asset_is_forwarded_without_model_events(self):
        with contextlib.redirect_stdout(io.StringIO()):
            status, _, body = self.request("GET", "/favicon.ico")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"asset not found")
        self.assertEqual(UpstreamHandler.seen["path"], "/favicon.ico")
        self.assertEqual(proxy.EVENTS.snapshot(), [])

    def test_dashboard_api_and_clear(self):
        status, headers, page = self.request("GET", "/_monitor/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Agent Traffic Monitor", page)

        status, _, body = self.request("GET", "/_monitor/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body)["events"], list)

        status, _, body = self.request("DELETE", "/_monitor/events")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "events": []})
        self.assertEqual(proxy.EVENTS.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
