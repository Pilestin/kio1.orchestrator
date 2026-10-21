"use strict";

// The console. It talks only to the service that served it — no CDN, no font
// host, no analytics — so it works on a machine with no route to the internet,
// which is where an orchestrator like this usually lives.

const TOKEN_KEY = "kio1.registry.token";
const REFRESH_MS = 4000;

const state = {
  tab: "workflows",
  selected: null,     // the run whose steps are open
  editing: null,      // the agent id being edited, or null when adding
  writable: true,
  settings: null,     // the last /settings payload, or null before it loads
};

const NEW_ENTRY = `{
  "agent_id": "MyAgent",
  "description": "What it does, in the words the planner should read.",
  "endpoint": "http://127.0.0.1:9000",
  "capabilities": ["my_capability"]
}`;

// ── plumbing ────────────────────────────────────────────────────────────────

/** Escape text someone else wrote before it reaches innerHTML. */
function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function $(id) {
  return document.getElementById(id);
}

// What was last written into each table, so a refresh can tell whether it has
// anything new to say.
const rendered = new WeakMap();

/** Replace a table's rows only when they actually changed.
 *
 * The page refreshes every few seconds. Assigning innerHTML unconditionally
 * rebuilds the DOM each time, which closes any <details> the reader has
 * opened, drops a text selection, and jumps the scroll position — a finished
 * run's output cannot be read at all.
 *
 * The comparison is against what was last rendered, not against the live
 * innerHTML. Opening a <details> adds an `open` attribute to the DOM, so the
 * live markup no longer matches what we produced and every refresh would look
 * like a change — which is the bug this exists to fix.
 */
function setRows(element, html) {
  if (rendered.get(element) === html) return;

  element.innerHTML = html;
  rendered.set(element, html);
}

function token() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

async function api(path, options) {
  const settings = Object.assign({ headers: {} }, options || {});
  if (token()) settings.headers["Authorization"] = "Bearer " + token();
  if (settings.body) settings.headers["Content-Type"] = "application/json";

  const response = await fetch(path, settings);
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { detail: text }; }

  if (!response.ok) {
    const reason = payload && payload.detail ? payload.detail : response.statusText;
    const error = new Error(typeof reason === "string" ? reason : JSON.stringify(reason));
    error.status = response.status;
    throw error;
  }

  return payload;
}

function setConnection(kind, label) {
  const chip = $("conn");
  chip.className = "chip " + kind;
  chip.textContent = label;
}

function statusChip(value) {
  const kind = {
    ok: "ok", ready: "ok", completed: "ok",
    running: "warn", pending: "warn",
    unchecked: "idle", skipped: "idle", not_run: "idle",
    error: "bad", failed: "bad", unreachable: "bad",
  }[value] || "idle";

  return '<span class="chip ' + kind + '">' + esc(value) + "</span>";
}

// ── targets ─────────────────────────────────────────────────────────────────

function renderHistory(history) {
  if (!history || !history.length) return '<span class="muted small">—</span>';

  return history.map(function (item) {
    const mean = item.mean_success_ms === null || item.mean_success_ms === undefined
      ? "" : " · " + esc(item.mean_success_ms) + " ms";
    return '<div class="small mono">' + esc(item.capability) + ": "
      + esc(item.successes) + "/" + esc(item.attempts)
      + " · rate " + esc(item.rate) + mean + "</div>";
  }).join("");
}

function targetRow(agent) {
  const caps = (agent.capabilities || [])
    .map(function (c) { return '<span class="cap">' + esc(c) + "</span>"; }).join("");
  const id = esc(agent.agent_id);

  return "<tr>"
    + "<td><strong>" + id + "</strong>"
      + (agent.description ? '<div class="muted small">' + esc(agent.description) + "</div>" : "")
    + "</td>"
    + '<td class="small mono">' + esc(agent.kind) + "</td>"
    + '<td class="small mono">' + esc(agent.origin) + "</td>"
    + "<td>" + statusChip(agent.status)
      + (agent.last_error ? '<div class="reason">' + esc(agent.last_error) + "</div>" : "")
    + "</td>"
    + '<td><div class="caps">' + caps + "</div></td>"
    + "<td>" + renderHistory(agent.history) + "</td>"
    + '<td class="actions">'
      + '<button class="action quiet" data-edit="' + id + '">Edit</button>'
      + '<button class="action danger" data-remove="' + id + '">Remove</button>'
    + "</td></tr>";
}

