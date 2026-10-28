# Connecting an MCP server

Every other kind of registry entry makes you describe the target. You write
down its address, what it accepts, what comes back, and which capability
names a plan may ask it for — and all of it is wrong the moment the target
changes, because `config.json` has no way of knowing.

A **Model Context Protocol** server describes itself. KIO1 asks it what tools
it has; the answer includes each tool's name, what it does, and a JSON Schema
for its arguments. So an MCP entry says only *how to start the server*, and
everything else arrives from the server.

```json
"Workspace": {
    "type": "mcp",
    "description": "Reads and writes files in the workspace.",
    "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/work"]
}
```

That is the whole entry. Probe it and the registry holds fourteen
capabilities — `read_text_file`, `write_file`, `search_files`, `edit_file`,
and ten more — none of which anyone typed.

## Why this kind exists

The other three kinds each need a schema someone invented:

| Kind | What you must describe | Who keeps it correct |
| --- | --- | --- |
| `kio` | endpoint, path, capabilities | you |
| `http` | URL, method, headers, body, `result_path`, `health_url`, capabilities | you |
| `cli` | argv, cwd, env, capabilities | you |
| **`mcp`** | **how to start it** | **the server** |

MCP is also not KIO1's invention, which is the other half of the point. There
is an ecosystem of servers already written — filesystems, git, databases,
issue trackers, browsers, cloud APIs — and any of them is three lines of
configuration away from being a workflow target.

## The two ways to reach one

**As a child process (stdio).** Almost every published server works this way:
KIO1 starts it and talks newline-delimited JSON on its pipes.

```json
"Git": {
    "type": "mcp",
    "description": "Reads and writes the repository under analysis.",
    "command": ["uvx", "mcp-server-git", "--repository", "/work"],
    "cwd": "/work",
    "env": {"GIT_AUTHOR_NAME": "KIO1"}
}
```

**Over HTTP.** A remote or shared server is a URL.

```json
"Issues": {
    "type": "mcp",
    "description": "Reads and files issues.",
    "url": "https://mcp.internal.example/mcp",
    "headers": {"Authorization": "Bearer ${ISSUES_TOKEN}"}
}
```

An entry names one or the other, never both — which one it is decides how the
server is contacted. `${NAME}` in a header or an environment value is read
from KIO1's environment, so a token never enters `config.json`.

On Windows, a server published as an npm package needs `npx.cmd` rather than
`npx`: the command runs without a shell, and `npx` with no extension is a
shell builtin resolution that does not exist there.

## What the planner sees

The generated catalog lists each server's tools underneath it, with what each
one takes:

```
- **Workspace** _(MCP server)_ — Reads and writes files in the workspace.
  - `read_text_file` — Read the complete contents of a file as text. · arguments: path (string, required), tail (number), head (number)
  - `write_file` — Create a new file or completely overwrite an existing one. · arguments: path (string, required), content (string, required)
  - `search_files` — Recursively search for files matching a pattern. · arguments: path (string, required), pattern (string, required), excludePatterns (array)
```

A tool's name **is** the capability value. A plan addresses one the same way
it addresses any other target:

```json
{
    "step_id": "s2",
    "agent_id": "Workspace",
    "capability": "write_file",
    "task": "Record the review note",
    "arguments": {"path": "/work/REVIEW.md", "content": "…"}
}
```

## Free text and structured arguments

This is the one genuine seam, and it is worth understanding rather than
discovering.

A KIO agent is told what to do in prose: `"task": "Diagnose the null
dereference in the payment module"`. An MCP tool takes named arguments:
`{"path": "app/payment.py", "edits": [...]}`. Prose is not arguments, and no
amount of string handling turns one into the other.

KIO1 tries three things, in order:

1. **The plan said.** A step's `arguments` object is passed through. The
   planner can fill it in because the catalog showed it the schema — it is
   completing a form it has actually seen. This is the path to be on, and the
   system prompt asks for it.
2. **There is only one thing it could mean.** A tool whose schema requires
   exactly one string gets the step's `task` as that string. A great many MCP
   tools are shaped this way — a query, a path, a command — and demanding
   ceremony for them would cost more than it buys.
