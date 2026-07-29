#!/usr/bin/env bash
#
# Manual test: registration watchdog behaviour around add-on removal.
#
#   ./tests/watchdog-test.sh
#
# The container entrypoint re-checks the MCP registration a few times after start
# (at +2s/+4s/+10s/+20s) so a config file that loses the `codebase-memory` entry
# gets it back. `ddev add-on remove` strips that entry on the host but leaves the
# container running, so the watchdog has to know when to stay out of the way.
#
# This is NOT part of tests/test.bats on purpose: it depends on acting inside a
# ~36s window relative to container start, and `ddev restart` returns at a
# variable point within it, which makes it inherently flaky as an automated test.
# Run it by hand after touching the registration logic in
# codebase-memory-build/entrypoint.sh.
#
# Both halves must pass. Part B alone is not enough: a guard that disabled the
# watchdog entirely would also stop the entry coming back after removal, while
# silently breaking the self-heal that Part A covers.

set -uo pipefail

ADDON="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
PROJNAME="test-watchdog-codebase-memory"
export DDEV_NONINTERACTIVE=true
export DDEV_NO_INSTRUMENTATION=true

mkdir -p "${HOME}/tmp"
TESTDIR="$(mktemp -d "${HOME}/tmp/${PROJNAME}.XXXXXX")"

# `ddev delete` returns before Docker has finished removing the containers.
wait_for_project_removal() {
  local tries=0
  while [ "${tries}" -lt 60 ] \
        && [ -n "$(docker ps -aq --filter "name=ddev-${PROJNAME}-" 2>/dev/null)" ]; do
    sleep 1
    tries=$((tries + 1))
  done
}

cleanup() {
  cd "${HOME}" || true
  ddev delete -Oy "${PROJNAME}" >/dev/null 2>&1
  wait_for_project_removal
  docker volume rm "ddev-${PROJNAME}-codebase-memory-cache" >/dev/null 2>&1
  [ -n "${TESTDIR}" ] && rm -rf "${TESTDIR}"
}
trap cleanup EXIT

registration() {
  if jq -e '.mcpServers["codebase-memory"]' "${TESTDIR}/.mcp.json" >/dev/null 2>&1; then
    echo present
  else
    echo absent
  fi
}

# --- Set up a project with the add-on installed ------------------------------
ddev delete -Oy "${PROJNAME}" >/dev/null 2>&1
wait_for_project_removal

mkdir -p "${TESTDIR}/src"
cat > "${TESTDIR}/src/app.js" <<'EOF'
export function bootstrap() { return createRouter(); }
export function createRouter() { return { get: (p, h) => h }; }
EOF
cd "${TESTDIR}" || exit 1
git init -q .
git add -A && git -c user.email=t@t -c user.name=t commit -qm init

ddev config --project-name="${PROJNAME}" --project-tld=ddev.site --docroot=. >/dev/null 2>&1 \
  || { echo "FATAL: ddev config failed"; exit 1; }
ddev add-on get "${ADDON}" >/dev/null 2>&1 \
  || { echo "FATAL: add-on install failed"; exit 1; }
ddev start -y >/dev/null 2>&1 \
  || { echo "FATAL: ddev start failed"; exit 1; }
sleep 20
echo "baseline: registration is $(registration)"

# --- Part A: the watchdog must restore the entry while installed -------------
echo "--- Part A: entry removed by hand, add-on still installed ---"
docker restart "ddev-${PROJNAME}-codebase-memory" >/dev/null 2>&1
sleep 1
jq 'del(.mcpServers["codebase-memory"])' "${TESTDIR}/.mcp.json" > "${TESTDIR}/.tmp.json" \
  && mv "${TESTDIR}/.tmp.json" "${TESTDIR}/.mcp.json"
echo "  right after deleting it: $(registration)"
sleep 35
PART_A="$(registration)"
echo "  after watchdog window:  ${PART_A}   (expected: present)"

# --- Part B: the watchdog must stay out once removed ------------------------
echo "--- Part B: add-on removed inside the watchdog window ---"
docker restart "ddev-${PROJNAME}-codebase-memory" >/dev/null 2>&1
sleep 1
ddev add-on remove codebase-memory-mcp >/dev/null 2>&1
echo "  right after removal:    $(registration)"
sleep 40
PART_B="$(registration)"
echo "  after watchdog window:  ${PART_B}   (expected: absent)"

# --- Verdict ----------------------------------------------------------------
echo ""
rc=0
if [ "${PART_A}" = "present" ]; then
  echo "PASS  Part A: watchdog restores the entry while the add-on is installed"
else
  echo "FAIL  Part A: watchdog did not restore the entry — self-heal is broken"
  rc=1
fi
if [ "${PART_B}" = "absent" ]; then
  echo "PASS  Part B: watchdog leaves the entry alone after removal"
else
  echo "FAIL  Part B: entry reappeared after removal — the guard is not working"
  rc=1
fi

exit "${rc}"
