#!/usr/bin/env bats

# Bats is a testing framework for Bash
# Documentation https://bats-core.readthedocs.io/en/stable/
# Bats libraries documentation https://github.com/ztombol/bats-docs
#
# For local tests, install bats-core, bats-assert, bats-file, bats-support
# And run this in the add-on root directory:
#   bats ./tests/test.bats
# To exclude release tests:
#   bats ./tests/test.bats --filter-tags '!release'
# For debugging:
#   bats ./tests/test.bats --show-output-of-passing-tests --verbose-run --print-output-on-failure

# `ddev delete` returns before Docker has finished tearing the project down, and
# every test reuses the same project name. Starting too soon fails in more than one
# way — "container ... is marked for removal", or "network ... has active
# endpoints" when a container is gone from `ps` but its network endpoint has not
# detached yet — so wait for both the containers and the project network.
wait_for_project_removal() {
  local tries=0
  while [ "${tries}" -lt 60 ]; do
    if [ -z "$(docker ps -aq --filter "name=ddev-${PROJNAME}-" 2>/dev/null)" ] \
       && [ -z "$(docker network ls -q --filter "name=ddev-${PROJNAME}_default" 2>/dev/null)" ]; then
      return 0
    fi
    sleep 1
    tries=$((tries + 1))
  done
}

# Even with that wait, Docker teardown is eventually-consistent enough that a
# start can still lose a race. Retry a bounded number of times instead of trying
# to enumerate every failure mode; `ddev poweroff` is the documented way to clear
# stale ddev networks and endpoints. Note it stops other ddev projects too, but it
# only runs when a start has already failed.
ddev_start_with_retry() {
  local attempt
  for attempt in 1 2; do
    if ddev start -y >/dev/null 2>&1; then
      return 0
    fi
    echo "# ddev start attempt ${attempt} failed; poweroff and retry" >&3
    ddev poweroff >/dev/null 2>&1 || true
    wait_for_project_removal
    sleep 5
  done
  # Final attempt with output visible, so a real failure is reported properly.
  ddev start -y
}

setup() {
  set -eu -o pipefail

  export GITHUB_REPO=IT-Cru/ddev-codebase-memory-mcp

  TEST_BREW_PREFIX="$(brew --prefix 2>/dev/null || true)"
  export BATS_LIB_PATH="${BATS_LIB_PATH}:${TEST_BREW_PREFIX}/lib:/usr/lib/bats"
  bats_load_library bats-assert
  bats_load_library bats-file
  bats_load_library bats-support

  export DIR="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." >/dev/null 2>&1 && pwd)"
  export PROJNAME="test-$(basename "${GITHUB_REPO}")"
  mkdir -p "${HOME}/tmp"
  export TESTDIR="$(mktemp -d "${HOME}/tmp/${PROJNAME}.XXXXXX")"
  export DDEV_NONINTERACTIVE=true
  export DDEV_NO_INSTRUMENTATION=true
  # A project of this name may survive an earlier run, and `mktemp` means it is
  # registered against a *different* directory. ddev then refuses to work here
  # with "a project ... already exists ... that was created at <other path>", so
  # clear the registration by name — not by path — before configuring this one.
  ddev delete -Oy "${PROJNAME}" >/dev/null 2>&1 || true
  ddev stop -U "${PROJNAME}" >/dev/null 2>&1 || true
  wait_for_project_removal
  cd "${TESTDIR}"

  # Give the indexer real symbols to find, so the graph assertions are meaningful.
  mkdir -p src
  cat > src/handler.php <<'PHPEOF'
<?php
namespace App;

class OrderHandler {
    public function handle(array $order): bool {
        return $this->validate($order);
    }
    private function validate(array $order): bool { return isset($order['id']); }
}
PHPEOF
  cat > src/app.js <<'JSEOF'
export function bootstrap() { return createRouter(); }
export function createRouter() { return { get: (p, h) => h }; }
JSEOF

  run ddev config --project-name="${PROJNAME}" --project-tld=ddev.site --docroot=.
  assert_success
  run ddev_start_with_retry
  assert_success
}

teardown() {
  set -eu -o pipefail
  ddev delete -Oy "${PROJNAME}" >/dev/null 2>&1
  wait_for_project_removal
  # Persist TESTDIR if running inside GitHub Actions. Useful for uploading test result artifacts
  # See example at https://github.com/ddev/github-action-add-on-test#preserving-artifacts
  if [ -n "${GITHUB_ENV:-}" ]; then
    [ -e "${GITHUB_ENV:-}" ] && echo "TESTDIR=${HOME}/tmp/${PROJNAME}" >> "${GITHUB_ENV}"
  else
    [ "${TESTDIR}" != "" ] && rm -rf "${TESTDIR}"
  fi
}