let lastAgents = [];

async function loadTargets() {
  const warning = $("token-warning");

  try {
    const data = await api("/registry/agents");
    lastAgents = data.agents || [];
    warning.classList.add("hidden");
    setConnection("ok", "connected");

    setRows($("targets-body"),
      lastAgents.map(targetRow).join("")
      || '<tr><td colspan="7" class="muted small">No targets yet. Add one below.</td></tr>');
    $("targets-count").textContent = lastAgents.length + " known";
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

async function loadSaveLocation() {
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

function startAdding() {
  state.editing = null;
  $("editor-title").textContent = "Add a target";
  $("save-entry").textContent = "Add";
  $("cancel-edit").classList.add("hidden");
  $("entry").value = NEW_ENTRY;
  $("editor-result").textContent = "";
  $("probe-result").className = "notice hidden";
}

function startEditing(agentId) {
  const agent = lastAgents.find(function (item) { return item.agent_id === agentId; });

  if (!agent) return;

  const definition = agent.source && Object.keys(agent.source).length
    ? agent.source
    : {
        type: agent.kind,
        description: agent.description,
        endpoint: agent.endpoint,
        capabilities: agent.capabilities,
      };

  state.editing = agentId;
  $("editor-title").textContent = "Editing " + agentId;
  $("save-entry").textContent = "Save";
  $("cancel-edit").classList.remove("hidden");
  $("entry").value = JSON.stringify(definition, null, 2);
  $("editor-result").textContent = "";
  $("probe-result").className = "notice hidden";
  $("editor-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function saveEntry() {
  const result = $("editor-result");
  const persist = $("persist").checked;
  let parsed;

  try {
    parsed = JSON.parse($("entry").value);
  } catch (error) {
    result.className = "small reason";
    result.textContent = "That is not valid JSON: " + error.message;
    return;
  }

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

    result.className = "small muted";
    result.textContent = message + (persist ? " · saved" : " · this session only");
    startAdding();
    loadTargets();
  } catch (error) {
    result.className = "small reason";
    result.textContent = error.message;
  }
}

document.addEventListener("click", async function (event) {
  const target = event.target;
  if (!target.getAttribute) return;

  const editId = target.getAttribute("data-edit");
  if (editId) { startEditing(editId); return; }

  const removeId = target.getAttribute("data-remove");
  if (!removeId) return;

  if (!window.confirm("Remove " + removeId + "?\nIf it is in the configuration "
      + "file, it is taken out of there too.")) return;

  try {
    await api("/registry/agents/" + encodeURIComponent(removeId), { method: "DELETE" });
  } catch (error) {
    window.alert(error.message);
  }

  loadTargets();
});

/** Try the definition in the box without saving it. */
async function testEntry() {
  const panel = $("probe-result");
  const button = $("test-entry");
  let parsed;

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

// ── workflows ───────────────────────────────────────────────────────────────

async function loadRuns() {
  try {
    const data = await api("/workflows?limit=25");
    const runs = data.workflows || [];

    setRows($("runs-body"),
      runs.map(function (run) {
      const selected = run.session_id === state.selected ? " selected" : "";
      return '<tr class="clickable' + selected + '" data-session="' + esc(run.session_id) + '">'
        + '<td class="small mono">' + esc(new Date(run.created_at).toLocaleString()) + "</td>"
        + "<td>" + statusChip(run.status) + "</td>"
        + '<td class="small">' + esc((run.request_text || "").slice(0, 90)) + "</td>"
        + '<td class="small mono">' + esc(run.workflow_id || "—") + "</td>"
        + "</tr>";
      }).join("") || '<tr><td colspan="4" class="muted small">Nothing has run yet.</td></tr>');

    $("runs-count").textContent = runs.length + " shown";
  } catch (error) {
    setRows(
      $("runs-body"),
      '<tr><td colspan="4" class="reason">' + esc(error.message) + "</td></tr>"
    );
  }
}

$("runs-body").addEventListener("click", function (event) {
  const row = event.target.closest("tr[data-session]");
  if (!row) return;
  state.selected = row.getAttribute("data-session");
  loadRuns();
  loadDetail();
});

async function loadDetail() {
  const panel = $("detail-panel");

  if (!state.selected) { panel.classList.add("hidden"); return; }

  panel.classList.remove("hidden");
  $("detail-id").textContent = state.selected;

  try {
    const detail = await api("/workflow/" + encodeURIComponent(state.selected));
    const session = detail.session || {};
    const steps = detail.steps || [];

    // A run that never got past planning records its reason here and has no
    // steps at all. Without this the console shows an empty table and no
    // explanation for a run the list already called failed.
    const note = $("detail-note");

    if (session.error) {
      note.textContent = session.error;
      note.className = "notice bad";
    } else if (session.status === "failed" && !steps.length) {
      note.textContent = "This run failed before any step was recorded.";
      note.className = "notice bad";
    } else if (session.status === "failed") {
      const unreachable = steps.filter(function (step) {
        return (step.error || "").indexOf("unreachable") !== -1;
      });

      note.textContent = unreachable.length
        ? "Nothing answered at the addresses these targets are configured on. "
          + "Are the agents running? The Targets tab shows what is reachable."
        : "";
      note.className = unreachable.length ? "notice bad" : "notice hidden";
    } else {
      note.className = "notice hidden";
    }

    setRows($("steps-body"),
      steps.map(function (step) {
      const output = step.output
        ? '<details><summary>output</summary><pre class="output">'
          + esc(JSON.stringify(step.output, null, 2)) + "</pre></details>"
        : "";
      return "<tr>"
        + '<td class="mono small">' + esc(step.step_id) + "</td>"
        + "<td><strong>" + esc(step.agent_id) + "</strong></td>"
        + '<td class="mono small">' + esc(step.capability) + "</td>"
        + "<td>" + statusChip(step.status) + "</td>"
        + '<td class="num small">' + esc(step.duration_ms) + " ms</td>"
        + "<td>" + (step.error ? '<div class="reason">' + esc(step.error) + "</div>" : "")
          + output + "</td>"
        + "</tr>";
      }).join("") || '<tr><td colspan="6" class="muted small">No steps recorded yet.</td></tr>');
  } catch (error) {
    setRows(
      $("steps-body"),
      '<tr><td colspan="6" class="reason">' + esc(error.message) + "</td></tr>"
    );
  }
}

$("run").addEventListener("click", async function () {
  const button = $("run");
  const result = $("run-result");
  const request = $("request").value.trim();

  if (!request) {
    result.className = "small reason";
    result.textContent = "Say what you want done.";
    return;
  }

  button.disabled = true;
  result.className = "small muted";
  result.textContent = "Planning…";

  try {
    const data = await api("/workflow/run", {
      method: "POST",
      body: JSON.stringify({ request: request }),
    });
    state.selected = data.session_id;
    result.textContent = "Accepted.";
    loadRuns();
    loadDetail();
  } catch (error) {
    result.className = "small reason";
    result.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

// ── help ────────────────────────────────────────────────────────────────────

document.querySelectorAll(".use-sample").forEach(function (button) {
  button.addEventListener("click", function () {
    const sample = document.querySelector('[data-sample="' + button.dataset.for + '"]');
    state.editing = null;
    startAdding();
    $("entry").value = sample.textContent;
    showTab("targets");
    $("editor-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
});

$("to-help").addEventListener("click", function (event) {
  event.preventDefault();
  showTab("help");
});

// ── settings ───────────────────────────────────────────────────

/** Fill the model list, keeping whatever is in force selectable.
 *
 * A provider that cannot be asked returns nothing, and the note says so
 * rather than leaving an empty list that reads as "there are no models".
 */
function renderModels(settings) {
  const select = $("settings-model");
  const options = settings.models.length ? settings.models : [settings.model];

  setRows(select, options.map(function (name) {
    return '<option value="' + esc(name) + '">' + esc(name) + "</option>";
  }).join(""));

  select.value = settings.model;
  $("settings-model-note").textContent = settings.models_error
    || (settings.models.length > 1
      ? settings.models.length + " installed"
      : "");
}

/** Show which model and workspace the next run will use, on the Run page. */
function renderPlannerSummary(settings) {
  const chip = $("planner-chip");
  chip.className = "chip idle";
  chip.textContent = settings.model;

  const workspace = settings.workspace;
  $("run-workspace").textContent = workspace.path
    ? "workspace " + workspace.path
      + (workspace.entry_point ? " · " + workspace.entry_point : "")
    : "";
}

async function loadSettings(options) {
  const quiet = options && options.quiet;

  try {
    const settings = await api("/settings");
    state.settings = settings;
    // Reaching /settings proves the service is up and the token is accepted,
    // which is what the chip is reporting. It used to be set only by the
    // Targets tab, so every other tab sat at "connecting" forever.
    setConnection("ok", "connected");

    renderPlannerSummary(settings);

    // Only repaint the form when the reader is not in the middle of it: this
    // runs on the refresh timer, and overwriting a half-typed path would be
    // the console fighting whoever is using it.
    if (state.tab === "settings" && !formIsFocused()) {
      renderModels(settings);
      $("settings-temperature").value = settings.temperature;
      $("settings-timeout").value = settings.request_timeout;
      $("settings-workspace-path").value = settings.workspace.path;
      $("settings-workspace-entry").value = settings.workspace.entry_point;
      $("settings-provider").textContent = settings.provider;
      $("settings-prompt").textContent = settings.prompt_path;
      $("settings-path").textContent = settings.config_path;

      const warning = $("settings-readonly");

      if (settings.writable) {
        warning.classList.add("hidden");
      } else {
        warning.textContent = "Saving is turned off on this deployment "
          + "(KIO1_CONFIG_WRITABLE), so nothing here can be changed.";
        warning.classList.remove("hidden");
      }

      $("settings-save").disabled = !settings.writable;
    }
  } catch (error) {
    setConnection("bad", error.status === 401 ? "token" : "error");

    if (!quiet) {
      $("settings-result").textContent = error.message;
    }
  }
}

/** Report whether a settings control currently has the caret. */
function formIsFocused() {
  const active = document.activeElement;

  return Boolean(active && active.closest && active.closest("#view-settings"));
}

async function saveSettings() {
  const button = $("settings-save");
  const result = $("settings-result");

  const previousModel = state.settings ? state.settings.model : "";

  button.disabled = true;
  result.textContent = "Saving…";

  try {
    const settings = await api("/settings", {
      method: "PUT",
      body: JSON.stringify({
        model: $("settings-model").value,
        temperature: Number($("settings-temperature").value),
        request_timeout: Number($("settings-timeout").value),
        workspace_path: $("settings-workspace-path").value,
        workspace_entry_point: $("settings-workspace-entry").value,
      }),
    });

    state.settings = settings;
    renderPlannerSummary(settings);
    result.textContent = "Saved to " + settings.config_path
      + " · in force from the next run";

    // A local model is being read into memory right now if it changed, and a
    // run started before that finishes waits for it.
    if (previousModel !== settings.model) {
      result.textContent += " · loading " + settings.model + "…";
    }
  } catch (error) {
    result.textContent = error.message;
  } finally {
    button.disabled = !(state.settings && state.settings.writable);
  }
}

$("settings-save").addEventListener("click", saveSettings);
$("settings-reload").addEventListener("click", function () {
  $("settings-result").textContent = "";
  document.activeElement.blur();
  loadSettings({});
});

// ── shell ───────────────────────────────────────────────────────────────────

const TABS = ["workflows", "targets", "settings", "help"];

function showTab(name) {
  state.tab = name;

  TABS.forEach(function (candidate) {
    $("view-" + candidate).classList.toggle("hidden", candidate !== name);
    $("tab-" + candidate).setAttribute("aria-selected", String(candidate === name));
  });

  refresh();
}

TABS.forEach(function (name) {
  $("tab-" + name).addEventListener("click", function () { showTab(name); });
});

$("set-token").addEventListener("click", function () {
  const value = window.prompt(
    "Registry token (KIO1_REGISTRY_TOKEN on the server).\n"
    + "Kept in this browser tab only, never stored on disk.",
    token()
  );

  if (value === null) return;

  try { sessionStorage.setItem(TOKEN_KEY, value.trim()); } catch { /* private mode */ }

  refresh();
});

function refresh() {
  if (state.tab === "targets") {
    loadTargets();
    loadSaveLocation();
  } else if (state.tab === "workflows") {
    loadRuns();
    loadDetail();
    // The Run page shows which model will write the plan and which code the
    // agents will be pointed at. Both are the settings a run goes wrong on,
    // and both are worth seeing before pressing the button rather than after.
    loadSettings({ quiet: true });
  } else if (state.tab === "settings") {
    loadSettings({ quiet: true });
  }
}

startAdding();
refresh();
window.setInterval(refresh, REFRESH_MS);
