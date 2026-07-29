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

# `ddev delete` returns before Docker has finished removing the containers. Every
# test reuses the same project name, so starting the next one too soon fails with
# "container ... is marked for removal and cannot be connected to the network".
wait_for_project_removal() {
  local tries=0
  while [ "${tries}" -lt 60 ] \
        && [ -n "$(docker ps -aq --filter "name=ddev-${PROJNAME}-" 2>/dev/null)" ]; do
    sleep 1
    tries=$((tries + 1))
  done
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
  ddev delete -Oy "${PROJNAME}" >/dev/null 2>&1 || true
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
  run ddev start -y
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

export INIT_REQ='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"bats","version":"1"}}}'

# Open a real MCP session the way an agent container does: read the registered
# ssh command straight out of .mcp.json and feed JSON-RPC lines into it.
mcp_session() {
  local cmd
  cmd="$(jq -r '.mcpServers["codebase-memory"] | ([.command] + .args) | @sh' "${TESTDIR}/.mcp.json")"
  printf '%s\n' "$@" | ddev exec bash -c "${cmd}" 2>/dev/null
}

# Wait for the container entrypoint to register the server with both clients.
# A healthy container is not sufficient: the healthcheck covers the binary and
# sshd, both of which come up before registration runs. On macOS the file also
# has to sync from the container back to the host before these assertions see it.
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

  # Registered for Claude Code, as an stdio server reached over SSH
  assert_file_exist "${TESTDIR}/.mcp.json"
  run jq -r '.mcpServers["codebase-memory"].command' "${TESTDIR}/.mcp.json"
  assert_success
  assert_output "ssh"

  # Registered for OpenCode, whose stdio transport is type "local"
  assert_file_exist "${TESTDIR}/opencode.json"
  run jq -r '.mcp["codebase-memory"].type' "${TESTDIR}/opencode.json"
  assert_success
  assert_output "local"

  # DDEV config is kept out of the graph
  assert_file_exist "${TESTDIR}/.cbmignore"

  wait_for_index

  # The end-to-end contract: a JSON-RPC session over SSH from a *different*
  # container, exactly as Claude Code / OpenCode open it. Driven from the web
  # container because it mounts the project (and so the shared SSH key) at the
  # same path the agent containers do.
  run mcp_session "${INIT_REQ}"
  assert_success
  assert_output --partial '"serverInfo"'
  assert_output --partial '"codebase-memory-mcp"'

  # Tools are exposed over that same pipe
  run mcp_session "${INIT_REQ}" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
  assert_success
  assert_output --partial "search_graph"
  assert_output --partial "trace_path"

  # The graph actually contains this project's symbols, and the host command
  # quotes regex arguments so they survive the shell inside the container.
  run ddev cbm search_graph --label Function --name-pattern '.*Router.*'
  assert_success
  assert_output --partial "createRouter"

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
  run jq -r '.mcpServers["codebase-memory"].command' "${TESTDIR}/.mcp.json"
  assert_output "ssh"
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