3. **Nothing is needed.** A tool with no required arguments is called with
   none.

Anything else fails the step, naming what the tool wanted:

```
Workspace.edit_file needs arguments (path (string, required), edits (array,
required), dryRun (boolean)), and the step carried only a free-text task. The
plan must include an 'arguments' object for this tool.
```

That refusal is deliberate. Inventing a `path` for a tool that overwrites
files is worse than a step that says what it was missing.

Keep writing `task` even when a step carries `arguments`: it is what a person
reads in the run's history, and what the next step's prompt is given.

## Probing, and what a probe changes

A probe of an MCP server is a `tools/list`. Reachability and discovery are
the same round trip, because a server that answers the listing is by
definition up.

The listing becomes the entry's capabilities. That has three consequences
worth knowing:

- **A server can gain or lose a tool while KIO1 runs.** The background
  refresh picks it up; nothing needs restarting and nothing needs editing.
- **An unprobed MCP entry advertises nothing specific.** It is in the
  registry, but the catalog lists no tools under it and a step addressed to
  it is refused rather than guessed at. Probing is not optional for this kind
  the way it is for a REST API with no `health_url`.
- **Declaring `capabilities` turns them into an allow-list.** Name some and
  only those tools are offered; name none and everything the server reports
  is. Naming none is the common case.

```json
"Workspace": {
    "type": "mcp",
    "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/work"],
    "capabilities": ["read_text_file", "list_directory", "search_files"]
}
```

That entry is a read-only view of a server that can also write. An allow-list
matching nothing is reported as a failure naming what the server *does* have,
rather than as a target that quietly offers nothing.

## Sessions

Starting an MCP server is the slow part — `npx` fetching a package takes
seconds — and calling one is fast. So a session is opened once and reused:
the probe starts the server, and every step afterwards talks to the same
process. Three consecutive steps against a filesystem server take single-digit
milliseconds each.

Calls to one server are serialized. A stdio server has one pair of pipes, and
two steps of a parallel level writing to it at once would interleave their
frames. Different servers still run concurrently.

A session that dies is discarded and reopened on the next use, so a server
that crashed does not make its target permanently unusable. Stopping KIO1
stops every server it started.

## Trying one before you save it

The console's **Check connection** button (and **Show its tools** in the Try box) reports what an MCP entry can do
before it is added:

```
Workspace (Tool server) offers 14 tools it reported itself.
Tried: npx -y @modelcontextprotocol/server-filesystem /work
It answered.

  • read_text_file(path (string, required), tail (number), head (number))  Read the complete contents of a file…
  • write_file(path (string, required), content (string, required))  Create a new file or completely overwrite…
```

From the command line:

```bash
curl -s -X POST http://localhost:8101/registry/agents/probe -H "Authorization: Bearer $KIO1_REGISTRY_TOKEN" -H "Content-Type: application/json" -d '{"agent_id":"Workspace","entry":{"type":"mcp","command":["npx","-y","@modelcontextprotocol/server-filesystem","/work"]}}'
```

## In a container

An MCP server started with `command` runs **inside KIO1's container**, so the
program has to be there. `npx` and `uvx` are not in the image by default; a
deployment that wants stdio servers either adds them to `deploy/Dockerfile`
or reaches a server over `url` instead.

`KIO1_AGENT_HOST` applies to a `url` entry the same way it applies to
everything else, and only to loopback addresses. A `command` entry has no
address to rewrite.

## Known gaps

- **Only tools.** MCP also defines resources, prompts, sampling, and
  elicitation. A workflow step maps onto a tool call, and the rest have no
  place in the plan schema yet.
- **No progress or cancellation.** A long tool call is waited on until the
  step's timeout, and a cancelled workflow does not tell the server.
- **The client is KIO1's own.** The wire protocol is implemented here rather
  than taken from the official SDK, which is async where KIO1's executor is
  threads. `McpSession` is the boundary, so swapping the inside is a
  contained change.

---

See also: [Connecting your own module](connecting-a-tool.md) for the `http`
and `cli` kinds, [The live directory](live-directory.md) for probing and the
registration API, and [The console](console.md) for the page all of this is
administered from.
