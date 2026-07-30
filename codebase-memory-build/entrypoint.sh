#!/bin/bash
#ddev-generated

# =============================================================================
# Codebase Memory MCP — DDEV entrypoint
#
#   1. Register the server in .mcp.json (Claude Code) and opencode.json (OpenCode)
#   2. Index the project once, in the background
#   3. Optionally bridge the graph UI onto a routable port
#   4. Exec the MCP Streamable HTTP bridge as the container's main process
#
# The bridge spawns one codebase-memory-mcp process per MCP session, all sharing
# this container's coordination daemon and graph cache.
# =============================================================================

set -uo pipefail

PROJECT_ROOT="/var/www/html"
# Presence of this file is how the container knows it is still installed:
# `ddev add-on remove` deletes it, but leaves this container running.
ADDON_COMPOSE_FILE="${PROJECT_ROOT}/.ddev/docker-compose.codebase-memory.yaml"
CBM_HTTP_HOST="${CBM_HTTP_HOST:-codebase-memory}"
CBM_BRIDGE_PORT="${CBM_BRIDGE_PORT:-9750}"
CBM_BRIDGE_PATH="${CBM_BRIDGE_PATH:-/mcp}"
CBM_UI="${CBM_UI:-false}"
CBM_UI_PORT="${CBM_UI_PORT:-9749}"
CBM_REGISTER_MCP="${CBM_REGISTER_MCP:-true}"
CBM_AUTO_INDEX="${CBM_AUTO_INDEX:-true}"

log() { echo "[codebase-memory] $*"; }

# --- 1. Register the MCP server with the AI clients -------------------------
# Both files live in the project root and are read natively by their client:
# Claude Code reads .mcp.json, OpenCode merges a project-level opencode.json on
# top of its global config. Only our own key is ever touched.
#
# The URL resolves on the project's Docker network, so any container in the
# project can reach it with no credentials to distribute.
MCP_URL="http://${CBM_HTTP_HOST}:${CBM_BRIDGE_PORT}${CBM_BRIDGE_PATH}"

atomic_write() {
  # $1 = destination, stdin = content. Same-directory temp keeps it a rename.
  local dest="$1" tmp
  tmp="$(mktemp "${dest}.XXXXXX")" || return 1
  cat > "$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$dest" && return 0
  rm -f "$tmp"
  return 1
}

register_claude_code() {
  local file="${PROJECT_ROOT}/.mcp.json"
  local desired current
  desired="$(jq -n --arg url "$MCP_URL" '{type: "http", url: $url}')" || return 1

  if [ -s "$file" ] && jq -e 'type == "object"' "$file" >/dev/null 2>&1; then
    current="$(jq -c '.mcpServers["codebase-memory"] // null' "$file")"
    [ "$current" = "$(jq -c . <<<"$desired")" ] && return 2   # already correct
    jq --argjson entry "$desired" \
      '.mcpServers["codebase-memory"] = $entry' "$file" | atomic_write "$file"
  else
    if [ -s "$file" ]; then
      log "WARNING: ${file} is not valid JSON — not registering with Claude Code"
      return 1
    fi
    jq -n --argjson entry "$desired" \
      '{mcpServers: {"codebase-memory": $entry}}' | atomic_write "$file"
  fi
}

register_opencode() {
  local file="${PROJECT_ROOT}/opencode.json"
  local desired current
  # OpenCode calls an HTTP MCP server "remote".
  desired="$(jq -n --arg url "$MCP_URL" \
    '{type: "remote", url: $url, enabled: true}')" || return 1

  if [ -s "$file" ] && jq -e 'type == "object"' "$file" >/dev/null 2>&1; then
    current="$(jq -c '.mcp["codebase-memory"] // null' "$file")"
    [ "$current" = "$(jq -c . <<<"$desired")" ] && return 2   # already correct
    jq --argjson entry "$desired" \
      '.mcp["codebase-memory"] = $entry' "$file" | atomic_write "$file"
  else
    if [ -s "$file" ]; then
      log "WARNING: ${file} is not valid JSON — not registering with OpenCode"
      return 1
    fi
    jq -n --argjson entry "$desired" \
      '{"$schema": "https://opencode.ai/config.json", mcp: {"codebase-memory": $entry}}' \
      | atomic_write "$file"
  fi
}

