// The Targets page: what a plan can be sent to, and the editor that adds
// and changes them. The How-to-use page's samples open in that editor.

import { $, api, ask, esc, hooks, setConnection, setRows, state, statusChip, toast } from "./core.js";
import { renderMarkdown } from "./markdown.js";
import { showTab } from "./shell.js";

const NEW_ENTRY = `{
  "agent_id": "MyAgent",
  "description": "What it does, in the words the planner should read.",
  "endpoint": "http://127.0.0.1:9000",
  "capabilities": ["my_capability"]
}`;

// ── targets ─────────────────────────────────────────────────────────────────

// What each kind is, in words someone who never read the docs would use.
const KIND_NAMES = {
  kio: "KIO", http: "Web API", cli: "Command", mcp: "Tool server", llm: "AI",
};

// How many capabilities a card shows before "+3": the rest are in its tooltip.
const CAPS_SHOWN = 4;

/** How often each capability worked lately, for a tooltip. */
export function historyLine(history) {
  return (history || []).map(function (item) {
    return item.capability + ": " + item.successes + "/" + item.attempts + " worked";
  }).join("\n");
}

/** "No answer from 127.0.0.1:8104" rather than the operating system's whole
 * sentence, which is in the tooltip for whoever needs it. */
export function noAnswer(error) {
  const address = /https?:\/\/([^/\s]+)/.exec(error);
  return address ? "No answer from " + address[1] : "Not answering";
}

export function agentCard(agent) {
  const id = esc(agent.agent_id);
  const off = agent.enabled === false;
  const down = !off && agent.status === "unreachable";
  const caps = agent.capabilities || [];
  const dot = off ? "idle" : down ? "bad" : agent.last_error ? "warn" : "ok";
  const status = off ? "switched off" : agent.status + (agent.last_error ? " — " + agent.last_error : "");
  const about = [agent.origin ? "From " + agent.origin : "", historyLine(agent.history)]
    .filter(Boolean).join("\n");

  return '<article class="agent-card' + (off ? " is-off" : "") + '">'
    + '<div class="agent-head">'
      + '<span class="dot ' + dot + '" title="' + esc(status) + '"></span>'
      + '<span class="target-name" title="' + esc(about) + '">' + id + "</span>"
      + (agent.approval
        ? '<svg class="icon lock" aria-label="Asks before running"><title>Asks before running</title>'
          + '<use href="#i-lock"/></svg>' : "")
      + '<span class="kind-tag ' + esc(agent.kind) + '">' + esc(KIND_NAMES[agent.kind] || agent.kind) + "</span>"
      + '<span class="spacer"></span>'
      + '<span class="agent-actions">'
        + '<button class="icon-button" data-edit="' + id + '" title="Edit ' + id
          + '" aria-label="Edit ' + id + '"><svg class="icon"><use href="#i-edit"/></svg></button>'
        + '<button class="icon-button danger" data-remove="' + id + '" title="Remove ' + id
          + '" aria-label="Remove ' + id + '"><svg class="icon"><use href="#i-trash"/></svg></button>'
      + "</span>"
      + '<label class="switch" title="' + (off ? "Switch on" : "Switch off") + '">'
        + '<input type="checkbox" data-toggle="' + id + '"' + (off ? "" : " checked")
        + ' aria-label="Use ' + id + '"><span class="track"></span></label>'
    + "</div>"
    + (agent.description ? '<p class="agent-about">' + esc(agent.description) + "</p>" : "")
    + (caps.length
      ? '<div class="caps" title="' + esc(caps.join(", ")) + '">'
        + caps.slice(0, CAPS_SHOWN).map(function (c) { return '<span class="cap">' + esc(c) + "</span>"; }).join("")
        + (caps.length > CAPS_SHOWN ? '<span class="cap more">+' + (caps.length - CAPS_SHOWN) + "</span>" : "")
        + "</div>"
      : "")
    + (down && agent.last_error
      ? '<p class="reason" title="' + esc(agent.last_error) + '">' + esc(noAnswer(agent.last_error)) + "</p>" : "")
    + "</article>";
}

export let lastAgents = [];
const targetFilter = { text: "" };
// Which folded groups the reader opened, so a refresh does not shut them.
const openGroups = new Set();