# Open a real MCP session over Streamable HTTP the way an agent container does:
# use the URL exactly as registered in .mcp.json (so the assertion covers the
# registration being usable, not just well-formed), initialize to get a session
# id, then run any further JSON-RPC bodies passed as arguments.
#
# Driven from the web container: a different container on the same project
# network, which is the situation the agent containers are in. The probe is
# `docker cp`-ed rather than written into the project, so it does not depend on
# a mutagen sync landing first.
mcp_session() {
  local url probe
  url="$(jq -r '.mcpServers["codebase-memory"].url' "${TESTDIR}/.mcp.json")"
  probe="$(mktemp)"
  cat > "${probe}" <<'PROBE'
set -u
URL="$1"; shift
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"bats","version":"1"}}}'
curl -sS -D /tmp/mcp-hdr -H 'Content-Type: application/json' -d "$INIT" "$URL"
SID="$(grep -i '^Mcp-Session-Id:' /tmp/mcp-hdr | tr -d '\r' | awk '{print $2}')"
[ -n "$SID" ] || { echo "no session id returned" >&2; exit 1; }
for body in "$@"; do
  curl -sS -H 'Content-Type: application/json' -H "Mcp-Session-Id: $SID" -d "$body" "$URL"
done
PROBE
  docker cp "${probe}" "ddev-${PROJNAME}-web:/tmp/mcp-probe.sh" >/dev/null 2>&1
  rm -f "${probe}"
  ddev exec bash /tmp/mcp-probe.sh "${url}" "$@" 2>/dev/null
}

# Count live stdio server processes inside the add-on's container.
count_server_processes() {
  docker exec "ddev-${PROJNAME}-codebase-memory" \
    bash -c 'ps -eo args | grep -c "^codebase-memory-mcp$"' 2>/dev/null || echo 0
}

# Wait for the container entrypoint to register the server with both clients.
# A healthy container is not sufficient: registration happens before the HTTP
# bridge is exec'd, and on macOS the file also has to sync from the container
# back to the host before these assertions can see it.
wait_for_registration() {
  local tries=0
  until [ "${tries}" -ge 30 ]; do
    if jq -e '.mcpServers["codebase-memory"]' "${TESTDIR}/.mcp.json" >/dev/null 2>&1 \
       && jq -e '.mcp["codebase-memory"]' "${TESTDIR}/opencode.json" >/dev/null 2>&1; then
      return 0
    fi
    tries=$((tries + 1))
    sleep 2
  done
  echo "# 'codebase-memory' was not registered in .mcp.json / opencode.json in time" >&3
  ddev logs -s codebase-memory 2>&1 | tail -20 >&3
  return 1
}

# Wait for the background first-run index to finish.
wait_for_index() {
  local tries=0
  until [ "${tries}" -ge 60 ]; do
    if ddev exec -s codebase-memory codebase-memory-mcp cli list_projects 2>/dev/null \
        | grep -q '"nodes"'; then
      return 0
    fi
    tries=$((tries + 1))
    sleep 5
  done
  echo "# index did not complete in time; container logs:" >&3
  ddev logs -s codebase-memory 2>&1 | tail -20 >&3
  return 1
}

health_checks() {
  # The binary is installed and runnable in its own container
  run ddev exec -s codebase-memory codebase-memory-mcp --version
  assert_success
  assert_output --partial "codebase-memory-mcp"

  wait_for_registration

  # Registered for Claude Code as an HTTP server
  assert_file_exist "${TESTDIR}/.mcp.json"
  run jq -r '.mcpServers["codebase-memory"].type' "${TESTDIR}/.mcp.json"
  assert_success
  assert_output "http"
  run jq -r '.mcpServers["codebase-memory"].url' "${TESTDIR}/.mcp.json"
  assert_success
  assert_output "http://codebase-memory:9750/mcp"

  # Registered for OpenCode, which calls an HTTP MCP server "remote"
  assert_file_exist "${TESTDIR}/opencode.json"
  run jq -r '.mcp["codebase-memory"].type' "${TESTDIR}/opencode.json"
  assert_success
  assert_output "remote"

  # DDEV config is kept out of the graph
  assert_file_exist "${TESTDIR}/.cbmignore"

  wait_for_index

  # The end-to-end contract: a JSON-RPC session over HTTP from a *different*
  # container, using the registered URL, exactly as Claude Code / OpenCode do.
  run mcp_session
  assert_success
  assert_output --partial '"serverInfo"'
  assert_output --partial '"codebase-memory-mcp"'

  # Tools are exposed over that same session
  run mcp_session '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
  assert_success
  assert_output --partial "search_graph"
  assert_output --partial "trace_path"

  # A real tool call round-trips through the bridge and hits the graph
  run mcp_session '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_graph","arguments":{"project":"var-www-html","label":"Function","name_pattern":".*Router.*"}}}'
  assert_success
  assert_output --partial "createRouter"

  # The graph actually contains this project's symbols, and the host command
  # quotes regex arguments so they survive the shell inside the container.
  run ddev cbm search_graph --label Function --name-pattern '.*Router.*'
  assert_success
  assert_output --partial "createRouter"

  # --project is auto-filled for get_graph_schema too, which the README tells
  # people to run first.
  run ddev cbm get_graph_schema
  assert_success
  assert_output --partial "node_labels"

  # Exactly one project: a second, redundant graph appears if the pre-index and
  # a session's auto-index disagree about the project name.
  run bash -c "ddev cbm list_projects 2>/dev/null | tail -1 | jq '.projects | length'"
  assert_success
  assert_output "1"
}

