# Codebase graph — instructions for AI agents

<!-- ddev-generated -->

This project has a queryable knowledge graph of its own source: functions,
classes, call chains, imports and HTTP routes. Use it instead of reading or
grepping your way through files.

## Start here

Call `get_graph_schema` once at the start of a structural task. It reports the node
labels, edge types and counts that actually exist in this project, which tells you
what is worth asking for.

## Prefer one query over many calls

For anything multi-hop, use `query_graph` with an explicit `RETURN` — one call, and
only the fields you asked for:

```
MATCH (caller:Function)-[:CALLS]->(f:Function {name: "handle"})
RETURN caller.name, caller.file_path
```

That is cheaper and clearer than `search_graph` followed by `trace_path` followed
by `get_code_snippet`, where every intermediate result has to pass through your
context to reach the next step.

## Keep results small

Graph results carry per-node fields nobody reads — fingerprints (`fp`), metric
vectors (`sp`), a dozen complexity counters. Pulling a wide result set into context
to pick out three names is waste.

- Always pass `limit` to `search_graph`.
- Ask `query_graph` for the specific properties you need, not whole nodes.
- When you want a shape the tools do not return directly, shell out to `cbm` and
  filter with `jq` — then only the answer enters your context:

```bash
/var/www/html/.ddev/codebase-memory/cbm search_graph --label Function \
  | jq '[.results[] | {name, file_path, out_degree}] | sort_by(-.out_degree) | .[:5]'
```

`cbm --help` lists more examples, `cbm --tools` the available tools. It needs no
credentials and talks to the same server and the same graph as your MCP tools.

## Which tool answers which question

| Question | Tool |
|---|---|
| What is in this codebase? Languages, packages, routes, hotspots | `get_architecture` |
| What exists matching a name or label? | `search_graph` |
| Who calls this, and what does it call? | `trace_path` (or `query_graph` for more than one hop) |
| Show me the source of this symbol | `get_code_snippet` |
| Anything relational, aggregated or multi-hop | `query_graph` |
| Where does this string appear? | `search_code` |

## Worth knowing

- The graph project is named after the container mount path, so it is
  `var-www-html`. Tools that need `--project`/`project` take that value; `cbm`
  fills it in for you.
- The graph is kept current automatically. If results look stale, `cbm index_repository
  --repo-path /var/www/html` re-indexes, but check first — it is rarely the problem.
- The graph knows structure, not intent. It will tell you what calls what; it will
  not tell you why. Read the code for that, once the graph has told you where.
