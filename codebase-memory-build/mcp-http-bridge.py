#!/usr/bin/env python3
# ddev-generated
"""Streamable HTTP transport in front of a stdio MCP server.

codebase-memory-mcp speaks stdio only, so it cannot be registered with a URL.
This bridge accepts MCP Streamable HTTP on /mcp and gives every MCP session its
own child process — concurrent agents (Claude Code, OpenCode, several sessions of
each) must never share one stdin/stdout pair, because a stdio pipe carries a
single interleaved JSON-RPC stream and per-session `initialize` state.

Deliberately narrow: it implements the parts of the transport this server needs
and refuses the rest explicitly rather than half-supporting it.

  POST   /mcp      a JSON-RPC message (or batch); routed to the session's child
  DELETE /mcp      end the session and reap its child process
  GET    /mcp      405 — no server-initiated messages, so no SSE stream
  GET    /health   liveness for the container healthcheck

Only the Python standard library is used; there is no third-party code in the
path between an agent and your source.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHILD_COMMAND = os.environ.get("CBM_BRIDGE_COMMAND", "codebase-memory-mcp").split()
HOST = os.environ.get("CBM_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CBM_BRIDGE_PORT", "9750"))
MCP_PATH = os.environ.get("CBM_BRIDGE_PATH", "/mcp")
# Optional shared secret. Unset by default: the endpoint is only published on the
# project's Docker network, the same posture as other DDEV MCP add-ons.
TOKEN = os.environ.get("CBM_BRIDGE_TOKEN", "")
# A session holds a live child process, so idle ones are reaped. Indexing a large
# repository can occupy a single call for a long time, hence the generous
# per-request ceiling.
IDLE_TIMEOUT = float(os.environ.get("CBM_BRIDGE_IDLE_TIMEOUT", "1800"))
REQUEST_TIMEOUT = float(os.environ.get("CBM_BRIDGE_REQUEST_TIMEOUT", "900"))
MAX_BODY_BYTES = int(os.environ.get("CBM_BRIDGE_MAX_BODY", str(16 * 1024 * 1024)))

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INTERNAL_ERROR = -32603


def log(message):
    sys.stderr.write("[mcp-http-bridge] %s\n" % message)
    sys.stderr.flush()


class ChildExited(Exception):
    """The stdio server died; the session is no longer usable."""


class ChildTimeout(Exception):
    """No response arrived within REQUEST_TIMEOUT."""


class Session:
    """One MCP session and the stdio child process that backs it."""

    def __init__(self, session_id):
        self.id = session_id
        # Two locks, deliberately. _io_lock serializes stdin writes and the wait
        # for the matching reply, and is therefore held for as long as a call
        # takes. _state_lock only guards small fields, so close() can run — and
        # tear the child down — while a long call is still in flight instead of
        # queueing behind it. Sharing one lock made DELETE and the idle reaper
        # block for the full duration of a slow request.
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.last_used = time.monotonic()
        self.closed = False
        self.proc = subprocess.Popen(
            CHILD_COMMAND,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._inbox = queue.Queue()
        # A reader thread rather than blocking reads: it makes request timeouts
        # and child death observable instead of hanging a worker thread forever.
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    @property
    def short_id(self):
        return self.id[:8]

    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    self._inbox.put(line)
        except (ValueError, OSError):
            pass
        finally:
            self._inbox.put(None)

    def _pump_stderr(self):
        # The child's stderr is its log channel. Forward it so it lands in
        # `ddev logs -s codebase-memory` instead of disappearing.
        try:
            for line in self.proc.stderr:
                line = line.rstrip()
                if line:
                    sys.stderr.write("[session %s] %s\n" % (self.short_id, line))
                    sys.stderr.flush()
        except (ValueError, OSError):
            pass

    def send(self, message):
        """Send one JSON-RPC message. Returns the response, or None for a notification."""
        wants_reply = "id" in message and message["id"] is not None
        with self._io_lock:
            with self._state_lock:
                if self.closed:
                    raise ChildExited("session closed")
                self.last_used = time.monotonic()
            if self.proc.poll() is not None:
                raise ChildExited("child exited with %s" % self.proc.returncode)
            try:
                self.proc.stdin.write(json.dumps(message) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise ChildExited(str(exc))

            if not wants_reply:
                return None

            wanted = message["id"]
            deadline = time.monotonic() + REQUEST_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ChildTimeout("no reply to id %r in %ss" % (wanted, REQUEST_TIMEOUT))
                try:
                    raw = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    raise ChildTimeout("no reply to id %r" % (wanted,))
                if raw is None:
                    raise ChildExited("child closed stdout")
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    log("session %s: dropping non-JSON stdout line" % self.short_id)
                    continue
                if isinstance(parsed, dict) and parsed.get("id") == wanted \
                        and ("result" in parsed or "error" in parsed):
                    with self._state_lock:
                        self.last_used = time.monotonic()
                    return parsed
                # Server-initiated traffic has nowhere to go: this server declares
                # no such capability and GET /mcp is refused, so record and drop.
                log("session %s: dropping unsolicited message %s"
                    % (self.short_id, raw[:200]))

    def close(self):
        with self._state_lock:
            if self.closed:
                return
            self.closed = True
        # Closing stdin and terminating also unblocks any in-flight send(): the
        # reader thread hits EOF and pushes its sentinel, so the waiter raises
        # ChildExited instead of sitting until REQUEST_TIMEOUT.
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            # wait() after kill() is required, not optional: this bridge is PID 1
            # in the container, so an unreaped child stays a zombie forever —
            # there is no init to adopt it.
            try:
                self.proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                log("session %s: child %s would not die" % (self.short_id, self.proc.pid))
        except OSError:
            pass


class SessionRegistry:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def create(self):
        session = Session(uuid.uuid4().hex)
        with self._lock:
            self._sessions[session.id] = session
        log("session %s started (%d live)" % (session.short_id, len(self._sessions)))
        return session

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def drop(self, session_id):
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            session.close()
            log("session %s ended (%d live)" % (session.short_id, len(self._sessions)))
        return session is not None

    def reap_idle(self):
        now = time.monotonic()
        with self._lock:
            stale = [
                sid for sid, s in self._sessions.items()
                if now - s.last_used > IDLE_TIMEOUT or s.proc.poll() is not None
            ]
        for sid in stale:
            log("reaping idle/dead session %s" % sid[:8])
            self.drop(sid)

    def count(self):
        with self._lock:
            return len(self._sessions)


REGISTRY = SessionRegistry()


def _reaper():
    while True:
        time.sleep(60)
        try:
            REGISTRY.reap_idle()
        except Exception as exc:  # never let the reaper die
            log("reaper error: %s" % exc)


def error_response(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def is_initialize(message):
    return isinstance(message, dict) and message.get("method") == "initialize"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cbm-mcp-http-bridge"

    def log_message(self, fmt, *args):  # quieter default access log
        pass

    # --- helpers ------------------------------------------------------------
    def _send(self, status, payload=None, extra_headers=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self):
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == "Bearer %s" % TOKEN

    # --- routes -------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"status": "ok", "sessions": REGISTRY.count()})
            return
        if path == MCP_PATH:
            # Legal per the spec: a server with no server-initiated messages may
            # refuse the SSE stream. Clients fall back to plain POST responses.
            self._send(405, {"error": "no server-initiated stream"},
                       {"Allow": "POST, DELETE"})
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.split("?", 1)[0] != MCP_PATH:
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        session_id = self.headers.get("Mcp-Session-Id", "")
        if session_id and REGISTRY.drop(session_id):
            self._send(204)
        else:
            self._send(404, {"error": "unknown session"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != MCP_PATH:
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._send(400, error_response(None, JSONRPC_INVALID_REQUEST, "empty body"))
            return
        if length > MAX_BODY_BYTES:
            self._send(413, error_response(None, JSONRPC_INVALID_REQUEST, "body too large"))
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, error_response(None, JSONRPC_PARSE_ERROR, "parse error: %s" % exc))
            return

        batch = payload if isinstance(payload, list) else [payload]
        if not batch:
            self._send(400, error_response(None, JSONRPC_INVALID_REQUEST, "empty batch"))
            return

        session_id = self.headers.get("Mcp-Session-Id", "")
        new_session = False
        if session_id:
            session = REGISTRY.get(session_id)
            if session is None:
                # Per spec: the client should start over with a fresh initialize.
                self._send(404, error_response(None, JSONRPC_INVALID_REQUEST,
                                               "unknown session; re-initialize"))
                return
        elif any(is_initialize(m) for m in batch):
            try:
                session = REGISTRY.create()
            except OSError as exc:
                log("failed to spawn child: %s" % exc)
                self._send(500, error_response(None, JSONRPC_INTERNAL_ERROR,
                                               "cannot start server process"))
                return
            session_id = session.id
            new_session = True
        else:
            self._send(400, error_response(None, JSONRPC_INVALID_REQUEST,
                                           "missing Mcp-Session-Id"))
            return

        responses = []
        # Track the message actually being handled, so an error names the request
        # that failed rather than whatever happened to be first in the batch.
        failed_id = None
        try:
            for message in batch:
                if not isinstance(message, dict):
                    responses.append(error_response(None, JSONRPC_INVALID_REQUEST,
                                                    "not a JSON-RPC object"))
                    continue
                failed_id = message.get("id")
                reply = session.send(message)
                if reply is not None:
                    responses.append(reply)
        except ChildTimeout as exc:
            log("session %s timed out: %s" % (session.short_id, exc))
            self._send(504, error_response(failed_id, JSONRPC_INTERNAL_ERROR,
                                           "server timed out"))
            return
        except ChildExited as exc:
            log("session %s child exited: %s" % (session.short_id, exc))
            REGISTRY.drop(session_id)
            self._send(500, error_response(failed_id, JSONRPC_INTERNAL_ERROR,
                                           "server process exited"))
            return

        headers = {"Mcp-Session-Id": session_id} if new_session else None
        if not responses:
            # Notifications only.
            self._send(202, None, headers)
            return
        body = responses if isinstance(payload, list) else responses[0]
        self._send(200, body, headers)


def main():
    # Fail at startup rather than turning every session into a 500: an empty or
    # whitespace-only CBM_BRIDGE_COMMAND splits to [], which Popen rejects.
    if not CHILD_COMMAND:
        log("CBM_BRIDGE_COMMAND is empty — nothing to run")
        return 2

    threading.Thread(target=_reaper, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log("listening on http://%s:%d%s -> %s%s"
        % (HOST, PORT, MCP_PATH, " ".join(CHILD_COMMAND),
           " (token required)" if TOKEN else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
