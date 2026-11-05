import json
import unittest

from monitor import EventStore, parse_response_body, redact, redact_url, tool_calls, tool_results


class MonitorTests(unittest.TestCase):
    def test_recursive_redaction(self):
        value = {
            "Authorization": "Bearer secret",
            "headers": {"x-api-key": "sk-verysecretvalue", "safe": "yes"},
            "cookie": "session=abc",
        }
        clean = redact(value)
        self.assertEqual(clean["Authorization"], "[REDACTED]")
        self.assertEqual(clean["headers"]["x-api-key"], "[REDACTED]")
        self.assertEqual(clean["cookie"], "[REDACTED]")
        self.assertEqual(clean["headers"]["safe"], "yes")
        self.assertEqual(redact_url("/v1/messages?api_key=secret&safe=yes"), "/v1/messages?api_key=%5BREDACTED%5D&safe=yes")

    def test_store_is_bounded_in_capture_order_and_clearable(self):
        store = EventStore(limit=3)
        for number in range(5):
            store.add("unknown", {"number": number}, str(number))
        self.assertEqual([item["data"]["number"] for item in store.snapshot()], [2, 3, 4])
        self.assertEqual(len({item["id"] for item in store.snapshot()}), 3)
        store.clear()
        self.assertEqual(store.snapshot(), [])

    def test_sse_and_tool_extraction(self):
        raw = (
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_1","name":"read","input":{}}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"x\\"}"}}\n\n'
            b'event: content_block_stop\n'
            b'data: {"type":"content_block_stop","index":0}\n\n'
        )
        parsed = parse_response_body(raw, "text/event-stream")
        calls = list(tool_calls(parsed))
        self.assertEqual(calls[0]["name"], "read")
        self.assertEqual(calls[0]["input"], {"path": "x"})

    def test_tool_results_in_request_messages(self):
        body = {
            "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]}]
        }
        self.assertEqual(list(tool_results(body))[0]["tool_use_id"], "call_1")


if __name__ == "__main__":
    unittest.main()