@test "install from directory" {
  set -eu -o pipefail
  echo "# ddev add-on get ${DIR} with project ${PROJNAME} in $(pwd)" >&3
  run ddev add-on get "${DIR}"
  assert_success
  run ddev restart -y
  assert_success
  health_checks
}

@test "each MCP session gets its own server process" {
  set -eu -o pipefail
  # The transport's load-bearing property. A stdio pipe carries one interleaved
  # JSON-RPC stream and per-session `initialize` state, so if the bridge shared a
  # single child process, two agents (Claude Code + OpenCode, or two sessions of
  # one) would corrupt each other's traffic. Assert isolation directly rather
  # than trusting it.
  run ddev add-on get "${DIR}"
  assert_success
  run ddev restart -y
  assert_success
  wait_for_registration

  local url probe
  url="$(jq -r '.mcpServers["codebase-memory"].url' "${TESTDIR}/.mcp.json")"

  # Open three sessions and keep them open, then count server processes.
  probe="$(mktemp)"
  cat > "${probe}" <<'PROBE'
set -u
URL="$1"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"bats","version":"1"}}}'
for n in 1 2 3; do
  curl -sS -D "/tmp/h$n" -H 'Content-Type: application/json' -d "$INIT" "$URL" >/dev/null
  grep -i '^Mcp-Session-Id:' "/tmp/h$n" | tr -d '\r' | awk '{print $2}'
done
PROBE
  docker cp "${probe}" "ddev-${PROJNAME}-web:/tmp/open3.sh" >/dev/null
  rm -f "${probe}"

  run ddev exec bash /tmp/open3.sh "${url}"
  assert_success
  # Three distinct session ids
  local ids
  ids="$(echo "${output}" | grep -cE '^[0-9a-f]{32}$')"
  [ "${ids}" -eq 3 ]
  [ "$(echo "${output}" | sort -u | grep -cE '^[0-9a-f]{32}$')" -eq 3 ]

  # ...backed by three distinct processes
  run count_server_processes
  assert_output "3"

  # The bridge agrees about how many sessions it is holding
  run ddev exec curl -sS "http://codebase-memory:9750/health"
  assert_success
  assert_output --partial '"sessions": 3'
}

@test "graph UI is served through the ddev router" {
  set -eu -o pipefail
  run ddev add-on get "${DIR}"
  assert_success
  run ddev restart -y
  assert_success
  wait_for_registration

  # --resolve keeps this independent of *.ddev.site DNS while still sending the
  # project Host header, which is what the router matches on.
  local host="${PROJNAME}.ddev.site"
  local curl_opts=(--resolve "${host}:9760:127.0.0.1" -s -o)

  # No agent session anywhere: the UI must still come up, because the bridge opens
  # a session of its own on demand. This is what makes the add-on usable standalone.
  run bash -c "ddev exec curl -sS http://codebase-memory:9760/health"
  assert_success
  assert_output --partial '"sessions": 0'

  run bash -c "curl ${curl_opts[*]} '${TESTDIR}/ui.html' -w '%{http_code}' 'http://${host}:9760/'"
  assert_success
  assert_output "200"
  # Reaching it under a *.ddev.site host at all proves the bridge rewrote Host:
  # codebase-memory-mcp answers 403 to any host but localhost.
  run grep -q "Codebase Memory" "${TESTDIR}/ui.html"
  assert_success

  # ...and that it was the keeper, not an agent, holding the daemon up.
  run bash -c "ddev exec curl -sS http://codebase-memory:9760/health"
  assert_success
  assert_output --partial '"ui_keeper": true'
}