/** The targets the search box leaves visible. */
export function visibleAgents() {
  const text = targetFilter.text.toLowerCase();

  return lastAgents.filter(function (agent) {
    if (!text) return true;

    return [agent.agent_id, agent.description, (agent.capabilities || []).join(" ")]
      .join(" ").toLowerCase().indexOf(text) !== -1;
  });
}

/** One group of cards; the ones nobody needs every day are folded. */
function agentGroup(key, title, agents, folded) {
  if (!agents.length) return "";

  const cards = '<div class="agent-grid">' + agents.map(agentCard).join("") + "</div>";
  const heading = esc(title) + ' <span class="count">' + agents.length + "</span>";

  if (!folded) return '<section class="agent-group"><h2 class="group-title">' + heading + "</h2>" + cards + "</section>";

  // A search opens everything: what was searched for must not hide in a fold.
  const open = targetFilter.text || openGroups.has(key);

  return '<details class="agent-group" data-group="' + key + '"' + (open ? " open" : "") + ">"
    + '<summary class="group-title">' + heading + "</summary>" + cards + "</details>";
}

export function renderTargets() {
  const shown = visibleAgents();
  const off = shown.filter(function (a) { return a.enabled === false; });
  const down = shown.filter(function (a) { return a.enabled !== false && a.status === "unreachable"; });
  const ready = shown.filter(function (a) { return off.indexOf(a) === -1 && down.indexOf(a) === -1; });

  const html = agentGroup("ready", "Ready", ready, false)
    + agentGroup("down", "Not answering", down, true)
    + agentGroup("off", "Switched off", off, true);

  setRows($("targets-body"), html || '<div class="empty-state"><svg class="icon"><use href="#i-empty"/></svg>'
    + esc(lastAgents.length ? "Nothing matches." : "No agents yet. Add one to get started.") + "</div>");

  $("targets-count").textContent = lastAgents.length
    ? ready.length + " of " + lastAgents.length + " ready" : "";
}

$("targets-search").addEventListener("input", function () {
  targetFilter.text = $("targets-search").value.trim();
  renderTargets();
});

// `toggle` does not bubble, so it is caught on the way down.
$("targets-body").addEventListener("toggle", function (event) {
  const group = event.target.dataset && event.target.dataset.group;
  if (!group || targetFilter.text) return;

  if (event.target.open) openGroups.add(group); else openGroups.delete(group);
}, true);

export async function loadTargets() {
  const warning = $("token-warning");

  try {
    const data = await api("/registry/agents");
    lastAgents = data.agents || [];
    warning.classList.add("hidden");
    setConnection("ok", "connected");
    renderTargets();
  } catch (error) {
    setConnection("bad", "error");
    warning.textContent = error.status === 503 || error.status === 401
      ? error.message
      : "Could not read the registry: " + error.message;
    warning.classList.remove("hidden");
    setRows($("targets-body"), "");
    $("targets-count").textContent = "";
  }
}

export async function loadSaveLocation() {
  try {
    const info = await api("/registry/config");
    state.writable = info.writable;

    $("save-location").textContent = info.writable
      ? "saves to " + info.config_path
      : "read-only";

    const warning = $("readonly-warning");

    if (info.writable) {
      warning.classList.add("hidden");
    } else {
      warning.textContent = "Saving is turned off on this deployment "
        + "(KIO1_CONFIG_WRITABLE). Anything added here lasts until the service "
        + "restarts.";
      warning.classList.remove("hidden");
    }

    $("persist").checked = info.writable;
    $("persist").disabled = !info.writable;

    const note = $("help-save-location");
    if (note) {
      note.innerHTML = "Adding, editing, or removing a target here rewrites the "
        + "<code>dispatch.agents</code> section of <code>" + esc(info.config_path)
        + "</code>. Nothing else in it is touched, the write is atomic, and the "
        + "previous contents are kept alongside as <code>.bak</code>.";
    }
  } catch {
    $("save-location").textContent = "";
  }
}

// ── the editor ──────────────────────────────────────────────────────────────

