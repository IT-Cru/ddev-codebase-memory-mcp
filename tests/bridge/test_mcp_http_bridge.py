#!/usr/bin/env python3
"""Tests for codebase-memory-build/mcp-http-bridge.py.

Run from the repository root:

    python3 -m unittest discover -s tests/bridge -v

No Docker, no ddev, no third-party packages — standard library only, matching the
bridge itself. That is deliberate: these run in seconds on every push, whereas
tests/test.bats needs a full DDEV environment.

The bridge is exercised as a black box: started as a subprocess in front of
mock_stdio_server.py and driven over real HTTP. That covers the transport as a
client actually meets it, rather than testing internals that could drift from
observable behaviour.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
BRIDGE = os.path.join(REPO_ROOT, "codebase-memory-build", "mcp-http-bridge.py")
MOCK = os.path.join(HERE, "mock_stdio_server.py")

INIT_BODY = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "unittest", "version": "1"}},
}


def free_ports(count=1):
    """Distinct free ports. Bind them all before closing any, so two calls in a
    row cannot hand back the same number."""
    socks = []
    try:
        for _ in range(count):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def free_port():
    return free_ports(1)[0]


class Bridge:
    """A bridge subprocess wrapping the mock stdio server."""

    def __init__(self, **env_overrides):
        self.port, self.ui_proxy_port, self.ui_target_port = free_ports(3)
        env = dict(os.environ)
        env["CBM_BRIDGE_COMMAND"] = "%s %s" % (sys.executable, MOCK)
        env["CBM_BRIDGE_PORT"] = str(self.port)
        env["CBM_UI_PROXY_PORT"] = str(self.ui_proxy_port)
        # Point the UI target at a free port rather than letting it default to
        # 9749. On a developer machine that is where a natively installed
        # codebase-memory-mcp serves its own UI, and the bridge under test would
        # happily proxy to it — making results depend on what is running outside
        # the test, and differ from CI where nothing is listening there.
        env["CBM_UI_PORT"] = str(self.ui_target_port)
        # Most classes have no UI behind the proxy, so the keeper would wait out its
        # full startup window on every proxied request. Keep that short by default;
        # the keeper's own behaviour is covered explicitly.
        env.setdefault("CBM_UI_KEEPER_WAIT", "2")
        env.update({k: str(v) for k, v in env_overrides.items()})
        self.proc = subprocess.Popen(
            [sys.executable, BRIDGE], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.base = "http://127.0.0.1:%d" % self.port
        if not self._wait_ready():
            self.stop()
            raise RuntimeError("bridge did not become ready on port %d" % self.port)

    def _wait_ready(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                status, _, _ = self.call("GET", path="/health", timeout=2)
                if status == 200:
                    return True
            except Exception:
                time.sleep(0.2)
        return False

    def call(self, method="POST", body=None, session=None, path="/mcp",
             timeout=60, headers=None, raw_body=None):
        """Returns (status, headers, parsed_json_or_None)."""
        data = raw_body if raw_body is not None else (
            json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if session:
            req.add_header("Mcp-Session-Id", session)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        def decode(text):
            """Parsed JSON when it is JSON, the raw string otherwise (the UI proxy
            returns HTML), None when empty."""
            if not text:
                return None
            try:
                return json.loads(text)
            except ValueError:
                return text

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), decode(resp.read().decode())
        except urllib.error.HTTPError as exc:
            with exc:  # closing it keeps ResourceWarnings out of the test output
                return exc.code, dict(exc.headers), decode(exc.read().decode())

    def open_session(self):
        """Initialize and return (session_id, result)."""
        status, headers, body = self.call("POST", INIT_BODY)
        assert status == 200, "initialize returned %s" % status
        return headers.get("Mcp-Session-Id"), body

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        # Close the pipes explicitly; leaving them to the garbage collector emits
        # ResourceWarnings that bury real output.
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass


class BridgeTestCase(unittest.TestCase):
    """Shares one bridge across the class; override ENV for different settings."""

    ENV = {}

    @classmethod
    def setUpClass(cls):
        cls.bridge = Bridge(**cls.ENV)

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()


class TestProtocolBasics(BridgeTestCase):
    def test_health_endpoint(self):
        status, _, body = self.bridge.call("GET", path="/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("sessions", body)

    def test_initialize_returns_session_id_and_result(self):
        status, headers, body = self.bridge.call("POST", INIT_BODY)
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Mcp-Session-Id"))
        self.assertEqual(body["result"]["serverInfo"]["name"], "mock-stdio-server")

    def test_request_without_session_is_rejected(self):
        status, _, _ = self.bridge.call(
            "POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(status, 400)

    def test_unknown_session_is_rejected(self):
        status, _, body = self.bridge.call(
            "POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            session="deadbeef")
        # 404 tells the client to start over with a fresh initialize.
        self.assertEqual(status, 404)
        self.assertIn("re-initialize", json.dumps(body))

    def test_request_is_routed_to_its_own_session(self):
        session, init = self.bridge.open_session()
        pid = init["result"]["pid"]
        status, _, body = self.bridge.call(
            "POST", {"jsonrpc": "2.0", "id": 7, "method": "ping",
                     "params": {"marker": "abc"}}, session=session)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], 7)
        self.assertEqual(body["result"]["echo"]["marker"], "abc")
        # Same session keeps the same child process.
        self.assertEqual(body["result"]["pid"], pid)

    def test_notification_gets_202_and_no_body(self):
        session, _ = self.bridge.open_session()
        status, _, body = self.bridge.call(
            "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session=session)
        self.assertEqual(status, 202)
        self.assertIsNone(body)

    def test_get_on_mcp_path_is_refused(self):
        # No server-initiated messages, so no SSE stream. 405 is a legal answer
        # and clients fall back to plain POST responses.
        status, headers, _ = self.bridge.call("GET")
        self.assertEqual(status, 405)
        self.assertIn("POST", headers.get("Allow", ""))

    def test_malformed_json_is_rejected(self):
        status, _, _ = self.bridge.call("POST", raw_body=b"{not json")
        self.assertEqual(status, 400)

    def test_empty_body_is_rejected(self):
        status, _, _ = self.bridge.call("POST", raw_body=b"")
        self.assertEqual(status, 400)

    def test_batch_returns_array_of_responses(self):
        session, _ = self.bridge.open_session()
        status, _, body = self.bridge.call("POST", [
            {"jsonrpc": "2.0", "id": 11, "method": "a"},
            {"jsonrpc": "2.0", "id": 12, "method": "b"},
        ], session=session)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertEqual([r["id"] for r in body], [11, 12])

    def test_non_mcp_paths_go_to_the_ui_proxy(self):
        # Everything that is not /mcp or /health belongs to the graph UI, which is
        # a SPA and owns its own routing. With no UI running the proxy explains
        # itself instead of returning a bare 404.
        status, headers, body = self.bridge.call("GET", path="/nope")
        self.assertEqual(status, 503)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("Graph UI is not running", body)


class TestSessionIsolation(BridgeTestCase):
    """The transport's load-bearing property.

    A stdio pipe carries one interleaved JSON-RPC stream plus per-session
    initialize state, so a shared child process would let two agents corrupt each
    other's traffic.
    """

    def test_each_session_gets_a_separate_process(self):
        s1, init1 = self.bridge.open_session()
        s2, init2 = self.bridge.open_session()
        self.assertNotEqual(s1, s2)
        self.assertNotEqual(init1["result"]["pid"], init2["result"]["pid"])

    def test_concurrent_sessions_do_not_cross_talk(self):
        s1, init1 = self.bridge.open_session()
        s2, init2 = self.bridge.open_session()
        pid1, pid2 = init1["result"]["pid"], init2["result"]["pid"]
        seen = {"a": [], "b": []}

        def hammer(session, tag, base_id, sink):
            for i in range(8):
                _, _, body = self.bridge.call(
                    "POST", {"jsonrpc": "2.0", "id": base_id + i, "method": "ping",
                             "params": {"tag": tag}}, session=session)
                sink.append((body["id"], body["result"]["echo"]["tag"],
                             body["result"]["pid"]))

        t1 = threading.Thread(target=hammer, args=(s1, "a", 1000, seen["a"]))
        t2 = threading.Thread(target=hammer, args=(s2, "b", 2000, seen["b"]))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual([r[0] for r in seen["a"]], list(range(1000, 1008)))
        self.assertEqual([r[0] for r in seen["b"]], list(range(2000, 2008)))
        self.assertEqual({r[1] for r in seen["a"]}, {"a"})
        self.assertEqual({r[1] for r in seen["b"]}, {"b"})
        self.assertEqual({r[2] for r in seen["a"]}, {pid1})
        self.assertEqual({r[2] for r in seen["b"]}, {pid2})


class TestTeardown(BridgeTestCase):
    def _alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def test_delete_ends_session_and_reaps_child(self):
        session, init = self.bridge.open_session()
        pid = init["result"]["pid"]
        status, _, _ = self.bridge.call("DELETE", session=session)
        self.assertEqual(status, 204)

        status, _, _ = self.bridge.call(
            "POST", {"jsonrpc": "2.0", "id": 3, "method": "ping"}, session=session)
        self.assertEqual(status, 404)

        deadline = time.monotonic() + 15
        while self._alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(self._alive(pid), "child process %s was not reaped" % pid)

    def test_delete_of_unknown_session_is_404(self):
        status, _, _ = self.bridge.call("DELETE", session="nosuchsession")
        self.assertEqual(status, 404)

    def test_delete_is_not_blocked_by_an_in_flight_request(self):
        """Regression: session teardown used to queue behind a running call.

        send() held the session lock for the whole response wait and close()
        wanted the same lock, so DELETE — and the idle reaper, which stalled for
        every other session too — waited for the in-flight request to finish.
        """
        session, _ = self.bridge.open_session()
        slow_seconds = 6.0
        outcome = {}

        def slow_call():
            started = time.monotonic()
            status, _, _ = self.bridge.call(
                "POST", {"jsonrpc": "2.0", "id": 21, "method": "slow",
                         "params": {"seconds": slow_seconds}}, session=session)
            outcome["elapsed"] = time.monotonic() - started
            outcome["status"] = status

        worker = threading.Thread(target=slow_call)
        worker.start()
        time.sleep(1.0)  # let the slow call take the I/O lock

        started = time.monotonic()
        status, _, _ = self.bridge.call("DELETE", session=session, timeout=30)
        elapsed = time.monotonic() - started
        worker.join()

        self.assertEqual(status, 204)
        self.assertLess(elapsed, 2.0,
                        "DELETE waited %.2fs for the in-flight request" % elapsed)


class TestAuthToken(BridgeTestCase):
    ENV = {"CBM_BRIDGE_TOKEN": "s3cret"}

    def test_request_without_token_is_unauthorized(self):
        status, _, _ = self.bridge.call("POST", INIT_BODY)
        self.assertEqual(status, 401)

    def test_request_with_wrong_token_is_unauthorized(self):
        status, _, _ = self.bridge.call(
            "POST", INIT_BODY, headers={"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_request_with_token_is_accepted(self):
        status, headers, _ = self.bridge.call(
            "POST", INIT_BODY, headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Mcp-Session-Id"))

    def test_health_stays_open_for_the_container_healthcheck(self):
        status, _, _ = self.bridge.call("GET", path="/health")
        self.assertEqual(status, 200)


class TestRequestTimeout(BridgeTestCase):
    ENV = {"CBM_BRIDGE_REQUEST_TIMEOUT": "2"}

    def test_slow_request_times_out_with_504(self):
        session, _ = self.bridge.open_session()
        status, _, body = self.bridge.call(
            "POST", {"jsonrpc": "2.0", "id": 31, "method": "slow",
                     "params": {"seconds": 10}}, session=session, timeout=30)
        self.assertEqual(status, 504)
        self.assertEqual(body["error"]["code"], -32603)

    def test_timeout_error_names_the_request_that_failed(self):
        """Regression: the error id came from batch[0], not the failing message."""
        session, _ = self.bridge.open_session()
        status, _, body = self.bridge.call("POST", [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 42, "method": "slow", "params": {"seconds": 10}},
        ], session=session, timeout=30)
        self.assertEqual(status, 504)
        self.assertEqual(body["id"], 42)


class FakeUI:
    """Stands in for the graph UI: records the Host header it was given."""

    def __init__(self):
        self.port = free_port()
        self.seen_hosts = []
        self.seen_origins = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _respond(self):
                outer.seen_hosts.append(self.headers.get("Host"))
                outer.seen_origins.append(self.headers.get("Origin"))
                # Mimic the real UI: reject any Host that is not loopback, and any
                # Origin that is not this server's own. Both are DNS-rebinding
                # defences, and the Origin one only fires for browser requests —
                # curl sends no Origin at all.
                host = (self.headers.get("Host") or "").split(":")[0]
                origin = self.headers.get("Origin")
                if origin is not None and origin != "http://%s" % (self.headers.get("Host") or ""):
                    body = b"forbidden origin"
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    return
                if host not in ("127.0.0.1", "localhost"):
                    body = b"forbidden"
                    self.send_response(403)
                else:
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    sent = self.rfile.read(length) if length else b""
                    body = json.dumps({
                        "path": self.path, "method": self.command,
                        "body": sent.decode() or None,
                    }).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Upstream", "fake-ui")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = _respond
            do_POST = _respond
            do_HEAD = _respond

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class TestGraphUIProxy(unittest.TestCase):
    """The UI listens on loopback only and rejects non-localhost Host headers, so
    the bridge has to proxy it *and* rewrite Host for a *.ddev.site URL to work."""

    @classmethod
    def setUpClass(cls):
        cls.ui = FakeUI()
        cls.bridge = Bridge(CBM_UI_PORT=cls.ui.port)

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()
        cls.ui.stop()

    def _get(self, path="/", method="GET", body=None, host_header=None,
             origin=None):
        url = "http://127.0.0.1:%d%s" % (self.bridge.ui_proxy_port, path)
        data = body.encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        if host_header:
            req.add_header("Host", host_header)
        # Browsers send this for the crossorigin-tagged assets; default it on so
        # the proxy is exercised the way a browser exercises it.
        req.add_header("Origin", origin or "http://127.0.0.1:%d" % self.bridge.ui_proxy_port)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, dict(exc.headers), exc.read().decode()

    def test_proxies_and_rewrites_host_to_loopback(self):
        status, headers, body = self._get("/", host_header="uitest.ddev.site:9761")
        self.assertEqual(status, 200, body)
        # The upstream saw loopback, not the ddev.site name it would have refused.
        self.assertTrue(self.ui.seen_hosts)
        self.assertTrue(self.ui.seen_hosts[-1].startswith("127.0.0.1"),
                        "upstream saw Host=%r" % self.ui.seen_hosts[-1])
        self.assertEqual(headers.get("X-Upstream"), "fake-ui")

    def test_rewrites_origin_as_well_as_host(self):
        """Regression: the UI rejects a foreign Origin with 403, for /assets/* and
        /api/* alike. Browsers always send one (the asset tags are `crossorigin`);
        curl sends none — so forwarding it untouched passed every command-line check
        and rendered a blank page in a browser."""
        status, _, body = self._get(
            "/assets/app.js", host_header="proj.ddev.site:9761")
        self.assertEqual(status, 200, "foreign Origin was forwarded: %s" % (body,))
        self.assertTrue(self.ui.seen_origins)
        self.assertEqual(self.ui.seen_origins[-1],
                         "http://127.0.0.1:%d" % self.ui.port)

    def test_preserves_path_and_query(self):
        status, _, body = self._get("/api/processes?a=1&b=2")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["path"], "/api/processes?a=1&b=2")

    def test_forwards_post_body(self):
        status, _, body = self._get("/api/index", method="POST", body='{"x":1}')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["method"], "POST")
        self.assertEqual(payload["body"], '{"x":1}')

    def test_serves_explanatory_503_when_ui_is_down(self):
        """With no MCP session there is no daemon and no UI; say so in the page."""
        self.ui.stop()
        try:
            status, headers, body = self._get("/")
            self.assertEqual(status, 503)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("not running", body)
            self.assertIn("ddev claude-code", body)
        finally:
            # Later tests in this class must not depend on ordering.
            type(self).ui = FakeUI()


class TestUIKeeper(unittest.TestCase):
    """The UI must be reachable with no agent attached.

    CBM's UI belongs to a daemon that exits with the last MCP session, so without a
    keeper the graph is only viewable while an agent happens to be running — which
    is the whole obstacle to using this add-on standalone.
    """

    def test_keeper_opens_a_session_when_the_ui_is_absent(self):
        bridge = Bridge(CBM_UI_KEEPER_WAIT="2")
        try:
            # Nothing is holding the daemon up yet.
            _, _, health = bridge.call("GET", path="/health")
            self.assertFalse(health["ui_keeper"])

            # A UI request should make the bridge try to bring one up. The mock
            # server never serves a UI, so the request still fails — but the
            # attempt itself is the behaviour under test.
            bridge.call("GET", path="/", timeout=30)

            _, _, health = bridge.call("GET", path="/health")
            self.assertTrue(health["ui_keeper"],
                            "no keeper session was started for the UI request")
        finally:
            bridge.stop()

    def test_keeper_can_be_disabled(self):
        bridge = Bridge(CBM_UI_KEEPER="false")
        try:
            status, _, _ = bridge.call("GET", path="/", timeout=30)
            self.assertEqual(status, 503)
            _, _, health = bridge.call("GET", path="/health")
            self.assertFalse(health["ui_keeper"])
        finally:
            bridge.stop()

    def test_no_keeper_when_the_ui_is_already_up(self):
        """An agent session already holds the daemon, so nothing extra is needed."""
        ui = FakeUI()
        bridge = Bridge(CBM_UI_PORT=ui.port)
        try:
            status, _, _ = bridge.call("GET", path="/")
            self.assertEqual(status, 200)
            _, _, health = bridge.call("GET", path="/health")
            self.assertFalse(health["ui_keeper"],
                             "started a keeper although the UI was already serving")
        finally:
            bridge.stop()
            ui.stop()


class TestSinglePortSurface(BridgeTestCase):
    """MCP and the UI share one port, so the routed URL serves both."""

    def test_mcp_is_served_on_the_ui_port(self):
        url = "http://127.0.0.1:%d/mcp" % self.bridge.ui_proxy_port
        req = urllib.request.Request(url, data=json.dumps(INIT_BODY).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.headers.get("Mcp-Session-Id"))
        self.assertEqual(body["result"]["serverInfo"]["name"], "mock-stdio-server")

    def test_health_is_served_on_the_ui_port(self):
        url = "http://127.0.0.1:%d/health" % self.bridge.ui_proxy_port
        with urllib.request.urlopen(url, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read().decode())["status"], "ok")


class TestStartupValidation(unittest.TestCase):
    def test_empty_command_exits_nonzero(self):
        """Regression: an empty command split to [] and broke every session."""
        env = dict(os.environ)
        env["CBM_BRIDGE_COMMAND"] = "   "
        env["CBM_BRIDGE_PORT"] = str(free_port())
        proc = subprocess.run([sys.executable, BRIDGE], env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CBM_BRIDGE_COMMAND is empty", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
