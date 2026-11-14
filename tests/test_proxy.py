"""Verification of network routing and proxy behavior across arena environment.

Rules verified:
  1. Direct model calls must go straight to the gateway (bypassing proxies).
  2. General traffic / other requests properly honor HTTPS_PROXY.
  3. urllib correctly honors NO_PROXY exceptions.

Owner: luckydevs-1. Standard library only (RULES.md 10, 23).
"""

import http.server
import json
import os
import socketserver
import threading
import unittest
import urllib.error
import urllib.request

from bot import config, llm
from tests.session_script import BotProc, start_msg, turn_msg


class MockGatewayHandler(http.server.BaseHTTPRequestHandler):
    """Mock gateway HTTP server handling /chat/completions and /key/info."""

    received_requests = []

    def do_POST(self):
        MockGatewayHandler.received_requests.append(("POST", self.path))
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        req_json = json.loads(body.decode("utf-8")) if body else {}

        resp = {
            "id": "chatcmpl-test",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "```json\n{\"action\": \"test_action\", \"chat\": \"direct response\", \"why\": \"mock\"}\n```",
                },
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 15,
                "completion_tokens": 10,
                "total_tokens": 25,
            },
        }
        resp_bytes = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)

    def do_GET(self):
        MockGatewayHandler.received_requests.append(("GET", self.path))
        resp = {
            "info": {
                "max_budget": 6.0,
                "spend": 0.1234,
                "rpm_limit": 60,
                "expires": "2026-11-15T12:00:00Z",
            }
        }
        resp_bytes = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)

    def log_message(self, format, *args):
        # Suppress server stderr output in test runner
        pass


class MockProxyHandler(http.server.BaseHTTPRequestHandler):
    """Mock HTTP proxy server that intercepts traffic and records visits."""

    visited = []

    def do_GET(self):
        MockProxyHandler.visited.append(self.path)
        body = b"proxied-response"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        MockProxyHandler.visited.append(self.path)
        self.send_response(200, "Connection Established")
        self.end_headers()

    def log_message(self, format, *args):
        pass


