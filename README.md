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

`codebase-memory-mcp` speaks **stdio MCP only** — it has no HTTP transport (its
`--port` flag serves the graph UI, not the protocol). So the agent containers reach
it the same way they reach `web` and `beads`: over **SSH**, which pipes
stdin/stdout verbatim — exactly what a stdio transport needs.

```
┌──────────────────┐   ssh (stdio JSON-RPC)   ┌────────────────────────┐
│  claude-code     │ ───────────────────────► │  codebase-memory       │
│  opencode        │                          │   sshd                 │
│  (agent CLIs)    │                          │   codebase-memory-mcp  │
└──────────────────┘                          │   graph cache (volume) │
         │                                    └────────────────────────┘
         └── project mounted at /var/www/html ──────────┘
```

Each agent session spawns its own `codebase-memory-mcp` process in the container;
they share one coordination daemon and one graph cache.

Both containers mount the project at the **same path** (`/var/www/html`), so the
file paths stored in the graph resolve identically on both sides.

Authentication uses the shared keypair in `.ddev/.agent-ssh-keys/` — the same one
[ddev-ai-ssh](https://github.com/trebormc/ddev-ai-ssh) uses. Whichever add-on is
installed first creates it, and `sshd` in the container is restricted to public-key
auth with a forced command.

### What gets written to your project

| File | Purpose |
|------|---------|
| `.mcp.json` | Claude Code MCP registration (`codebase-memory` key only) |
| `opencode.json` | OpenCode MCP registration (`codebase-memory` key only) |
| `.cbmignore` | Excludes `.ddev/` from the graph — created only if absent |
| `.ddev/.agent-ssh-keys/` | Shared agent keypair (self-gitignored) |

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

```bash
ddev cbm ui on        # optionally: ddev cbm ui on <port>
ddev restart
```

Then open <http://127.0.0.1:9749> while an agent session is running.

The UI is served on host loopback and accepts only `localhost`/`127.0.0.1` as its
`Host`, so use the address above rather than a `*.ddev.site` URL. It is owned by
the coordination daemon, which exists only while at least one MCP session is open —
start `ddev claude-code` or `ddev opencode` first, then load the page.

Port `9749` is also the default for `codebase-memory-mcp` installed directly on
your host. If it is taken, choose another with `ddev cbm ui on 9850`. `ddev cbm ui
off` removes the port mapping again.

## Use with ddev-ai-workspace

```bash
ddev add-on get trebormc/ddev-ai-workspace
ddev add-on get IT-Cru/ddev-codebase-memory-mcp
ddev restart
```

Either order works. This add-on has no dependency on the workspace: it shares the
`.ddev/.agent-ssh-keys/` keypair with `ddev-ai-ssh`, whichever add-on creates it
first, and registers itself in the project-root config files both clients already
read.

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

If an agent reports the MCP server failing to start, run the registered command by
hand and read the error:

```bash
ddev exec bash -c "$(jq -r '.mcpServers["codebase-memory"] | ([.command] + .args) | @sh' .mcp.json)" </dev/null
```

| Symptom | Cause |
|---------|-------|
| `Permission denied (publickey)` | Missing keypair. Re-run `ddev add-on get`, then `ddev restart` |
| Agent shows no `codebase-memory` tools | Entry missing from `.mcp.json` / `opencode.json` — `ddev restart`, and check `CBM_REGISTER_MCP` |
| Empty query results | Index not finished. `ddev cbm logs`, or `ddev cbm index` |
| Graph UI returns 403 | Reached via a `*.ddev.site` URL; use `http://127.0.0.1:<port>` |
| Graph UI connection refused | No agent session open, so no daemon owns the UI |
| `port is already allocated` on start | The UI port is taken. `ddev cbm ui on <other-port>` |

Container logs, including indexing:

```bash
ddev cbm logs -f
```

## Removal

```bash
ddev add-on remove codebase-memory-mcp
```

This deregisters the server from both config files and removes the add-on's files.
The graph cache volume and the shared SSH keypair are kept on purpose — other AI
add-ons use the keypair, and re-indexing is expensive. To drop the cache:

```bash
docker volume rm ddev-<project>-codebase-memory-cache
```

## Credits

- [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) by DeusData
- Built from [ddev/ddev-addon-template](https://github.com/ddev/ddev-addon-template)
- Integrates with [ddev-ai-workspace](https://github.com/trebormc/ddev-ai-workspace) by trebormc

**Contributed and maintained by [IT-Cru](https://github.com/IT-Cru)**
