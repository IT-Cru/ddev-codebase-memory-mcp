# DDEV Codebase Memory MCP

[![tests](https://github.com/IT-Cru/ddev-codebase-memory-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/IT-Cru/ddev-codebase-memory-mcp/actions/workflows/tests.yml)
[![last commit](https://img.shields.io/github/last-commit/IT-Cru/ddev-codebase-memory-mcp)](https://github.com/IT-Cru/ddev-codebase-memory-mcp/commits)
[![release](https://img.shields.io/github/v/release/IT-Cru/ddev-codebase-memory-mcp)](https://github.com/IT-Cru/ddev-codebase-memory-mcp/releases/latest)

## Overview

Runs [Codebase Memory MCP](https://github.com/DeusData/codebase-memory-mcp) — a
knowledge graph of your codebase for AI agents (158 languages, sub-millisecond
queries) — in its own DDEV container, and wires it into
[Claude Code](https://github.com/trebormc/ddev-claude-code) and
[OpenCode](https://github.com/trebormc/ddev-opencode) as an MCP server.

Instead of reading file after file to answer "who calls this?", the agent queries
a graph of functions, classes, call chains and HTTP routes. It also works
standalone, without the AI add-ons.

## Installation

```bash
ddev add-on get IT-Cru/ddev-codebase-memory-mcp
```

```bash
ddev restart
```

That's it. On first start the project is indexed in the background, and the
server is registered for both clients. Watch the initial index with:

```bash
ddev cbm logs -f
```

Works alongside [ddev-ai-workspace](https://github.com/trebormc/ddev-ai-workspace)
in any install order — see [AI Workspace](#use-with-ddev-ai-workspace).

## Usage

Start an agent and ask it something structural — it will reach for the graph
instead of grepping:

```bash
ddev claude-code
```

```
> which functions call OrderHandler::handle, and what would break if I change it?
```

The same tools are available from the command line:

```bash
ddev cbm                                              # all commands
ddev cbm get_architecture                             # languages, packages, routes, hotspots
ddev cbm search_graph --label Function --name-pattern '.*Handler.*'
ddev cbm trace_path --function-name handle --direction both
ddev cbm get_code_snippet --qualified-name 'var-www-html.src.handler.OrderHandler.handle'
ddev cbm index                                        # force a re-index
ddev cbm logs -f                                      # indexing progress
```

Query tools need a `--project`; `ddev cbm` fills it in automatically when this
project is the only one in the graph.

## How it works

`codebase-memory-mcp` speaks **stdio MCP only** — it has no HTTP transport of its
own (its `--port` flag serves the graph UI, not the protocol). A small bridge
shipped with this add-on puts MCP Streamable HTTP in front of it, so agents
register it with a plain URL instead of a command:

```
┌──────────────────┐    POST /mcp (JSON-RPC)   ┌───────────────────────────┐
│  claude-code     │ ────────────────────────► │  codebase-memory          │
│  opencode        │                           │   mcp-http-bridge.py :9750│
│  (agent CLIs)    │                           │    ├─ codebase-memory-mcp │
└──────────────────┘                           │    ├─ codebase-memory-mcp │
         │                                     │    └─ …one per session    │
         │                                     │   graph cache (volume)    │
         └── project mounted at /var/www/html ──┴───────────────────────────┘
```

The bridge is ~300 lines of Python **standard library only** — no pip packages, so
nothing third-party sits between an agent and your source. It speaks the parts of
the transport this server needs and refuses the rest explicitly: `POST /mcp` for
messages, `DELETE /mcp` to end a session, `GET /mcp` answers `405` (this server
sends no server-initiated messages, so there is no SSE stream to open), and
`GET /health` backs the container healthcheck.

**Every MCP session gets its own `codebase-memory-mcp` process**, keyed by the
`Mcp-Session-Id` header. This is the design's load-bearing property, not an
optimization: a stdio pipe carries a single interleaved JSON-RPC stream plus
per-session `initialize` state, so a shared child process would let two agents
corrupt each other's traffic. All sessions share one coordination daemon and one
graph cache. Idle sessions are reaped, and their child process with them.

Both containers mount the project at the **same path** (`/var/www/html`), so the
file paths stored in the graph resolve identically on both sides.

The endpoint is published only on the project's Docker network, so there are no
credentials to distribute. Set `CBM_BRIDGE_TOKEN` to require
`Authorization: Bearer <token>` if you want it locked down anyway.

### What gets written to your project

| File | Purpose |
|------|---------|
| `.mcp.json` | Claude Code MCP registration (`codebase-memory` key only) |
| `opencode.json` | OpenCode MCP registration (`codebase-memory` key only) |
| `.cbmignore` | Excludes `.ddev/` from the graph — created only if absent |

Both JSON files are shared with other add-ons, so registration **merges**: other
MCP servers, your `model` setting, and anything else you added are preserved, and
`ddev add-on remove` deletes only the `codebase-memory` key.

Committing `.mcp.json` and `opencode.json` is a good idea — teammates then get the
server without extra setup.

The `.cbmignore` excludes `.ddev/` because DDEV's config directory contains shell
scripts that would otherwise be indexed as application code and appear in search
and trace results.

## Configuration

Settings live in `.ddev/.env.codebase-memory` and apply after `ddev restart`:

```bash
ddev dotenv set .ddev/.env.codebase-memory --cbm-workers=4
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `CBM_AUTO_INDEX` | `true` | Index on first start, and refresh a stale graph when a session opens |
| `CBM_REGISTER_MCP` | `true` | Maintain the `.mcp.json` / `opencode.json` entries |
| `CBM_WORKERS` | *(detected)* | Indexing threads. Worth setting: the binary sees **host** CPU count, not the container's quota |
| `CBM_MEM_BUDGET_MB` | *(detected)* | Cap the in-memory graph budget, likewise derived from host RAM |
| `CBM_LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error`, `none` |
| `CBM_VERSION` | `latest` | Pin a release, e.g. `v0.9.0` (rebuild required) |
| `CBM_VARIANT` | `ui` | `ui` or `default` (rebuild required) |
| `CBM_BRIDGE_TOKEN` | *(unset)* | Require `Authorization: Bearer <token>` on the endpoint |
| `CBM_BRIDGE_IDLE_TIMEOUT` | `1800` | Seconds before an idle session and its process are reaped |
| `CBM_BRIDGE_REQUEST_TIMEOUT` | `900` | Ceiling for a single call; a full re-index can be slow |

Indexing is bounded to the project by `CBM_ALLOWED_ROOT=/var/www/html`, so an agent
cannot direct the indexer at arbitrary host paths.

### Freshness

CBM watches the filesystem while a session is open, so edits land in the graph as
you make them. Changes made with **no agent running** — a `git pull`, a branch
switch, `composer install` — are picked up by a re-index when the next session
starts, which `CBM_AUTO_INDEX` enables by default. `ddev cbm index` forces one at
any time.

### Sharing the graph with your team

```bash
ddev cbm index --persistence true
```

This writes `.codebase-memory/graph.db.zst`, a compressed snapshot. Commit it and
teammates bootstrap from it instead of re-indexing from scratch. Add
`.codebase-memory/` to `.gitignore` if you'd rather everyone index locally.

### Graph UI

The graph visualization needs no setting up. Start an agent, then open:

```
http://<project>.ddev.site:9760      (or https on :9761)
```

```bash
ddev cbm ui
```

prints that URL and whether it is currently up.

The UI belongs to the coordination daemon, which exists only while at least one
MCP session is open — so it answers while `ddev claude-code` or `ddev opencode` is
running, and serves an explanatory page the rest of the time. There is nothing to
enable or disable: with no session there is no daemon and no UI, and with a session
the UI is already there.

The bridge reverse-proxies it, rewriting the `Host` header on the way through.
That is what makes a `*.ddev.site` URL work at all: `codebase-memory-mcp` binds the
UI to loopback and refuses any `Host` other than `localhost`, as DNS-rebinding
protection.

> **Coming from a non-DDEV install?** Your own `codebase-memory-mcp` keeps serving
> its graph on <http://localhost:9749> — that is untouched. A DDEV project's graph
> is a *separate* index living in this container, published on `9760` precisely so
> the two never collide. If you run both, `9749` is your machine's codebase and
> `9760` is this project's.

**Several projects at once work as you would expect.** `ddev-router` publishes
`9760` once and routes by hostname, the same way it serves every project on `443`,
so `projectA.ddev.site:9760` and `projectB.ddev.site:9760` each reach their own
container and their own graph. Nothing needs a distinct port per project.

A port clash is therefore only ever with a **non-DDEV** process on your host — a
natively installed `codebase-memory-mcp` being the likely one. If `9760`/`9761` are
taken, move them:

```bash
ddev dotenv set .ddev/.env.codebase-memory --cbm-ui-http-expose=9860:9760
```

Because the UI is reachable on the project's hostname, anything that can reach your
DDEV router can browse the graph and call its `/api` endpoints — which include
triggering a re-index and killing CBM processes. That is a deliberate trade for a
local development tool; if it does not suit you, drop the `HTTP_EXPOSE` /
`HTTPS_EXPOSE` lines from `docker-compose.codebase-memory.yaml`.

## Use with ddev-ai-workspace

```bash
ddev add-on get trebormc/ddev-ai-workspace
ddev add-on get IT-Cru/ddev-codebase-memory-mcp
ddev restart
```

Either order works. This add-on has no dependency on the workspace — it only
registers itself in the project-root config files both clients already read, the
same way `ddev-playwright-mcp` does, and nothing owned by another add-on changes.

If you install the workspace *after* this add-on, run `ddev restart` afterwards so
the registration is re-checked.

## Notes

**The graph project is named `var-www-html`.** CBM derives the name from the
repository path, which inside the container is the mount point, so it appears in
qualified names such as `var-www-html.src.app.createRouter`. `ddev cbm` supplies
`--project` for you; agents read the name from `list_projects`.

**Platforms.** Linux `amd64` and `arm64`, using the statically linked `portable`
release build. Downloads are SHA-256 verified against the release `checksums.txt`
at image build time.

**Resource use.** Indexing is RAM-first and parallel. On a large monorepo, set
`CBM_WORKERS` and `CBM_MEM_BUDGET_MB` — left unset, the binary sizes itself against
the host's CPUs and RAM rather than the container's limits.

## Troubleshooting

Check the server is reachable exactly as an agent reaches it:

```bash
ddev cbm version
```

Check the endpoint from another container, the way an agent reaches it:

```bash
ddev exec curl -sS http://codebase-memory:9750/health
```

Open a real MCP session by hand — this is the whole handshake:

```bash
ddev exec curl -sS -D /tmp/h -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' http://codebase-memory:9750/mcp
```

| Symptom | Cause |
|---------|-------|
| `Connection refused` from an agent | Bridge not up. `ddev cbm logs`, check the container is healthy |
| `HTTP 404` with `re-initialize` | Session expired or the container restarted; the client should re-initialize |
| `HTTP 401` | `CBM_BRIDGE_TOKEN` is set but the client sends no matching bearer header |
| Agent shows no `codebase-memory` tools | Entry missing from `.mcp.json` / `opencode.json` — `ddev restart`, and check `CBM_REGISTER_MCP` |
| Empty query results | Index not finished. `ddev cbm logs`, or `ddev cbm index` |
| Graph UI says "not running" | No agent session open, so no daemon owns the UI — start `ddev claude-code` |
| `port is already allocated` on start | `9760`/`9761` are taken; move them with `--cbm-ui-http-expose=` |

Container logs, including indexing:

```bash
ddev cbm logs -f
```

## Removal

```bash
ddev add-on remove codebase-memory-mcp
```

This deregisters the server from both config files and removes the add-on's files.
The graph cache volume is kept on purpose, because re-indexing is expensive. To
drop it:

```bash
docker volume rm ddev-<project>-codebase-memory-cache
```

## Credits

- [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) by DeusData
- Built from [ddev/ddev-addon-template](https://github.com/ddev/ddev-addon-template)
- Integrates with [ddev-ai-workspace](https://github.com/trebormc/ddev-ai-workspace) by trebormc

**Contributed and maintained by [IT-Cru](https://github.com/IT-Cru)**
