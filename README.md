# DDEV Codebase Memory MCP

[![add-on registry](https://img.shields.io/badge/DDEV-Add--on_Registry-blue)](https://addons.ddev.com)
[![tests](https://github.com/IT-Cru/ddev-codebase-memory-mcp/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/IT-Cru/ddev-codebase-memory-mcp/actions/workflows/tests.yml?query=branch%3Amain)
[![last commit](https://img.shields.io/github/last-commit/IT-Cru/ddev-codebase-memory-mcp)](https://github.com/IT-Cru/ddev-codebase-memory-mcp/commits)
[![release](https://img.shields.io/github/v/release/IT-Cru/ddev-codebase-memory-mcp)](https://github.com/IT-Cru/ddev-codebase-memory-mcp/releases/latest)

## Overview

Runs [Codebase Memory MCP](https://github.com/DeusData/codebase-memory-mcp) — a
knowledge graph of your codebase across 158 languages, with sub-millisecond queries
— in its own DDEV container, indexed and kept up to date for you.

Instead of reading file after file to answer "who calls this?", you query a graph
of functions, classes, call chains and HTTP routes. Three ways to use it, and they
all work off the same index:

| | |
|---|---|
| **Browse the graph** | A visual explorer at `http://<project>.ddev.site:9760` — no agent, no config |
| **Give an AI agent the tools** | An MCP endpoint any client can reach by URL, registered automatically for the DDEV AI agents |
| **Query from the shell** | `ddev cbm <tool>` runs any of the graph tools as a one-shot command |

Nothing here depends on other add-ons — and it slots straight into
[ddev-ai-workspace](https://github.com/trebormc/ddev-ai-workspace) when you use it,
see [AI agents](#ai-agents).

## Installation

```bash
ddev add-on get IT-Cru/ddev-codebase-memory-mcp
ddev restart
```

That's it. The project is indexed in the background on first start, and the MCP
server is registered for the AI agent add-ons if they are present. Watch the
initial index with:

```bash
ddev cbm logs -f
```

## Browse the graph

Open the visual explorer — no agent required, nothing to enable:

```
http://<project>.ddev.site:9760      (or https on :9761)
```

```bash
ddev cbm ui
```

prints the URL and whether it is up. See [Graph UI](#graph-ui) for the details.

## Query from the shell

Every graph tool is available as a one-shot command, which makes the add-on useful
on its own and handy for scripting:

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

## AI agents

The server is registered automatically for
[Claude Code](https://github.com/trebormc/ddev-claude-code) and
[OpenCode](https://github.com/trebormc/ddev-opencode), so with either installed
you just start an agent and ask something structural — it reaches for the graph
instead of grepping:

```bash
ddev claude-code
```

```
> which functions call OrderHandler::handle, and what would break if I change it?
```

Those two come as part of
[ddev-ai-workspace](https://github.com/trebormc/ddev-ai-workspace), which bundles a
full AI development setup for DDEV — agent CLIs, a headless browser, task tracking
and more. This add-on is a natural companion to it: install in either order, no
configuration, nothing owned by another add-on is modified. See
[Use with ddev-ai-workspace](#use-with-ddev-ai-workspace).

### Getting more out of an agent

Two things ship with the add-on to make agents use the graph well rather than
falling back to grep.

**Instructions.** `.ddev/codebase-memory/AGENT-INSTRUCTIONS.md` tells an agent which
tool answers which question, to call `get_graph_schema` first, and to prefer one
`query_graph` call over a chain of narrower ones. Reference it from your own
instruction file rather than having it installed over the top — for Claude Code, add
to `CLAUDE.md`:

```
@.ddev/codebase-memory/AGENT-INSTRUCTIONS.md
```

For OpenCode, add the path to the `instructions` array in your `opencode.json`.

**A shell shim.** `.ddev/codebase-memory/cbm` runs any graph tool from inside a
container — agent containers mount the project but have no `ddev` binary — and prints
only the tool's result, so it composes in a script:

```bash
CBM=.ddev/codebase-memory/cbm
out="$($CBM search_graph --label Class)"
prefix="$(printf '%s\n' "$out" | sed -n 's/^\(var-www-html[^ ]*\) (.*/\1/p')"
for name in $(printf '%s\n' "$out" | awk '/^  [A-Za-z]/ {print $1}'); do
  "$CBM" get_code_snippet --qualified-name "$prefix.$name"
done
```

What that buys is **round-trips, not payload size**. Every MCP tool call sends the
whole conversation through the model again, so a question that fans out over ten
symbols costs eleven inferences; the script above costs one. Payload size is already
handled upstream — the query tools answer in a compact tree format, so there is
nothing left for us to trim.

Note the query surface is **not JSON** — slice it with `awk`, not `jq`. Some tools,
`get_code_snippet` among them, still answer in JSON, so check rather than assume.

It reuses one MCP session across calls, so a script of several queries does not start
a server process per invocation, and re-initializes by itself if the container
restarted. `cbm --help` has more examples.

`--project` is filled in with `var-www-html`, which is what CBM derives from the
container mount path — so it holds whatever your DDEV project is called. It would
only be wrong if you indexed under a custom `--name` or from a subdirectory, and in
that case the server answers with the names it does have; `CBM_PROJECT` overrides the
default.

### Other MCP clients

Any client that speaks MCP over HTTP can use the same endpoint. From inside the
project's Docker network:

```
http://codebase-memory:9750/mcp
```

and from your host, through the DDEV router:

```
https://<project>.ddev.site:9761/mcp
```

Both are the same server and the same graph. A host-side client is configured with
that URL wherever it accepts a remote or HTTP MCP server — for example a
`{"type": "http", "url": "..."}` entry, or a UI that asks for an endpoint URL.
Verified with a full `initialize` / `tools/list` / `tools/call` exchange from the
host; whether a specific desktop client can reach a locally-resolved `*.ddev.site`
address depends on that client, since some fetch remote MCP servers from their own
cloud rather than from your machine.

Set `CBM_BRIDGE_TOKEN` to require `Authorization: Bearer <token>` on `/mcp` if you
would rather it were not open on the router. The graph UI stays reachable either
way, since a browser cannot send that header.

## How it works

`codebase-memory-mcp` speaks **stdio MCP only** — it has no HTTP transport of its
own (its `--port` flag serves the graph UI, not the protocol). A small bridge
shipped with this add-on puts MCP Streamable HTTP in front of it, so agents
register it with a plain URL instead of a command:

```
┌───────────────────────┐              ┌──────────────────────────────────────┐
│ agent CLIs (in DDEV)  │              │ codebase-memory container            │
│ host MCP client       │              │                                      │
│ browser               │ ── HTTP ───► │ mcp-http-bridge.py                   │
└───────────────────────┘              │   ├─ /mcp  → one codebase-memory-mcp │
                                       │   │           process per session    │
                                       │   └─ /      → graph UI (proxied)     │
                                       │                                      │
                                       │ graph cache (named volume)           │
                                       └──────────────────────────────────────┘

both mount the project at /var/www/html, so graph paths resolve on either side
```

The bridge is Python **standard library only** — no pip packages, so nothing
third-party sits between an agent and your source. It speaks the parts of the
transport this server needs and refuses the rest explicitly: `POST /mcp` for
messages, `DELETE /mcp` to end a session, `GET /mcp` answers `405` (this server
sends no server-initiated messages, so there is no SSE stream to open), and
`GET /health` backs the container healthcheck. Everything else is proxied to the
graph UI, so one port serves both and the add-on claims no extra host ports.

**Every MCP session gets its own `codebase-memory-mcp` process**, keyed by the
`Mcp-Session-Id` header. This is the design's load-bearing property, not an
optimization: a stdio pipe carries a single interleaved JSON-RPC stream plus
per-session `initialize` state, so a shared child process would let two agents
corrupt each other's traffic. All sessions share one coordination daemon and one
graph cache. Idle sessions are reaped, and their child process with them.

Both containers mount the project at the **same path** (`/var/www/html`), so the
file paths stored in the graph resolve identically on both sides.

Agents inside the project reach the endpoint over the Docker network, so there are
no credentials to distribute; it is also published on the DDEV router for host-side
clients and for the UI. Set `CBM_BRIDGE_TOKEN` to require
`Authorization: Bearer <token>` on `/mcp` if you would rather it were not open
there.

### What gets written to your project

| File | Purpose |
|------|---------|
| `.mcp.json` | Claude Code MCP registration (`codebase-memory` key only) |
| `opencode.json` | OpenCode MCP registration (`codebase-memory` key only) |
| `.cbmignore` | Excludes `.ddev/` from the graph — created only if absent |
| `.codebase-memory.json` | Drupal projects only: indexes `.module`/`.install`/`.theme` as PHP — created only if absent |

Both JSON files are shared with other add-ons, so registration **merges**: other
MCP servers, your `model` setting, and anything else you added are preserved, and
`ddev add-on remove` deletes only the `codebase-memory` key.

Committing `.mcp.json` and `opencode.json` is a good idea — teammates then get the
server without extra setup.

The `.cbmignore` excludes `.ddev/` because DDEV's config directory contains shell
scripts that would otherwise be indexed as application code and appear in search
and trace results.

### Drupal file extensions

On a Drupal or Backdrop project, install writes a `.codebase-memory.json` mapping
`.module`, `.install`, `.theme`, `.profile`, `.engine` and `.inc` to PHP. The indexer
decides how to parse a file from its extension, so without that mapping every hook,
schema and update function, and every theme preprocessor is missing from the graph —
on a test project those files contributed **0 of 0** indexed functions before the
mapping and **7 of 7** after, call edges included.

Other project types get nothing, deliberately. WordPress, TYPO3 and Laravel keep their
code in `.php`, which is indexed already — `.blade.php` included, since it ends in
`.php`. Twig templates are left out because they define no functions or classes, so
mapping them would add file nodes and little else.

Delete or edit the file if you disagree; it is only created when absent. To check what
the graph is missing in your own project, ask for
`check_index_coverage --paths <file>` — `not_tracked` means the indexer skipped it.

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
| `CBM_VERSION` | `v0.10.3` | The `codebase-memory-mcp` release to install; `latest` or any tag (rebuild required) |
| `CBM_BRIDGE_TOKEN` | *(unset)* | Require `Authorization: Bearer <token>` on `/mcp` (the UI stays open) |
| `CBM_UI_KEEPER` | `true` | Open a short-lived session so the graph UI works with no agent running |
| `CBM_UI_KEEPER_IDLE` | `900` | Seconds of no UI traffic before that session is released |
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

There is nothing to enable, and no agent needs to be running. Upstream, the UI
belongs to a coordination daemon that exists only while an MCP session does; the
bridge opens a short-lived session of its own on the first UI request and releases
it once nobody is looking, so browsing the graph works on a project where you never
start an agent at all. Expect the first page load after an idle spell to take a
moment while that comes up.

The bridge reverse-proxies it, rewriting the `Host` and `Origin` headers on the way
through. That is what makes a `*.ddev.site` URL work at all: `codebase-memory-mcp`
binds the UI to loopback and refuses any other `Host`, and any `Origin` but its own,
as DNS-rebinding protection. Browsers send `Origin` for the UI's assets, so without
that second rewrite the page loads blank while every request still looks healthy to
`curl`.

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

[ddev-ai-workspace](https://github.com/trebormc/ddev-ai-workspace) by
[@trebormc](https://github.com/trebormc) turns a DDEV project into a full AI
development environment: Claude Code and OpenCode CLIs in their own containers, a
headless Playwright browser, git-backed task tracking, shared agent configuration
and more. If you want AI agents in DDEV, start there — this add-on then gives those
agents a knowledge graph of the codebase to query instead of grepping their way
through it.

```bash
ddev add-on get trebormc/ddev-ai-workspace
ddev add-on get IT-Cru/ddev-codebase-memory-mcp
ddev restart
```

Either order works, and there is nothing to configure. This add-on declares no
dependency on the workspace and modifies nothing the workspace owns — it registers
itself in the two project-root config files the agent CLIs already read, the same
way `ddev-playwright-mcp` does. Remove either add-on and the other keeps working.

If you install the workspace *after* this add-on, run `ddev restart` so the
registration is re-checked.

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
| Graph UI says "not running" | The daemon could not be started — check `ddev cbm logs`, and that `CBM_UI_KEEPER` is not `false` |
| Graph UI slow on first load | Expected: the daemon is starting. Subsequent loads are immediate |
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
