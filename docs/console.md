# The console

A web page for administering KIO1: what it can reach, what it has been doing,
and what you want it to be able to reach. Served by the Workflow API itself
at `/`.

```bash
docker compose -f deploy/compose.yaml up -d --build
# then open http://localhost:8000/
```

Or from source, without containers:

```bash
uvicorn app:app --port 8000
```

## Finding your way around

| | |
|---|---|
| **Menu** | Three pages: **Chat**, **Agents**, **Settings**. *How to use* is the `?` at the top right. |
| **Status** | The dot at the top right is green when the service answers, red when it does not; beside it, the model that plans. Hover for the provider and the workspace; click to change the model. |
| **Theme** | Light, dark, or *follow the system*, from the switch at the bottom of the menu. Remembered in this browser and applied before the page paints, so there is no white flash in dark mode. |
| **Collapse** | The button beside the logo collapses the menu to icons; also remembered. |
| **Ctrl K** | Opens the command palette: go to a page, start a new chat, open a conversation, add or edit a target, switch theme. Type to filter, arrows to choose, Enter to run. |
| **Enter** | Sends the message; Shift+Enter starts a new line. |
| **Addresses** | Each page has its own — `#/run`, `#/targets`, `#/settings`, `#/help` — and an open conversation adds its id, `#/run/<id>`, so a reload or a shared link opens the same place. |
| **Copy** | Every code block and output has a copy button on hover; an answer has one below it; the session id in the run details copies on click. |

Confirmations and results appear as dialogs and short messages in the
corner, rather than as browser pop-ups.

## Chat

The page is still addressed as `#/run`.

A chat. The conversations are on the left, newest first; **New chat** starts
one. On a narrow screen the list is behind the button at the top left.

Each message is one run. What you write goes to the planning model together
with a generated catalog of the usable targets and the rules in the system
prompt. The model answers with a plan — steps, each naming a target and one of
its capabilities — and KIO1 dispatches it. Under your message:

- **The plan card** — the run's status, its steps as they start and finish,
  and what it is doing meanwhile ("reading the request · 23 s", "waiting for
  your approval"). It is open while the run works and when it failed; a
  run that went well folds to one line ("completed · 8 steps · 90 s"), and a
  click opens it.
- **Approve this plan?** or **a question**, when the run needs you. They
  appear below the last message, and the run waits for them.
- **The answer**, with the targets it came from as small tags.

A later message continues the conversation: the last three runs go with it,
so "now add type hints" knows which function. How much goes along, and why,
is in [review.md](review.md#follow-up-requests).

The examples on an empty chat are only the ones a ready agent can do.

Below an answer: copy it, ask the same again, or open the run details.

A run that failed says why in one plain sentence ("The model took too long.
A smaller or a cloud model is faster — see Settings."); the error itself is
under **Details**. A run that was in progress when KIO1 stopped is closed at
the next start and says so, instead of showing "working…" for ever.

### Choosing targets and attaching files

Type `@` (or press the @ button) for a list of the enabled targets; arrows
and Enter pick one. The name stays in the message as plain text —
`@CodeReviewer what does this do?` — and KIO1 tells the planner to use the
targets named that way, whatever their case. An `@` in an earlier message or
inside an attached file chooses nothing.

The paperclip, or dropping files on the conversation, attaches them (2 MB
each, up to 8 per message). They are stored under `data/attachments/`, and
every step is told where in `data.attachments`, for targets that read files.
A text file's content also goes into what the planner reads — up to 12 000
characters in all, smallest files first — so it can copy the code a target
needs into the target's task; a target that only reads text, such as an LLM
agent, would otherwise never see the file.

Three things are still where most failed runs come from:

- A capability the catalog does not list cannot be asked for.
- The model decides the order, including which step consumes which. A step
  that needs an earlier step's output has to say so, and a small model
  routinely does not.
- **Code pasted into a message is not sent to an agent as a file.** It is part
  of the request the model reads, and the model copies what a target needs
  into that target's task. The code agents analyse on disk comes from the
  workspace on the Settings tab.

The header shows which model will write the plan; its tooltip names the
workspace the agents will be pointed at.

### Run details

The panel button at the top right opens a drawer with everything the chat
leaves out, fed by `GET /workflow/{session_id}/events` (Server-Sent Events):

- **What the model is writing** — the plan, token by token, as the model
  produces it, then the answer. A reasoning model's thinking, when it is
  allowed to think, is in a collapsed section above it.
- **Activity** — the catalog it was given, the plan once parsed (with each
  step's target, dependencies, and arguments), every step starting and
  finishing with its output, and KIO1's own log lines for the run. Untick
  *log lines* to hide those.
- **Steps** — what was recorded for each step, with its output.

A local model reads the whole prompt before it writes a word; on a computer
without a GPU that is 30–60 silent seconds. The plan card counts them, so a
slow start is not mistaken for a hang.

The drawer shows the conversation's latest run, or the one whose details
button was clicked, and replays its stream from the start. Only runs of the
current process are held, the latest 50; a run from before a restart has no
live record, and its outcome is in the steps table.

### How the page is built

`static/app.js` loads ES modules from `static/js/` — no build step, what is on
disk is what runs: `chat.js` (this page), `live.js` (the drawer and the
stream), `targets.js`, `settings.js`, `setup.js`, `palette.js`, `shell.js`
(menu, tabs, theme), and `core.js` (requests, escaping, toasts, dialogs).

## Agents

Everything the [live directory](live-directory.md) knows, as cards in three
groups: **Ready**, and folded below it **Not answering** and **Switched off**.
A card shows the agent's name, what kind it is (KIO, Web API, Command, Tool
server, AI), what it does, and up to four of its capabilities. The dot is its
state; hovering it gives the last check's error, and hovering the name gives
where it came from and how often each capability worked lately. Edit and
remove appear when the pointer is on the card; the switch takes it out of the
catalog without forgetting it. The search box opens every group. The page is
still addressed as `#/targets`, and the API still says target.

Under the cards, the same information as the directory reports it:

```text
Id           Kind  Origin        Status        Capabilities              Recent
KIO3         kio   config        READY         requirements_engineering  1/1 · rate 0.667 · 23 ms
KIO5         kio   config        UNREACHABLE   synthetic_data_generation —
             http://127.0.0.1:8105/health is unreachable: timed out
Pytest       cli   config        READY         test_execution            —
MyLogSearch  http  registration  UNCHECKED     log_search                —
```

Two things worth reading off that: `KIO5` says *why* it is not being planned
for, and `MyLogSearch` is `unchecked` rather than `ready` because it is an
HTTP tool with no `health_url` — nothing was probed, so nothing is claimed.

### Adding, editing, removing

**Add an agent** asks first what is being connected, in plain words:

| Card | Kind | The form asks for |
|------|------|-------------------|
| **Instructions** | `llm` | a name, what it does, its instructions |
| **Web address** | `http` | a name, what it does, the address and method |
| **Tool server** | `mcp` | a name, and the command that starts it or its address |
| **Command** | `cli` | a name, what it does, the command as one line |

Each card has examples that fill the form in one click — a code reviewer,
the Open-Meteo weather API, git history, a filesystem server — so a first
agent needs nothing to be known. In an address, `{arguments.city}` marks an
input: the form turns each one into an argument the planner fills, and the
Try box gets a field for it. The capability is made from the name; **More**
changes it, and for a web address picks the part of the reply to keep. A
command is written on one line, as in a terminal, and split into the list
the entry needs. **Form / JSON** switches to the definition itself, and
anything else — a KIO service, headers, a body template — starts from
*paste a JSON definition*.

Saving probes it straight away and the result comes back:
`MyAgent → ready`, or `MyAgent → unreachable` with the reason.

### Try

The box under the form asks the agent one thing, the way a plan's step
would — workspace and arguments included — and shows what came back, or
why it failed. Nothing is saved and nothing is recorded against it, so a
draft can be tried until it does what it should
(`POST /registry/agents/try`). For a tool server the box lists the tools it
reports instead; when the program that starts it (`npx`, `uvx`) is not
installed, it says where to get it.

**Edit** opens a target in the same form — or as JSON, for a kind the
form does not show. The whole entry
is replaced rather than merged, because a merge cannot express *removing* a
field — dropping a tool's `health_url` would be impossible.

**Remove** takes it out of the directory, and out of the configuration file
when that is where it came from.

### Check connection

**Check connection** answers "would this work?" without adding anything. It
reads what is in the box, checks that it parses, and dials the address —
then reports three things:

- what it understood: `WeatherForecast (Web API) offers weather_check.`,
- **which address it actually dialled**, and
- whether that address answered.

The middle line is the point. What gets dialled is not always what you typed:
`KIO1_AGENT_HOST` and `KIO1_AGENT_ENDPOINT_<AGENT>` redirect addresses
per-deployment, and when they do, the panel says so explicitly —

> You wrote `http://127.0.0.1:11434/`, and this deployment dials
> `http://host.docker.internal:11434/` instead (`KIO1_AGENT_HOST`).

Without that line, a target you had written correctly could come back
`unreachable` with nothing in the entry to explain it.

What is probed depends on the kind: a `kio` target is asked for its health
path, an `http` tool is asked for its `health_url` (so give it one — without
it there is nothing safe to call), and a `cli` tool is checked for its
program being on this machine's `PATH`. Nothing is saved and nothing is run;
a failing test is information, not a refusal to add.

An `mcp` entry gets the most out of this button, because probing one is a
`tools/list` — the test tells you every tool the server has and what each one
takes, which is the list that becomes the entry's capabilities:

```
Workspace (Tool server) offers 14 tools it reported itself.
Tried: npx -y @modelcontextprotocol/server-filesystem /work
It answered.

  • read_text_file(path (string, required), tail (number), head (number))  Read the complete contents…
  • write_file(path (string, required), content (string, required))  Create a new file or completely…
```

See [Connecting an MCP server](mcp.md) for what that kind is and why its
entry is three lines long.

### Where changes go

By default all three **write to `config.json`**, so what you do here is still
there after a restart. That file also holds the provider, telemetry, and
policy settings, so the writer is careful with it:

- Only the `dispatch.agents` object is replaced. Every other key is read and
  written back as it was found, in its original order.
- The write is atomic where the filesystem allows it — a temporary file in
  the same directory, then a replace — so a crash halfway through leaves the
  original intact.
- The previous contents are kept alongside as `config.json.bak`.

In a container the configuration arrives as a **single-file bind mount**, and
the kernel refuses to rename over a mount point. There the file is rewritten
in place instead, which is not atomic — the backup is what a crash mid-write
would be recovered from. Saving from the console works either way; you do not
have to do anything.

The panel header shows which file is being written. Untick **Save to the
configuration file** to add something for this process only.

`KIO1_CONFIG_PATH` says which file that is; `KIO1_CONFIG_WRITABLE=0` turns
writing off for a deployment that ships a read-only configuration, and the
console then says so up front rather than failing when you press Add.

### Why the console may override the file

Discovery never overwrites a hand-written entry — an automatic source has no
business second-guessing a person. Editing in the console *is* a person, doing
the same thing they would have done by opening the file. So it overrides, and
the result is written to the file, which keeps one source of truth rather than
two.

## Workflows

A box to send a request, the recent runs, and — for whichever run you click —
its steps with status, duration, error, and the agent's output. The page
refreshes every four seconds, so a running workflow fills in as it goes.

## How to use

A tab inside the console explaining what a target is, the three kinds with
copy-paste examples, the placeholders they can carry, how an agent can
register itself instead, and what every status means. Each example has a
**Put this in the form** button.

It exists so someone plugging in their own agent does not have to find this
page first.

## Settings

Only the model is in view; everything else is folded under **Advanced**
(answer model, temperature and timeout, workspace, the administration token).
All of it is saved to `config.json` and takes effect from the next run —
nothing restarts.

**Planning model.** Which provider and which model write the plan. Pick a
provider — Ollama on this computer, OpenRouter, OpenAI, or Anthropic — and the
model field offers what it has: type part of a name to filter, which matters
for OpenRouter's several hundred. For a provider that cannot be asked, the
name is typed and the page says why there is no list.

A provider that needs a key shows a key field. A pasted key is tried first and
written to `.env` (which git ignores) only if it works; a wrong one is refused
on the spot and nothing changes. The switch takes effect from the next run,
without a restart.

This is the setting worth reaching for first when a run produces a plan that
is *valid but wrong* — steps in an impossible order, a capability that does not
fit the request. Following the system prompt's rules is the hard part of
planning, and a small model does not. Changing the model here also warms it in
the background, so the next run is not charged for reading it into memory.

**Temperature** and **planning timeout** sit beside it. Planning wants
repeatable output, so 0–0.3; and the timeout bounds the model's answer, not the
agents' work, so raise it when you move to a slower model.

**Workspace.** The repository and entry point every execution message carries,
so an agent knows what code to analyse without being told in prose.

> **The path is resolved by the agent, not by KIO1.** An agent in its own
> container has its own filesystem. `E:\projectspp` is real here and absent
> there, and the agent answers "not found" about a file you can see. Use the
> path inside the agent's container, and check the code is mounted at it.

## The registry token

The registry endpoints are closed until `KIO1_REGISTRY_TOKEN` is set on the
server. The console asks for it with the **Administration token** button
(Settings → Advanced, opened by itself when the token is missing) and keeps
it in `sessionStorage` — this browser tab only, gone when the tab closes,
never written to disk and never sent anywhere but this service.

Without a token the Agents page shows the server's own explanation of what to
set. Running workflows does not need one.

## What it is not

Three files — `index.html`, `app.css`, `app.js` — with no build step, no
framework, and **no external requests**: no CDN, no font host, no analytics.
It talks only to the service that served it, so it works on a machine with no
route to the internet, which is where an orchestrator like this usually
lives. There are tests that keep it that way.

Everything a viewer sees that someone else wrote — an agent's description, an
error message, an output payload — is escaped before it reaches the page.

## Endpoints it uses

All of them are ordinary API endpoints; the console is just one caller.

| Endpoint | Purpose |
|----------|---------|
| `GET /registry/agents` | Targets, their status, their history, and their definitions |
| `POST /registry/agents` | Add from a manifest; `persist` decides whether it is saved |
| `PUT /registry/agents/{id}` | Replace one target's definition |
| `DELETE /registry/agents/{id}` | Remove it, from the file too when it is in there |
| `GET /registry/config` | Which file saves go to, and whether they can |
| `POST /workflow/run` | Send a message; `conversation_id` continues a conversation, without it a new one starts; `attachments` names uploaded files |
| `POST /files` | Upload a file to attach, as `{name, content_base64}`; answers with its `attachment_id` |
| `GET /workflow/{session_id}/waiting`, `POST …/approval`, `POST …/answer` | What a run waits for, and the reply |
| `POST /conversations` | Open an empty conversation (New chat) |
| `GET /conversations?limit=N` | Conversations, the one last written to first |
| `GET /conversations/{id}` | One conversation with every run in it, oldest first |
| `DELETE /conversations/{id}` | Delete it and its runs (registry token) |
| `GET /workflows?limit=N` | Recent runs, newest first |
| `GET /workflow/{session_id}` | One run with its steps |
| `GET /workflow/{session_id}/events` | The run's live stream (Server-Sent Events); resumes from `Last-Event-ID` or `?after=` |
| `GET /settings`, `PUT /settings` | Provider, model, key, temperature, timeout, workspace |
| `GET /settings/models?provider=…` | A provider's models, before switching to it |

`GET /workflows` answers with an empty list when session recording is off — a
console asking what has run wants an empty answer when the answer is nothing,
not an error. It answers `503` when the Session Manager is configured but
unreachable, which is a different thing.

## Not here yet

- **No approval.** A workflow stopped by `block_on_review_required` shows as
  stopped, and there is no button to approve it and carry on. That needs the
  executor to be able to resume a plan part-way through, which it cannot do
  yet — see [Policy gates](policy-gates.md).
- **No editing of anything but agents.** Gates, discovery sources, and health
  settings are still file-only.
- **No lineage view.** The Session Manager records which artifact fed which
  step; the console does not draw it.
- **No authentication for running workflows.** Only the registry endpoints
  are token-gated. Anyone who can reach the service can start a run.
- **Polling, not streaming.** Four seconds is fine for a handful of
  operators and wrong for a wall display.
- **No concurrency control on saves.** Two people editing the same target at
  once, or someone editing the file by hand while the console saves, and the
  last write wins.
