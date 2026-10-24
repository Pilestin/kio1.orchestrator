// The Targets page: what a plan can be sent to, and the editor that adds
// and changes them. The How-to-use page's samples open in that editor.

import { $, api, ask, esc, hooks, setConnection, setRows, state, statusChip, toast } from "./core.js";
import { showTab } from "./shell.js";

const NEW_ENTRY = `{
  "agent_id": "MyAgent",
  "description": "What it does, in the words the planner should read.",
  "endpoint": "http://127.0.0.1:9000",
  "capabilities": ["my_capability"]
}`;

// ── targets ─────────────────────────────────────────────────────────────────

export function renderHistory(history) {
  if (!history || !history.length) return '<span class="muted small">—</span>';

  return history.map(function (item) {
    const mean = item.mean_success_ms === null || item.mean_success_ms === undefined
      ? "" : " · " + esc(item.mean_success_ms) + " ms";
    return '<div class="small mono">' + esc(item.capability) + ": "
      + esc(item.successes) + "/" + esc(item.attempts)
      + " · rate " + esc(item.rate) + mean + "</div>";
  }).join("");
}

export function targetRow(agent) {
  const caps = (agent.capabilities || [])
    .map(function (c) { return '<span class="cap">' + esc(c) + "</span>"; }).join("");
  const id = esc(agent.agent_id);
  const off = agent.enabled === false;

  return '<tr class="' + (off ? "is-off" : "") + '">'
    + '<td class="target-cell"><span class="target-name">' + id + "</span>"
      + (agent.approval
        ? '<svg class="icon lock" aria-label="Asks before running"><title>Asks before running</title>'
          + '<use href="#i-lock"/></svg>' : "")
      + '<span class="kind-tag ' + esc(agent.kind) + '">' + esc(agent.kind) + "</span>"
      + '<span class="target-origin muted small" title="Where this target came from">'
      + esc(agent.origin) + "</span>"
      + (agent.description ? '<div class="muted small description">' + esc(agent.description) + "</div>" : "")
    + "</td>"
    + "<td>" + (off ? statusChip("disabled") : statusChip(agent.status))
      // An error under a target that is still offered is a warning — one
      // failed check is not withdrawal — and reads as one.
      + (!off && agent.last_error
        ? '<div class="reason' + (agent.status === "unreachable" ? "" : " soft") + '" title="'
          + esc(agent.last_error) + '">'
          + (agent.status === "unreachable" ? "" : "Last check failed: ")
          + esc(agent.last_error) + "</div>"
        : "")
    + "</td>"
    + '<td><div class="caps">' + caps + "</div></td>"
    + '<td class="wide-only">' + renderHistory(agent.history) + "</td>"
    + '<td class="toggle"><label class="switch" title="' + (off ? "Enable" : "Disable") + " " + id + '">'
      + '<input type="checkbox" data-toggle="' + id + '"' + (off ? "" : " checked")
      + ' aria-label="Use ' + id + '"><span class="track"></span></label></td>'
    // Icons rather than words: the actions column has to fit a laptop screen
    // beside the capabilities, and the titles still say what each does.
    + '<td class="actions">'
      + '<button class="icon-button" data-edit="' + id + '" title="Edit ' + id
        + '" aria-label="Edit ' + id + '"><svg class="icon"><use href="#i-edit"/></svg></button>'
      + '<button class="icon-button danger" data-remove="' + id + '" title="Remove ' + id
        + '" aria-label="Remove ' + id + '"><svg class="icon"><use href="#i-trash"/></svg></button>'
    + "</td></tr>";
}

export let lastAgents = [];
const targetFilter = { text: "", kind: "" };

/** The targets the search box and kind filter leave visible. */
export function visibleAgents() {
  const text = targetFilter.text.toLowerCase();

  return lastAgents.filter(function (agent) {
    if (targetFilter.kind && agent.kind !== targetFilter.kind) return false;
    if (!text) return true;

    return [agent.agent_id, agent.description, (agent.capabilities || []).join(" ")]
      .join(" ").toLowerCase().indexOf(text) !== -1;
  });
}

