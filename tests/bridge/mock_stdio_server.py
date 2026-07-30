#!/usr/bin/env python3
"""A stand-in for codebase-memory-mcp, used by the bridge tests.

Speaks just enough stdio JSON-RPC to exercise the transport, and echoes its own
PID in every result so the tests can prove that each MCP session is backed by a
separate process.

Methods:
  initialize        adds protocolVersion/serverInfo, like a real MCP server
  slow              sleeps params.seconds before replying (timeout tests)
  ignore-terminate  stops responding to SIGTERM (teardown/kill-path tests)
  anything else     echoes method and params back

Notifications (no id) are never answered, matching JSON-RPC.
"""
import json
import os
import signal
import sys
import time


def main():
    sys.stderr.write("mock server up pid=%d\n" % os.getpid())
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue

        method = message.get("method")
        params = message.get("params") or {}

        if method == "ignore-terminate":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        elif method == "slow":
            time.sleep(float(params.get("seconds", 1)))

        # A notification gets no reply.
        if message.get("id") is None:
            sys.stderr.write("notification: %s\n" % method)
            sys.stderr.flush()
            continue

        result = {"pid": os.getpid(), "method": method, "echo": params}
        if method == "initialize":
            result["protocolVersion"] = "2024-11-05"
            result["serverInfo"] = {"name": "mock-stdio-server", "version": "1"}

        sys.stdout.write(json.dumps(
            {"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