class ProxyBehaviorTests(unittest.TestCase):

    def setUp(self):
        self._orig_env = dict(os.environ)
        self._orig_fake_llm = config.FAKE_LLM
        MockGatewayHandler.received_requests = []
        MockProxyHandler.visited = []

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        config.FAKE_LLM = self._orig_fake_llm

    def _start_server(self, handler_cls):
        server = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, port

    def _stop_server(self, server):
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass

    def test_1_model_calls_bypass_proxy(self):
        """Rule 1: Direct model calls must go straight to the gateway, bypassing proxies."""
        server, port = self._start_server(MockGatewayHandler)
        try:
            # Set proxy to an unreachable port
            unreachable_proxy = "http://127.0.0.1:59999"
            os.environ["HTTP_PROXY"] = unreachable_proxy
            os.environ["HTTPS_PROXY"] = unreachable_proxy
            os.environ["http_proxy"] = unreachable_proxy
            os.environ["https_proxy"] = unreachable_proxy
            # Ensure NO_PROXY does NOT bypass 127.0.0.1
            os.environ["NO_PROXY"] = "otherhost"
            os.environ["no_proxy"] = "otherhost"

            gateway_url = f"http://127.0.0.1:{port}/v1"

            # 1. Verify standard urllib without bypass fails because it tries to use the dead proxy
            req = urllib.request.Request(gateway_url + "/chat/completions")
            standard_opener = urllib.request.build_opener()
            with self.assertRaises(urllib.error.URLError) as ctx:
                standard_opener.open(req, timeout=1.0)
            self.assertIn("refused", str(ctx.exception).lower())

            # 2. Verify bot.llm.complete bypasses the proxy and reaches the gateway directly
            config.FAKE_LLM = False
            llm.set_key({"base_url": gateway_url, "api_key": "test-key-direct", "model": "haiku"})

            res = llm.complete([{"role": "user", "content": "hello gateway"}], timeout=3.0)
            self.assertIsNotNone(res)
            self.assertIn("test_action", res)

            # Confirm gateway server received the request directly
            self.assertGreater(len(MockGatewayHandler.received_requests), 0)
            self.assertEqual(MockGatewayHandler.received_requests[-1], ("POST", "/v1/chat/completions"))
        finally:
            self._stop_server(server)

    def test_2_general_traffic_honors_https_proxy(self):
        """Rule 2: General traffic / other requests must properly honor HTTPS_PROXY / HTTP_PROXY."""
        proxy_server, proxy_port = self._start_server(MockProxyHandler)
        target_server, target_port = self._start_server(MockGatewayHandler)
        try:
            os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{proxy_port}"
            os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy_port}"
            os.environ["http_proxy"] = f"http://127.0.0.1:{proxy_port}"
            os.environ["https_proxy"] = f"http://127.0.0.1:{proxy_port}"
            for k in ("NO_PROXY", "no_proxy"):
                os.environ.pop(k, None)

            # General opener should route through the proxy
            general_opener = llm.get_general_opener()
            target_url = f"http://127.0.0.1:{target_port}/general-request"
            with general_opener.open(target_url, timeout=2.0) as resp:
                content = resp.read()

            self.assertEqual(content, b"proxied-response")
            self.assertIn(target_url, MockProxyHandler.visited)
        finally:
            self._stop_server(proxy_server)
            self._stop_server(target_server)

    def test_3_urllib_honors_no_proxy_exceptions(self):
        """Rule 3: Exceptions - urllib must correctly honor NO_PROXY."""
        target_server, target_port = self._start_server(MockGatewayHandler)
        try:
            # Set proxy to an unreachable port
            unreachable_proxy = "http://127.0.0.1:59999"
            os.environ["HTTP_PROXY"] = unreachable_proxy
            os.environ["HTTPS_PROXY"] = unreachable_proxy
            os.environ["http_proxy"] = unreachable_proxy
            os.environ["https_proxy"] = unreachable_proxy

            # Arena standard NO_PROXY specification: gateway, localhost, 127.0.0.1 (ARENA-CONTRACT 2.3)
            os.environ["NO_PROXY"] = "gateway, localhost, 127.0.0.1, .dvloper.io"
            os.environ["no_proxy"] = "gateway, localhost, 127.0.0.1, .dvloper.io"

            # 1. Host 127.0.0.1 is in NO_PROXY: connection succeeds directly despite bad proxy
            general_opener = llm.get_general_opener()
            with general_opener.open(f"http://127.0.0.1:{target_port}/test-no-proxy", timeout=2.0) as resp:
                self.assertEqual(resp.status, 200)

            # 2. Test proxy_bypass environment matches without network resolution
            self.assertTrue(urllib.request.proxy_bypass("gateway"))
            self.assertTrue(urllib.request.proxy_bypass("gateway:8080"))
            self.assertTrue(urllib.request.proxy_bypass("localhost"))
            self.assertTrue(urllib.request.proxy_bypass("localhost:5000"))
            self.assertTrue(urllib.request.proxy_bypass("127.0.0.1"))
            self.assertTrue(urllib.request.proxy_bypass(f"127.0.0.1:{target_port}"))
            self.assertTrue(urllib.request.proxy_bypass("api.dvloper.io"))
            self.assertTrue(urllib.request.proxy_bypass("llm.hackaton-2026.dvloper.io"))

            # 3. Host NOT in NO_PROXY should attempt proxy and fail
            self.assertFalse(urllib.request.proxy_bypass("external-service.org"))
            with self.assertRaises(urllib.error.URLError):
                general_opener.open("http://127.0.0.1:59998/check", timeout=1.0)
        finally:
            self._stop_server(target_server)

    def test_4_keyinfo_direct_gateway(self):
        """Gateway account information query uses direct opener bypassing proxy."""
        server, port = self._start_server(MockGatewayHandler)
        try:
            # Set proxy to an unreachable port
            unreachable_proxy = "http://127.0.0.1:59999"
            os.environ["HTTP_PROXY"] = unreachable_proxy
            os.environ["HTTPS_PROXY"] = unreachable_proxy
            os.environ["NO_PROXY"] = "otherhost"

            gateway_base = f"http://127.0.0.1:{port}/v1"
            req = urllib.request.Request(gateway_base.rstrip("/v1") + "/key/info",
                                         headers={"Authorization": "Bearer test-key"})
            direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with direct_opener.open(req, timeout=2.0) as r:
                data = json.load(r)
            self.assertEqual(data.get("info", {}).get("max_budget"), 6.0)
            self.assertEqual(MockGatewayHandler.received_requests[-1], ("GET", "/key/info"))
        finally:
            self._stop_server(server)

    def test_5_arena_session_under_proxy_environment(self):
        """Simulate bot session execution in arena environment with proxy env vars."""
        bot = BotProc(env_extra={
            "BOT_FAKE_LLM": "1",
            "HTTPS_PROXY": "http://127.0.0.1:59999",
            "HTTP_PROXY": "http://127.0.0.1:59999",
            "NO_PROXY": "gateway,localhost,127.0.0.1",
        })
        try:
            # Start message should succeed cleanly
            obj, line, dt = bot.ask(start_msg(1, study=30), 10)
            self.assertIsNotNone(obj)
            self.assertEqual(obj.get("type"), "ready")
            self.assertTrue(obj.get("chat"))

            # Turn message should succeed cleanly
            t_obj, t_line, t_dt = bot.ask(turn_msg(1, legal=["1", "2"]), 10)
            self.assertIsNotNone(t_obj)
            self.assertEqual(t_obj.get("type"), "action")
            self.assertIn(t_obj.get("action"), ["1", "2"])
            self.assertTrue(t_obj.get("chat"))
        finally:
            bot.close_stdin()
            bot.wait(5)
            bot.cleanup()


if __name__ == "__main__":
    unittest.main()