@test "MCP is reachable from the host through the ddev router" {
  set -eu -o pipefail
  # The standalone path for non-DDEV clients: same graph, same server, reached by
  # URL from outside the project's Docker network.
  run ddev add-on get "${DIR}"
  assert_success
  run ddev restart -y
  assert_success
  wait_for_registration
  wait_for_index

  local host="${PROJNAME}.ddev.site"
  local url="http://${host}:9760/mcp"
  local init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"bats-host","version":"1"}}}'

  run bash -c "curl --resolve '${host}:9760:127.0.0.1' -sS -D '${TESTDIR}/mcp-h' -H 'Content-Type: application/json' -d '${init}' '${url}'"
  assert_success
  assert_output --partial '"serverInfo"'
  assert_output --partial '"codebase-memory-mcp"'

  # The session id header is what a host client needs for follow-up calls.
  run grep -qi '^Mcp-Session-Id:' "${TESTDIR}/mcp-h"
  assert_success
}

@test "the cbm shim queries the graph from inside a container" {
  set -eu -o pipefail
  # Agent containers mount the project but have no ddev binary and no MCP client,
  # so the shim is how an agent can pipe graph results through jq instead of
  # pulling whole tool responses into its context. Driven from the web container,
  # which sits in the same place on the project network.
  run ddev add-on get "${DIR}"
  assert_success
  run ddev restart -y
  assert_success
  wait_for_index

  local shim=/var/www/html/.ddev/codebase-memory/cbm

  run ddev exec "${shim}" --tools
  assert_success
  assert_output --partial "search_graph"
  assert_output --partial "query_graph"

  # --project is filled in automatically, and --name-pattern maps to name_pattern.
  run ddev exec "${shim}" search_graph --label Function --name-pattern '.*Router.*'
  assert_success
  assert_output --partial "createRouter"

  # get_graph_schema needs --project like the other query tools. The agent
  # instructions say to call it first, so a regression here is quietly expensive.
  run ddev exec "${shim}" get_graph_schema
  assert_success
  assert_output --partial "node_labels"

  # The point of the shim: compose with jq so only the answer is produced.
  run ddev exec bash -c "${shim} search_graph --label Function | jq -c '[.results[] | {name}]'"
  assert_success
  assert_output --partial '{"name":"createRouter"}'
  refute_output --partial '"fp"'

  # A bad tool name fails loudly rather than emitting empty output.
  run ddev exec "${shim}" no_such_tool
  assert_failure

  # One session serves repeated calls rather than one process per invocation.
  run bash -c "ddev exec curl -sS http://codebase-memory:9760/health"
  assert_success
  assert_output --partial '"sessions": 1'
}

@test "foreign MCP entries survive install and removal" {
  set -eu -o pipefail
  # Both config files are shared with other add-ons (ddev-playwright-mcp writes
  # .mcp.json too), so registration must merge rather than overwrite.
  echo '{"mcpServers":{"other":{"type":"http","url":"http://example:1/mcp"}}}' > "${TESTDIR}/.mcp.json"
  echo '{"model":"some/model","mcp":{"other":{"type":"remote","url":"http://example:1/mcp"}}}' > "${TESTDIR}/opencode.json"

  run ddev add-on get "${DIR}"
  assert_success
  run ddev restart -y
  assert_success
  wait_for_registration

  run jq -r '.mcpServers.other.url' "${TESTDIR}/.mcp.json"
  assert_output "http://example:1/mcp"
  run jq -r '.mcpServers["codebase-memory"].type' "${TESTDIR}/.mcp.json"
  assert_output "http"
  run jq -r '.model' "${TESTDIR}/opencode.json"
  assert_output "some/model"

  # Removal takes out our entry and leaves everything else alone
  run ddev add-on remove codebase-memory-mcp
  assert_success
  run jq -r '.mcpServers.other.url' "${TESTDIR}/.mcp.json"
  assert_output "http://example:1/mcp"
  run jq -r '.mcpServers["codebase-memory"] // "absent"' "${TESTDIR}/.mcp.json"
  assert_output "absent"
  run jq -r '.mcp["codebase-memory"] // "absent"' "${TESTDIR}/opencode.json"
  assert_output "absent"
}

# bats test_tags=release
@test "install from release" {
  set -eu -o pipefail
  echo "# ddev add-on get ${GITHUB_REPO} with project ${PROJNAME} in $(pwd)" >&3
  run ddev add-on get "${GITHUB_REPO}"
  assert_success
  run ddev restart -y
  assert_success
  health_checks
}
