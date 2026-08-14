# Codebase graph — instructions for AI agents

<!-- ddev-generated -->

This project has a queryable knowledge graph of its own source: functions, classes,
call chains, imports and HTTP routes, kept current automatically. Query it instead of
reading or grepping your way through files.

## Why this matters for cost

Reading files to answer a structural question is the expensive path. "Who calls this
function?" by grep means several searches and several whole files in your context; the
same question as one `trace_path` is a few dozen tokens. That difference — graph
instead of files — dwarfs every other saving available here.

Second, each tool call sends the whole conversation through the model again. Four
narrow calls cost four inferences; one `query_graph` that answers the question outright
costs one. Prefer the single query.

You do not need to worry about trimming results. The server already answers in a
compact tree format.

## Start here

Call `get_graph_schema` once at the start of a structural task. It reports the node
labels, edge types and counts that actually exist in this project, which tells you what
is worth asking for.

## Reading the output

The query tools answer in a compact tree, not JSON — a group line per file, then one
row per symbol:

```
total: 4
results: 4  (rows: name label lines in out; qn = group prefix + "." + name)
var-www-html.src.app (src/app.js):
  bootstrap Function 1-1 1 1
  createRouter Function 2-2 1 0
has_more: false
```

Build a qualified name by joining the group prefix and the row name with a dot:
`var-www-html.src.app.createRouter`. Some tools — `get_code_snippet` among them —
still answer in JSON, so check before piping rather than assuming either format.

## Which tool answers which question

| Question | Tool |
|---|---|
| What is in this codebase? Languages, packages, routes, hotspots | `get_architecture` |
| What exists matching a name or label? | `search_graph` |
| Who calls this, and what does it call? | `trace_path` |
| Anything relational, aggregated or more than one hop | `query_graph` |
| Show me the source of this symbol | `get_code_snippet` |
| Where does this string appear? | `search_code` |
| What did the last commit affect, and what is the blast radius? | `detect_changes` |
| What did *not* make it into the graph? | `check_index_coverage` |

Always pass `limit` to `search_graph`. Ask `query_graph` for the properties you need
rather than whole nodes.

## Chaining steps in one call

For work that fans out — every class, then each one's source — a shell script costs one
round-trip instead of one per step. `cbm` runs any graph tool from inside this
container:

```bash
CBM=/var/www/html/.ddev/codebase-memory/cbm
out="$($CBM search_graph --label Class)"
prefix="$(printf '%s\n' "$out" | sed -n 's/^\(var-www-html[^ ]*\) (.*/\1/p')"
for name in $(printf '%s\n' "$out" | awk '/^  [A-Za-z]/ {print $1}'); do
  "$CBM" get_code_snippet --qualified-name "$prefix.$name"
done
```

`cbm --help` has more examples, `cbm --tools` lists what is available. It needs no
credentials and talks to the same server and the same graph as your MCP tools.

Reach for this when a question fans out over many symbols. For a single question, call
the MCP tool directly — it is one round-trip either way.

## Worth knowing

- The graph project is named after the container mount path, so it is `var-www-html`.
  Tools that take `project` want that value; `cbm` fills it in for you.
- The graph is kept current automatically. If results look stale,
  `cbm index_repository --repo-path /var/www/html` re-indexes, but check first — it is
  rarely the problem.
- The graph knows structure, not intent. It will tell you what calls what; it will not
  tell you why. Read the code for that, once the graph has told you where.