export function renderTargets() {
  const shown = visibleAgents();
  const empty = lastAgents.length
    ? "Nothing matches the filter."
    : "No targets yet. Use “Add a target” to register one.";

  setRows($("targets-body"), shown.map(targetRow).join("")
    || '<tr><td colspan="6"><div class="empty-state"><svg class="icon"><use href="#i-empty"/></svg>'
      + esc(empty) + "</div></td></tr>");

  const usable = lastAgents.filter(function (a) {
    return a.enabled !== false && a.status !== "unreachable";
  }).length;
  $("targets-count").textContent = lastAgents.length + " known · " + usable + " usable";
}

$("targets-search").addEventListener("input", function () {
  targetFilter.text = $("targets-search").value.trim();
  renderTargets();
});

$("targets-kind").addEventListener("click", function (event) {
  const button = event.target.closest("button[data-kind]");
  if (!button) return;

  targetFilter.kind = button.dataset.kind;
  $("targets-kind").querySelectorAll("button").forEach(function (candidate) {
    candidate.setAttribute("aria-checked", String(candidate === button));
  });
  renderTargets();
});

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
  $("entry").focus({ preventScroll: true });
}

export function closeEditor() {
  $("editor-panel").classList.add("hidden");
  startAdding();
}

export function startAdding() {
  state.editing = null;
  $("editor-title").textContent = "Add a target";
  $("save-entry").textContent = "Add";
  $("cancel-edit").classList.add("hidden");
  $("entry").value = NEW_ENTRY;
  $("editor-result").textContent = "";
  $("probe-result").className = "notice hidden";
  fillAgentForm({});
  setEditorMode("agent");
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
// Adding an agent without writing JSON: a name, what it does, and its
// instructions make an `llm` target, run by the configured model.

const AGENT_TEMPLATES = {
  reviewer: {
    agent_id: "CodeReviewer",
    skill: "code_review",
    description: "Reviews code for bugs and risky patterns, and explains each finding briefly.",
    instructions: "You are a careful senior code reviewer. Read the code or diff in the inputs "
      + "and list concrete problems: bugs, unsafe patterns, missing error handling. For each, "
      + "give where it is and a one-line fix. If the inputs contain no code, say so.",
  },
  requirements: {
    agent_id: "RequirementsWriter",
    skill: "requirements_writing",
    description: "Turns a feature idea into user stories with acceptance criteria.",
    instructions: "You are a business analyst. Turn the idea in the task into three to six "
      + "user stories (As a … I want … so that …), each with two or three testable acceptance "
      + "criteria. Ask nothing; state any assumption you make.",
  },
  docs: {
    agent_id: "DocWriter",
    skill: "documentation",
    description: "Writes clear documentation for code or a feature, in the language of the task.",
    instructions: "You are a technical writer. Write short, clear documentation for what the "
      + "task describes: what it is for, how to use it, and one example. Write in the language "
      + "of the task. Use only facts from the task and its inputs.",
  },
};

/** "Code Reviewer!" → "code_reviewer": a capability from a name. */
export function slug(text) {
  return String(text || "")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

export function fillAgentForm(values) {
  $("agent-name").value = values.agent_id || "";
  $("agent-skill").value = values.skill || "";
  $("agent-description").value = values.description || "";
  $("agent-instructions").value = values.instructions || "";
  $("agent-name").disabled = Boolean(state.editing);
}

export function setEditorMode(mode) {
  state.editorMode = mode;
  $("agent-form").classList.toggle("hidden", mode !== "agent");
  $("json-form").classList.toggle("hidden", mode !== "json");
  $("editor-mode").querySelectorAll("button").forEach(function (button) {
    button.setAttribute("aria-checked", String(button.dataset.mode === mode));
  });
}

/** The `llm` entry the form describes, or an error naming what is missing. */
export function entryFromForm() {
  const name = $("agent-name").value.trim();
  const instructions = $("agent-instructions").value.trim();

  if (!name) throw new Error("Give the agent a name.");
  if (!/^[A-Za-z][A-Za-z0-9_-]{0,31}$/.test(name)) {
    throw new Error("Use letters, digits, - or _ in the name, starting with a letter.");
  }
  if (!instructions) throw new Error("Write its instructions: what it is and what it does.");

  const skill = slug($("agent-skill").value) || slug(name);
  const existing = state.editing
    ? (lastAgents.find(function (item) { return item.agent_id === state.editing; }) || {}).source || {}
    : {};

  // Fields the form does not show — a model, a temperature, approval — are
  // kept from the saved definition rather than dropped by an edit.
  return Object.assign({}, existing, {
    agent_id: name,
    type: "llm",
    description: $("agent-description").value.trim(),
    instructions: instructions,
    capabilities: [skill],
  });
}

/** In agent mode, write the form into the JSON box the save path reads. */
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

$("editor-mode").addEventListener("click", function (event) {
  const button = event.target.closest("button[data-mode]");
  if (!button) return;

  // Switching to JSON shows what the form makes, so it can be taken further.
  if (button.dataset.mode === "json" && state.editorMode === "agent"
      && $("agent-name").value.trim()) {
    syncFormToJson($("editor-result"));
  }

  setEditorMode(button.dataset.mode);
});

$("agent-templates").addEventListener("click", function (event) {
  const button = event.target.closest("[data-template]");
  if (!button) return;

  fillAgentForm(AGENT_TEMPLATES[button.dataset.template] || {});
  $("agent-instructions").focus();
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
  $("probe-result").className = misnamed ? "notice warn" : "notice hidden";
  $("probe-result").textContent = misnamed
    ? "This was saved with " + misnamed + "'s definition. Check the URL and capabilities before saving."
    : "";

  loadHistory(agentId);

  // An LLM agent opens in the form it was made with; anything else in JSON.
  if (agent.kind === "llm") {
    fillAgentForm({
      agent_id: agentId,
      skill: (definition.capabilities || [])[0] || "",
      description: definition.description || "",
      instructions: definition.instructions || "",
    });
    setEditorMode("agent");
  } else {
    setEditorMode("json");
  }

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
  // `closest`, not the target itself: most of a 30px icon button is its 16px
  // icon, and a click there lands on the <svg> or <use>, not the button.
  const target = event.target.closest && event.target.closest("[data-edit], [data-remove]");
  if (!target) return;

  const editId = target.getAttribute("data-edit");
  if (editId) { startEditing(editId); return; }

  removeTarget(target.getAttribute("data-remove"));
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
      : (result.capabilities.join(", ") || "no capabilities");

    const lines = [
      result.agent_id + " parses as a " + result.kind + " target offering "
        + offers,
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

/** Open the Add form on the Targets page with a definition already in it.
 *
 * In JSON mode: `startAdding` leaves the editor on the agent form, where a
 * definition written into the hidden JSON box would show as an empty form.
 */
export function putInForm(text) {
  startAdding();
  setEditorMode("json");
  $("entry").value = text;
  showTab("targets");
  openEditor();
}

document.querySelectorAll(".use-sample").forEach(function (button) {
  button.addEventListener("click", function () {
    putInForm(document.querySelector('[data-sample="' + button.dataset.for + '"]').textContent);
  });
});

$("to-help").addEventListener("click", function (event) {
  event.preventDefault();
  showTab("help");
});

hooks.paletteSources.push(function () {
  return [{ label: "Add a target", hint: "targets", run: function () { showTab("targets"); startAdding(); openEditor(); } }]
    .concat(lastAgents.map(function (agent) {
      return {
        label: "Edit target " + agent.agent_id,
        hint: agent.kind,
        run: function () { showTab("targets"); startEditing(agent.agent_id); },
      };
    }));
});