register_all() {
  register_claude_code; local cc=$?
  register_opencode;    local oc=$?
  [ $cc -eq 0 ] && log "registered in .mcp.json (Claude Code) -> ${MCP_URL}"
  [ $oc -eq 0 ] && log "registered in opencode.json (OpenCode) -> ${MCP_URL}"
  # 2 means "already present and current"
  { [ $cc -eq 0 ] || [ $cc -eq 2 ]; } && { [ $oc -eq 0 ] || [ $oc -eq 2 ]; }
}

if [ "$CBM_REGISTER_MCP" = "true" ]; then
  register_all

  # Sibling AI add-ons rewrite .mcp.json from their own entrypoints at the same
  # moment (read-modify-write), so a concurrent start can drop our key. Re-check
  # for a while and repair; in the steady state every pass is a no-op read.
  (
    for delay in 2 4 10 20; do
      sleep "$delay"
      # Give up if the add-on was removed in the meantime. `ddev add-on remove`
      # deletes this file and strips our entry from the client configs, but the
      # container keeps running until the next `ddev restart` — so without this
      # check the watchdog would put the entry straight back.
      [ -f "$ADDON_COMPOSE_FILE" ] || exit 0
      register_all >/dev/null 2>&1
    done
  ) &
fi

# --- 2. Keep the graph fresh -------------------------------------------------
# CBM's own auto_index defaults to false, and its file watcher only sees changes
# while a session is open. In a DDEV project plenty happens with no agent
# attached (git pull, composer install, a branch switch), so without this the
# graph silently goes stale between sessions. Setting it here — rather than
# relying on whatever is persisted in the cache volume — keeps behaviour
# identical on a fresh volume and an inherited one.
if [ "$CBM_AUTO_INDEX" = "true" ]; then
  codebase-memory-mcp config set auto_index true >/dev/null 2>&1 \
    || log "WARNING: could not enable auto_index"
else
  codebase-memory-mcp config set auto_index false >/dev/null 2>&1 || true
fi

# --- 2b. First-time indexing (background) -----------------------------------
# Runs once per cache volume; `ddev cbm index` forces a re-index at any time.
# CLI mode neither starts nor joins the coordination daemon, and takes per-project
# locks, so it is safe alongside live agent sessions.
INDEX_SENTINEL="${CBM_CACHE_DIR:-${HOME}/.cache/codebase-memory-mcp}/.ddev-auto-indexed"
if [ "$CBM_AUTO_INDEX" = "true" ] && [ ! -f "$INDEX_SENTINEL" ]; then
  (
    log "indexing ${PROJECT_ROOT} (first run; output below, then auto-sync keeps it fresh)"
    # Deliberately no --name: the project name is derived from the repo path
    # ("var-www-html"), and a session's own auto-index always uses that derived
    # name. Overriding it here produced two identical graphs — the named one from
    # this pre-index plus a derived one from the first agent session.
    if codebase-memory-mcp cli --progress index_repository --repo-path "$PROJECT_ROOT"; then
      mkdir -p "$(dirname "$INDEX_SENTINEL")" && touch "$INDEX_SENTINEL"
      log "index complete"
    else
      log "indexing failed — run 'ddev cbm index' to retry"
    fi
  ) &
fi

# --- 3. Graph UI port forwarder (opt-in) ------------------------------------
# codebase-memory-mcp binds its UI to 127.0.0.1 inside the container with no
# option to change the bind address, so a published port mapping alone cannot
# reach it. Bridge it onto the container's external interface on a second port;
# `ddev cbm ui on` publishes that port to host loopback, where the UI's
# localhost-only Host allowlist still accepts the request.
#
# The UI is owned by the coordination daemon, which lives only while at least one
# MCP session is open: expect the URL to answer while an agent session is running.
if [ "$CBM_UI" = "true" ]; then
  (
    while true; do
      socat "TCP-LISTEN:${CBM_UI_BRIDGE_PORT:-9748},fork,reuseaddr,bind=0.0.0.0" \
            "TCP:127.0.0.1:${CBM_UI_PORT}" 2>/dev/null
      sleep 5
    done
  ) &
  log "graph UI bridge listening on :${CBM_UI_BRIDGE_PORT:-9748} -> 127.0.0.1:${CBM_UI_PORT}"
fi

# --- 4. Serve MCP over HTTP -------------------------------------------------
# exec so the bridge is PID 1: Docker signals reach it directly, and its exit
# is the container's exit rather than being masked by a wrapper shell.
log "serving MCP over HTTP at ${MCP_URL}"
exec python3 -u /usr/local/bin/mcp-http-bridge.py