export function openEditor() {
  $("editor-panel").classList.remove("hidden");
  $("editor-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

export function closeEditor() {
  $("editor-panel").classList.add("hidden");
  startAdding();
}

export function startAdding() {
  state.editing = null;
  $("editor-title").textContent = "Add an agent";
  $("save-entry").textContent = "Add";
  $("cancel-edit").classList.add("hidden");
  $("entry").value = NEW_ENTRY;
  $("editor-result").textContent = "";
  $("probe-result").className = "notice hidden";
  fillAgentForm({});
  showKindPicker();
  $("history-row").classList.add("hidden");
}

// ── history ─────────────────────────────────────────────────────────────────

/** Offer the earlier versions of the target being edited, when there are any. */
export async function loadHistory(agentId) {
  const row = $("history-row");
  row.classList.add("hidden");

  try {
    const data = await api("/registry/agents/" + encodeURIComponent(agentId) + "/history");
    // The newest is what the editor already shows.
    const earlier = (data.versions || []).slice(1).filter(function (v) { return v.entry; });

    if (state.editing !== agentId || !earlier.length) return;

    setRows($("history-select"), earlier.map(function (version) {
      return '<option value="' + esc(version.version) + '">'
        + esc(new Date(version.changed_at).toLocaleString() + " · " + version.action)
        + "</option>";
    }).join(""));
    row.classList.remove("hidden");
  } catch {
    // A store without history (config.json) simply offers none.
  }
}

$("history-restore").addEventListener("click", async function () {
  const agentId = state.editing;
  const version = Number($("history-select").value);
  if (!agentId || !version) return;

  try {
    await api("/registry/agents/" + encodeURIComponent(agentId) + "/restore", {
      method: "POST",
      body: JSON.stringify({ version: version }),
    });
    toast(agentId + " restored.", "ok");
    closeEditor();
    loadTargets();
  } catch (error) {
    toast(error.message, "bad");
  }
});

// ── the agent form ──────────────────────────────────────────────────────────
//
// Adding an agent without writing JSON. The first question is what is being
// connected, in plain words; the form then asks only what that kind needs,
// and the JSON it makes is one click away for whoever wants to go further.

// The kinds the form can describe. Anything else — a KIO service, an entry
// with headers or a body template — opens as JSON.
const FORM_KINDS = ["llm", "http", "mcp", "cli"];

// One click to a working agent, so trying one needs nothing to be known.
const EXAMPLES = {
  llm: [
    {
      label: "Code reviewer",
      agent_id: "CodeReviewer",
      skill: "code_review",
      description: "Reviews code for bugs and risky patterns, and explains each finding briefly.",
      instructions: "You are a careful senior code reviewer. Read the code or diff in the inputs "
        + "and list concrete problems: bugs, unsafe patterns, missing error handling. For each, "
        + "give where it is and a one-line fix. If the inputs contain no code, say so.",
      try: "def average(values): return sum(values) / len(values)",
    },
    {
      label: "Requirements writer",
      agent_id: "RequirementsWriter",
      skill: "requirements_writing",
      description: "Turns a feature idea into user stories with acceptance criteria.",
      instructions: "You are a business analyst. Turn the idea in the task into three to six "
        + "user stories (As a … I want … so that …), each with two or three testable acceptance "
        + "criteria. Ask nothing; state any assumption you make.",
      try: "Customers should be able to save items to a wish list.",
    },
    {
      label: "Documentation writer",
      agent_id: "DocWriter",
      skill: "documentation",
      description: "Writes clear documentation for code or a feature, in the language of the task.",
      instructions: "You are a technical writer. Write short, clear documentation for what the "
        + "task describes: what it is for, how to use it, and one example. Write in the language "
        + "of the task. Use only facts from the task and its inputs.",
      try: "A function slug(text) that turns 'Code Reviewer!' into 'code_reviewer'.",
    },
  ],
  http: [
    {
      label: "Weather (Open-Meteo)",
      agent_id: "Weather",
      skill: "weather_check",
      description: "Current temperature and weather at a latitude and longitude.",
      url: "https://api.open-meteo.com/v1/forecast?latitude={arguments.latitude}"
        + "&longitude={arguments.longitude}&current_weather=true",
      method: "GET",
      result_path: "current_weather",
      try: "What is the weather?",
      args: { latitude: "41.01", longitude: "28.97" },
    },
  ],
  mcp: [
    { label: "Files", agent_id: "Files", command: "npx -y @modelcontextprotocol/server-filesystem {workspace}" },
    { label: "Git", agent_id: "Git", command: "uvx mcp-server-git --repository {workspace}" },
    { label: "Web pages", agent_id: "Fetch", command: "uvx mcp-server-fetch" },
  ],
  cli: [
    {
      label: "Git history",
      agent_id: "GitHistory",
      skill: "repository_history",
      description: "Reads the recent commit history of this repository.",
      command: "git log --oneline -20",
      try: "Show the recent commits",
    },
    {
      label: "Python version",
      agent_id: "PythonVersion",
      skill: "python_version_check",
      description: "Which Python version is installed on this machine.",
      command: "python --version",
      try: "Which Python is installed?",
    },
  ],
};

const COMMAND_PLACEHOLDERS = {
  cli: "git log --oneline -20",
  mcp: "npx -y @modelcontextprotocol/server-filesystem C:\\work   or   https://…/mcp",
};

/** "Code Reviewer!" → "code_reviewer": a capability from a name. */
export function slug(text) {
  return String(text || "")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

/** `git log -n "two words"` → ["git", "log", "-n", "two words"]: a command
 * as one line, the way people write it, split as a shell would. */
export function splitCommand(text) {
  const parts = [];
  const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let match;

  while ((match = pattern.exec(String(text || ""))) !== null) {
    parts.push(match[1] !== undefined ? match[1] : match[2] !== undefined ? match[2] : match[3]);
  }

  return parts;
}

/** The reverse: a command list as one line, quoting what has spaces. */
export function joinCommand(parts) {
  return (parts || []).map(function (part) {
    return /\s/.test(part) ? '"' + part + '"' : part;
  }).join(" ");
}

/** The inputs an address names: `{arguments.city}` → "city". */
export function argumentNames(url) {
  const names = [];
  const pattern = /\{arguments\.([A-Za-z_][A-Za-z0-9_]*)\}/g;
  let match;

  while ((match = pattern.exec(String(url || ""))) !== null) {
    if (names.indexOf(match[1]) === -1) names.push(match[1]);
  }

  return names;
}

function workspacePath() {
  return (state.settings && state.settings.workspace && state.settings.workspace.path) || ".";
}

export function fillAgentForm(values) {
  $("agent-name").value = values.agent_id || "";
  $("agent-skill").value = values.skill || "";
  $("agent-description").value = values.description || "";
  $("agent-instructions").value = values.instructions || "";
  $("agent-url").value = values.url || "";
  $("agent-method").value = values.method || "GET";
  $("agent-result-path").value = values.result_path || "";
  $("agent-command").value = String(values.command || "").replace("{workspace}", workspacePath());
  $("agent-name").disabled = Boolean(state.editing);
  $("try-task").value = values.try || "";
  renderTryArgs(values.args || {});
}

/** Show the fields one kind uses, and the examples for it. */
export function pickKind(kind) {
  state.agentKind = kind;

  $("kind-picker").classList.add("hidden");
  $("agent-form").classList.remove("hidden");
  $("try-box").classList.remove("hidden");
  $("editor-actions").classList.remove("hidden");
  $("kind-back").classList.toggle("hidden", Boolean(state.editing));

  document.querySelectorAll("#agent-form [data-for]").forEach(function (field) {
    field.classList.toggle("hidden", field.dataset.for.split(" ").indexOf(kind) === -1);
  });

  $("agent-command-label").textContent = kind === "mcp" ? "Command or address" : "Command";
  $("agent-command").placeholder = COMMAND_PLACEHOLDERS[kind] || "";

  setRows($("agent-templates"), (EXAMPLES[kind] || []).map(function (example, index) {
    return '<button class="example" data-example-index="' + index + '">' + esc(example.label) + "</button>";
  }).join(""));
  $("agent-templates").classList.toggle("hidden", Boolean(state.editing) || !(EXAMPLES[kind] || []).length);

  // An MCP server is asked what it offers; the box lists that instead.
  $("try-task").classList.toggle("hidden", kind === "mcp");
  $("try-run").lastChild.textContent = kind === "mcp" ? "Show its tools" : "Try";
  $("try-result").className = "try-result hidden";

  // A kind the form cannot show is edited as JSON, and only as JSON.
  $("editor-mode").classList.toggle("hidden", FORM_KINDS.indexOf(kind) === -1);
  setEditorMode(FORM_KINDS.indexOf(kind) === -1 ? "json" : "agent");
}

/** Back to the first question. */
export function showKindPicker() {
  state.agentKind = "";
  $("kind-picker").classList.remove("hidden");
  $("agent-form").classList.add("hidden");
  $("json-form").classList.add("hidden");
  $("try-box").classList.add("hidden");
  $("editor-actions").classList.add("hidden");
  $("probe-result").className = "notice hidden";
}

export function setEditorMode(mode) {
  state.editorMode = mode;
  $("agent-form").classList.toggle("json-mode", mode === "json");
  $("json-form").classList.toggle("hidden", mode !== "json");
  $("editor-mode").querySelectorAll("button").forEach(function (button) {
    button.setAttribute("aria-checked", String(button.dataset.mode === mode));
  });
  renderTryArgs();
}

/** The entry the form describes, or an error naming what is missing. */
export function entryFromForm() {
  const kind = state.agentKind;
  const name = $("agent-name").value.trim();
  const description = $("agent-description").value.trim();

  if (!name) throw new Error("Give the agent a name.");
  if (!/^[A-Za-z][A-Za-z0-9_-]{0,31}$/.test(name)) {
    throw new Error("Use letters, digits, - or _ in the name, starting with a letter.");
  }
  if (kind !== "mcp" && !description) {
    throw new Error("Say in a sentence what it does: that is what the planner reads.");
  }

  const existing = state.editing
    ? (lastAgents.find(function (item) { return item.agent_id === state.editing; }) || {}).source || {}
    : {};
  // Fields the form does not show — a model, headers, approval — are kept
  // from the saved definition rather than dropped by an edit.
  const entry = Object.assign({}, existing, { agent_id: name, type: kind });
  const skill = slug($("agent-skill").value) || slug(name);

  if (description) entry.description = description;

  if (kind === "llm") {
    entry.instructions = $("agent-instructions").value.trim();
    if (!entry.instructions) throw new Error("Write its instructions: what it is and what it does.");
    entry.capabilities = [skill];
  } else if (kind === "http") {
    entry.url = $("agent-url").value.trim();
    if (!/^https?:\/\//.test(entry.url)) throw new Error("The address starts with http:// or https://.");
    entry.method = $("agent-method").value;
    entry.capabilities = [skill];

    // Each {arguments.x} becomes an input the planner fills; one already
    // described keeps its type and description.
    const names = argumentNames(entry.url);
    const known = existing.arguments || {};
    if (names.length) {
      entry.arguments = {};
      names.forEach(function (argName) {
        entry.arguments[argName] = known[argName] || { type: "string", required: true };
      });
    } else {
      delete entry.arguments;
    }

    const path = $("agent-result-path").value.trim();
    if (path) entry.result_path = path.split("."); else delete entry.result_path;
  } else {
    const command = $("agent-command").value.trim();
    if (!command) {
      throw new Error(kind === "mcp" ? "Write the command that starts it, or its address." : "Write the command to run.");
    }

    if (kind === "mcp" && /^https?:\/\//.test(command)) {
      entry.url = command;
      delete entry.command;
    } else {
      entry.command = splitCommand(command);
      delete entry.url;
    }

    // A tool server names its own tools; a command does one thing.
    if (kind === "cli") entry.capabilities = [skill];
  }

  return entry;
}

/** In form mode, write the form into the JSON box the save path reads. */
export function syncFormToJson(result) {
  if (state.editorMode !== "agent") return true;

  try {
    $("entry").value = JSON.stringify(entryFromForm(), null, 2);
    return true;
  } catch (error) {
    result.className = "small reason";
    result.textContent = error.message;
    return false;
  }
}

$("kind-picker").addEventListener("click", function (event) {
  const card = event.target.closest("[data-pick]");
  if (!card) return;

  pickKind(card.dataset.pick);
  $("agent-name").focus();
});

$("pick-json").addEventListener("click", function () {
  pickKind("json");
  $("entry").focus();
});

$("kind-back").addEventListener("click", showKindPicker);

$("editor-mode").addEventListener("click", function (event) {
  const button = event.target.closest("button[data-mode]");
  if (!button) return;

  // Switching to JSON shows what the form makes, so it can be taken further.
  if (button.dataset.mode === "json" && state.editorMode === "agent" && $("agent-name").value.trim()) {
    syncFormToJson($("editor-result"));
  }

  setEditorMode(button.dataset.mode);
});

$("agent-templates").addEventListener("click", function (event) {
  const button = event.target.closest("[data-example-index]");
  if (!button) return;

  fillAgentForm((EXAMPLES[state.agentKind] || [])[Number(button.dataset.exampleIndex)] || {});
});

$("agent-url").addEventListener("input", function () { renderTryArgs(); });

// ── trying it ───────────────────────────────────────────────────────────────

/** An input per {arguments.x} in the address, keeping what was typed. */
export function renderTryArgs(values) {
  const box = $("try-args");
  const typed = {};

  box.querySelectorAll("[data-arg]").forEach(function (input) { typed[input.dataset.arg] = input.value; });
  Object.assign(typed, values || {});

  const names = state.agentKind === "http" && state.editorMode !== "json"
    ? argumentNames($("agent-url").value) : [];

  setRows(box, names.map(function (argName) {
    return '<label class="field">' + esc(argName)
      + '<input type="text" data-arg="' + esc(argName) + '" value="' + esc(typed[argName] || "") + '"></label>';
  }).join(""));
  box.classList.toggle("hidden", !names.length);
}

/** What came back, the way a person reads it: the text when there is one,
 * the JSON otherwise. */
function tryResultHtml(result) {
  const head = statusChip(result.status)
    + '<span class="small muted">' + esc(result.capability) + " · " + esc(result.duration_ms) + " ms</span>"
    + (result.verdict ? '<span class="small muted">· ' + esc(result.verdict) + "</span>" : "");

  if (result.status !== "ok") {
    return '<div class="try-head">' + head + '</div><pre class="output bad">' + esc(result.error || "") + "</pre>";
  }

  const output = result.output || {};
  const text = ["answer", "text", "stdout", "summary", "result"]
    .map(function (key) { return output[key]; })
    .find(function (value) { return typeof value === "string" && value.trim(); });

  // An AI agent writes Markdown; a command's output is text whose * and _
  // are its own, so only the first is rendered.
  if (text && state.agentKind === "llm") {
    return '<div class="try-head">' + head + '</div><div class="md try-md">' + renderMarkdown(text) + "</div>";
  }

  return '<div class="try-head">' + head + "</div>"
    + '<pre class="output">' + esc(text || JSON.stringify(output, null, 2)) + "</pre>";
}

export async function runTry() {
  const box = $("try-result");
  const button = $("try-run");

  // A tool server's answer to "try" is the list of what it offers.
  if (state.agentKind === "mcp") {
    testEntry();
    return;
  }

  if (!syncFormToJson(box)) {
    box.className = "try-result notice bad";
    return;
  }

  let entry;
  try {
    entry = JSON.parse($("entry").value);
  } catch (error) {
    box.className = "try-result notice bad";
    box.textContent = "That is not valid JSON: " + error.message;
    return;
  }

  const task = $("try-task").value.trim();
  if (!task) {
    $("try-task").focus();
    return;
  }

  const args = {};
  $("try-args").querySelectorAll("[data-arg]").forEach(function (input) {
    if (input.value.trim()) args[input.dataset.arg] = input.value.trim();
  });

  button.disabled = true;
  box.className = "try-result";
  box.innerHTML = '<div class="try-head">' + statusChip("running") + '<span class="small muted">asking…</span></div>';

  try {
    const result = await api("/registry/agents/try", {
      method: "POST",
      body: JSON.stringify({
        entry: entry,
        agent_id: state.editing || entry.agent_id || "",
        task: task,
        arguments: args,
      }),
    });
    box.innerHTML = tryResultHtml(result);
  } catch (error) {
    box.className = "try-result notice bad";
    box.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

$("try-run").addEventListener("click", runTry);
$("try-task").addEventListener("keydown", function (event) {
  if (event.key === "Enter") { event.preventDefault(); runTry(); }
});

export function startEditing(agentId) {
  const agent = lastAgents.find(function (item) { return item.agent_id === agentId; });

  if (!agent) return;

  const definition = agent.source && Object.keys(agent.source).length
    ? Object.assign({}, agent.source)
    : {
        type: agent.kind,
        description: agent.description,
        endpoint: agent.endpoint,
        capabilities: agent.capabilities,
      };

  // A definition saved under the wrong name — another target's pasted in
  // before saving checked for it — names that other target inside. Opened
  // as it is, it could never be saved here: the server refuses a definition
  // for another target. The name it is saved under is this one, so the
  // editor says so, and warns that the rest may be the other target's.
  const misnamed = typeof definition.agent_id === "string" && definition.agent_id !== agentId
    ? definition.agent_id : "";
  if ("agent_id" in definition) definition.agent_id = agentId;

  state.editing = agentId;
  $("editor-title").textContent = "Editing " + agentId;
  $("save-entry").textContent = "Save";
  $("cancel-edit").classList.remove("hidden");
  $("entry").value = JSON.stringify(definition, null, 2);
  $("editor-result").textContent = "";

  const resultPath = Array.isArray(definition.result_path)
    ? definition.result_path.join(".") : definition.result_path;
  fillAgentForm({
    agent_id: agentId,
    skill: (definition.capabilities || [])[0] || "",
    description: definition.description || "",
    instructions: definition.instructions || "",
    url: agent.kind === "http" ? definition.url || "" : "",
    method: definition.method || "GET",
    result_path: resultPath || "",
    command: definition.command ? joinCommand(definition.command) : definition.url || "",
  });
  pickKind(agent.kind);

  $("probe-result").className = misnamed ? "notice warn" : "notice hidden";
  $("probe-result").textContent = misnamed
    ? "This was saved with " + misnamed + "'s definition. Check the URL and capabilities before saving."
    : "";

  loadHistory(agentId);
  openEditor();
}

export async function saveEntry() {
  const result = $("editor-result");
  const persist = $("persist").checked;
  let parsed;

  if (!syncFormToJson(result)) return;

  try {
    parsed = JSON.parse($("entry").value);
  } catch (error) {
    result.className = "small reason";
    result.textContent = "That is not valid JSON: " + error.message;
    return;
  }

  const button = $("save-entry");
  button.disabled = true;
  result.className = "small muted";
  result.textContent = "Trying it…";

  try {
    let message;

    if (state.editing) {
      // The whole entry is replaced rather than merged: a merge could not
      // express removing a field.
      const data = await api("/registry/agents/" + encodeURIComponent(state.editing), {
        method: "PUT",
        body: JSON.stringify({ entry: parsed, persist: persist }),
      });
      message = data.agent.agent_id + " → " + data.agent.status;
    } else {
      const data = await api("/registry/agents", {
        method: "POST",
        body: JSON.stringify({ manifest: parsed, persist: persist }),
      });
      message = (data.agents || [])
        .map(function (a) { return a.agent_id + " → " + a.status; }).join(", ");
    }

    toast(message + (persist ? " · saved" : " · this session only"), "ok");
    closeEditor();
    loadTargets();
  } catch (error) {
    result.className = "small reason";
    result.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

/** Switch a target on or off, keeping everything else about it.
 *
 * A disabled target stays in the list with its definition and history, but
 * the planner is not shown it and it is not probed — so turning one off to
 * see how plans change is undone with one click rather than retyped.
 */
export async function toggleEnabled(agentId) {
  const agent = lastAgents.find(function (item) { return item.agent_id === agentId; });

  if (!agent || !agent.source || !Object.keys(agent.source).length) {
    toast(agentId + " has no saved definition to change; edit it instead.", "warn");
    renderTargets();
    return;
  }

  const enabling = agent.enabled === false;
  const entry = Object.assign({}, agent.source, { enabled: enabling });

  try {
    await api("/registry/agents/" + encodeURIComponent(agentId), {
      method: "PUT",
      body: JSON.stringify({ entry: entry, persist: state.writable }),
    });
    toast(agentId + (enabling ? " is back in the catalog." : " is switched off."), "ok");
  } catch (error) {
    toast(error.message, "bad");
  }

  loadTargets();
}

export async function removeTarget(agentId) {
  const sure = await ask({
    title: "Remove " + agentId + "?",
    text: "If it is in the configuration file, it is taken out of there too. "
      + "Switching it off instead keeps its definition.",
    ok: "Remove",
    danger: true,
  });

  if (sure === null) return;

  try {
    await api("/registry/agents/" + encodeURIComponent(agentId), { method: "DELETE" });
    toast(agentId + " removed.", "ok");
  } catch (error) {
    toast(error.message, "bad");
  }

  loadTargets();
}

document.addEventListener("click", function (event) {
  const target = event.target;
  if (!target.getAttribute) return;

  const editId = target.getAttribute("data-edit");
  if (editId) { startEditing(editId); return; }

  const removeId = target.getAttribute("data-remove");
  if (removeId) removeTarget(removeId);
});

document.addEventListener("change", function (event) {
  const toggleId = event.target.getAttribute && event.target.getAttribute("data-toggle");
  if (toggleId) toggleEnabled(toggleId);
});

$("add-target").addEventListener("click", function () { startAdding(); openEditor(); });
$("close-editor").addEventListener("click", closeEditor);

/** Try the definition in the box without saving it. */
export async function testEntry() {
  const panel = $("probe-result");
  const button = $("test-entry");
  let parsed;

  if (!syncFormToJson($("editor-result"))) return;

  try {
    parsed = JSON.parse($("entry").value);
  } catch (error) {
    panel.className = "notice bad";
    panel.textContent = "That is not valid JSON: " + error.message;
    return;
  }

  button.disabled = true;
  panel.className = "notice";
  panel.textContent = "Trying…";

  try {
    const result = await api("/registry/agents/probe", {
      method: "POST",
      body: JSON.stringify({ entry: parsed, agent_id: state.editing || "" }),
    });

    // An MCP server reports its own tools, and there are routinely a dozen
    // of them. Counting here and listing them below keeps the first line
    // readable for every kind of target.
    const offers = result.tools && result.tools.length
      ? result.tools.length + " tools it reported itself"
      : (result.capabilities.join(", ") || "nothing yet");

    const lines = [
      result.agent_id + " (" + (KIND_NAMES[result.kind] || result.kind) + ") offers " + offers + ".",
    ];

    if (result.probed) {
      lines.push("Tried: " + result.probed);
    }

    // The one thing the writer cannot see for themselves.
    if (result.rehosted_from) {
      lines.push(
        "You wrote " + result.rehosted_from + ", and this deployment dials "
        + result.probed + " instead (KIO1_AGENT_HOST)."
      );
    }

    if (result.status === "ready") {
      lines.push("It answered.");
      panel.className = "notice ok";
    } else if (result.status === "unchecked") {
      lines.push(
        "Nothing about this can be probed, so it will be used without one. "
        + "Add a health_url to have it checked."
      );
      panel.className = "notice warn";
    } else {
      lines.push(result.detail);
      // The usual reason a tool server will not start: what launches it is
      // not installed. Saying where to get it saves a search.
      const missing = /could not start '(npx|uvx)'/.exec(result.detail || "");
      if (missing) {
        lines.push(missing[1] === "npx"
          ? "npx comes with Node.js: install it from nodejs.org, then try again."
          : "uvx comes with uv: install it from docs.astral.sh/uv, then try again.");
      }
      panel.className = "notice bad";
    }

    // An MCP server was not told what it can do — it was asked, and this is
    // the answer. It goes last because it is the longest part and the
    // verdict above it is what the button was pressed for.
    if (result.tools && result.tools.length) {
      lines.push("");
      result.tools.forEach(function (tool) {
        let line = "  • " + tool.name;
        if (tool.arguments) {
          line += "(" + tool.arguments + ")";
        }
        if (tool.description) {
          // These run to whole paragraphs. The planner is given the long
          // form; a person scanning the panel wants to tell them apart.
          const summary = tool.description.replace(/\s+/g, " ").trim();
          line += "  " + (summary.length > 90
            ? summary.slice(0, 90) + "…"
            : summary);
        }
        lines.push(line);
      });
    }

    panel.textContent = lines.join("\n");
  } catch (error) {
    panel.className = "notice bad";
    panel.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

$("test-entry").addEventListener("click", testEntry);
$("save-entry").addEventListener("click", saveEntry);
$("cancel-edit").addEventListener("click", startAdding);

// ── help ────────────────────────────────────────────────────────────────────

document.querySelectorAll(".use-sample").forEach(function (button) {
  button.addEventListener("click", function () {
    const sample = document.querySelector('[data-sample="' + button.dataset.for + '"]');
    state.editing = null;
    startAdding();
    // A sample is JSON, so it opens as JSON.
    pickKind("json");
    $("entry").value = sample.textContent;
    showTab("targets");
    openEditor();
  });
});

$("to-help").addEventListener("click", function (event) {
  event.preventDefault();
  showTab("help");
});

hooks.paletteSources.push(function () {
  return [{ label: "Add an agent", hint: "agents", run: function () { showTab("targets"); startAdding(); openEditor(); } }]
    .concat(lastAgents.map(function (agent) {
      return {
        label: "Edit agent " + agent.agent_id,
        hint: agent.kind,
        run: function () { showTab("targets"); startEditing(agent.agent_id); },
      };
    }));
});
