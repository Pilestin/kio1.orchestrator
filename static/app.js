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
  settingsDirty: false, // the settings form has unsaved changes
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

/** Take a token handed over in the address, then remove it from there.
 *
 * The launcher opens the console at `/#token=…` so nobody has to copy a
 * secret out of a terminal. The fragment never reaches the server, and it is
 * cleared at once so it is not left in the address bar or a bookmark.
 */
(function adoptTokenFromAddress() {
  const match = /(?:^|&)token=([^&]+)/.exec(window.location.hash.slice(1));
  if (!match) return;

  try { sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1])); } catch { /* private mode */ }

  // Only the token is removed: `#/settings&token=…` still opens Settings.
  const rest = window.location.hash.slice(1).split("&")
    .filter(function (part) { return part && part.indexOf("token=") !== 0; }).join("&");
  history.replaceState(null, "", window.location.pathname + window.location.search
    + (rest ? "#" + rest : ""));
})();

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
    unchecked: "idle", skipped: "idle", not_run: "idle", disabled: "idle",
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
  const off = agent.enabled === false;

  return '<tr class="' + (off ? "is-off" : "") + '">'
    + '<td class="target-cell"><span class="target-name">' + id + "</span>"
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

let lastAgents = [];
const targetFilter = { text: "", kind: "" };

/** The targets the search box and kind filter leave visible. */
function visibleAgents() {
  const text = targetFilter.text.toLowerCase();

  return lastAgents.filter(function (agent) {
    if (targetFilter.kind && agent.kind !== targetFilter.kind) return false;
    if (!text) return true;

    return [agent.agent_id, agent.description, (agent.capabilities || []).join(" ")]
      .join(" ").toLowerCase().indexOf(text) !== -1;
  });
}

function renderTargets() {
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

async function loadTargets() {
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

function openEditor() {
  $("editor-panel").classList.remove("hidden");
  $("editor-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  $("entry").focus({ preventScroll: true });
}

function closeEditor() {
  $("editor-panel").classList.add("hidden");
  startAdding();
}

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
  openEditor();
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
async function toggleEnabled(agentId) {
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

async function removeTarget(agentId) {
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

let lastRuns = [];

/** "3 min ago" for recent times, the date for older ones. */
function relativeTime(value) {
  const then = new Date(value);
  const seconds = Math.round((Date.now() - then.getTime()) / 1000);

  if (isNaN(seconds)) return "";
  if (seconds < 45) return "just now";
  if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
  if (seconds < 86400) return Math.round(seconds / 3600) + " h ago";
  if (seconds < 7 * 86400) return Math.round(seconds / 86400) + " d ago";

  return then.toLocaleDateString();
}

function runItem(run) {
  const selected = run.session_id === state.selected ? " selected" : "";

  return '<li class="run-item' + selected + '" data-session="' + esc(run.session_id)
    + '" tabindex="0" title="' + esc(run.request_text || "") + '">'
    + '<span class="request">' + esc(run.request_text || "(empty request)") + "</span>"
    + statusChip(run.status)
    + '<span class="when" title="' + esc(new Date(run.created_at).toLocaleString()) + '">'
      + esc(relativeTime(run.created_at))
      + (run.workflow_id ? " · " + esc(run.workflow_id) : "") + "</span>"
    + "</li>";
}

function renderRuns() {
  const text = $("runs-search").value.trim().toLowerCase();
  const shown = lastRuns.filter(function (run) {
    return !text || [run.request_text, run.workflow_id, run.status].join(" ")
      .toLowerCase().indexOf(text) !== -1;
  });

  setRows($("runs-body"), shown.map(runItem).join("")
    || '<li class="empty-state"><svg class="icon"><use href="#i-empty"/></svg>'
      + (lastRuns.length ? "Nothing matches the filter." : "Nothing has run yet. Try an example above.")
      + "</li>");

  $("runs-count").textContent = lastRuns.length ? String(lastRuns.length) : "";
}

async function loadRuns() {
  try {
    const data = await api("/workflows?limit=50");
    lastRuns = data.workflows || [];
    renderRuns();
  } catch (error) {
    setRows($("runs-body"), '<li class="empty-state reason">' + esc(error.message) + "</li>");
  }
}

function selectRun(sessionId) {
  state.selected = sessionId;
  renderRuns();
  loadDetail();
  followRun(sessionId);

  // Below the two-column width the detail sits under the list, where a
  // selection would otherwise change nothing the reader can see.
  if (!window.matchMedia("(min-width: 1180px)").matches) {
    $("detail-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

$("runs-search").addEventListener("input", renderRuns);

$("runs-body").addEventListener("click", function (event) {
  const item = event.target.closest("[data-session]");
  if (item) selectRun(item.getAttribute("data-session"));
});

$("runs-body").addEventListener("keydown", function (event) {
  const item = event.target.closest("[data-session]");
  if (item && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    selectRun(item.getAttribute("data-session"));
  }
});

$("detail-id").addEventListener("click", function () {
  copyText(state.selected, "Session id copied.");
});

/** The run's answer, first thing in its detail. Citations such as [Python]
 * are shown as small tags so it is clear which target said what. */
function showAnswer(text) {
  const box = $("detail-answer");

  if (!text) { box.classList.add("hidden"); return; }

  const html = esc(text).replace(
    /\[([A-Za-z0-9_.-]{1,40}(?:,\s*[A-Za-z0-9_.-]{1,40})*)\]/g,
    function (match, names) {
      return names.split(/,\s*/).map(function (name) {
        return '<span class="source">' + name + "</span>";
      }).join("");
    })
    // Models write **bold** and `code`; applied after escaping, so only
    // these two marks become markup.
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>");

  if (box.dataset.text !== text) {
    box.innerHTML = html;
    box.dataset.text = text;
  }

  box.classList.remove("hidden");
}

function durationOf(session) {
  const ms = new Date(session.updated_at) - new Date(session.created_at);

  if (!isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return ms + " ms";
  if (ms < 120000) return (ms / 1000).toFixed(1) + " s";

  return Math.round(ms / 60000) + " min";
}

async function loadDetail() {
  const panel = $("detail-panel");

  $("detail-placeholder").classList.toggle("hidden", Boolean(state.selected));

  if (!state.selected) { panel.classList.add("hidden"); return; }

  panel.classList.remove("hidden");
  $("detail-id").textContent = state.selected.slice(0, 8) + "…";

  try {
    const detail = await api("/workflow/" + encodeURIComponent(state.selected));
    const session = detail.session || {};
    const steps = detail.steps || [];

    $("detail-status").innerHTML = statusChip(session.status);
    $("detail-request").textContent = session.request_text || "";
    $("detail-meta").textContent = [
      relativeTime(session.created_at),
      session.status === "running" || session.status === "pending" ? "" : durationOf(session),
      session.workflow_id || "",
      session.execution_mode || "",
    ].filter(Boolean).join(" · ");
    showAnswer(session.answer);

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
        ? '<details><summary>Output</summary><pre class="output">'
          + esc(JSON.stringify(step.output, null, 2)) + "</pre></details>"
        : "";
      return "<tr>"
        + '<td class="mono small">' + esc(step.step_id) + "</td>"
        + '<td><span class="target-name">' + esc(step.agent_id) + "</span>"
          + '<div class="mono small muted">' + esc(step.capability) + "</div></td>"
        + "<td>" + statusChip(step.status) + "</td>"
        + '<td class="num small">' + esc(step.duration_ms) + " ms</td>"
        + "<td>" + (step.error ? '<div class="reason">' + esc(step.error) + "</div>" : "")
          + output + "</td>"
        + "</tr>";
      }).join("") || '<tr><td colspan="5" class="muted small">No steps recorded yet.</td></tr>');

    addCopyButtons($("steps-body"));
  } catch (error) {
    setRows(
      $("steps-body"),
      '<tr><td colspan="5" class="reason">' + esc(error.message) + "</td></tr>"
    );
  }
}

// ── live view ───────────────────────────────────────────────────────────────
//
// A run takes a minute or more with a local model. The stream at
// /workflow/<id>/events says what it is doing meanwhile: the model's answer as
// it is written, each step as it starts and ends, and KIO1's own log lines.

const LIVE_KINDS = [
  "status", "planning_started", "model_output", "model_answer", "plan",
  "step_started", "step_finished", "log", "finished",
  "round_started", "round_failed", "review_started", "review", "answer",
];

const live = { source: null, session: null, started: 0, streamed: false, timer: null };

function liveLine(html, className, isLog) {
  const list = $("live-log");
  const item = document.createElement("li");
  const seconds = live.started ? ((Date.now() - live.started) / 1000).toFixed(1) + "s" : "";
  if (isLog) item.className = "is-log";
  item.innerHTML = '<span class="at">' + esc(seconds) + "</span>"
    + '<span class="what' + (className ? " " + className : "") + '">' + html + "</span>";

  // Follow the bottom only if the reader is already there.
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 30;
  list.appendChild(item);
  addCopyButtons(item);
  if (atBottom) list.scrollTop = list.scrollHeight;
}

function appendText(id, text) {
  const pre = $(id);
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
  pre.textContent += text;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function setLiveState(kind, label, phase) {
  $("live-state").className = "chip " + kind;
  $("live-state").textContent = label;
  if (phase !== undefined) $("live-phase").textContent = phase;
}

function onLiveEvent(event) {
  let data;
  try { data = JSON.parse(event.data); } catch { return; }
  if (!live.started) live.started = data.time * 1000;

  switch (data.kind) {
    case "status":
      if (data.status === "running") setLiveState("warn", "running", "starting");
      if (data.status === "pending") setLiveState("warn", "pending", "queued");
      if (data.status === "completed") {
        setLiveState("ok", "completed", "");
        liveLine("Run completed.", "ok");
      }
      if (data.status === "failed") {
        setLiveState("bad", "failed", "");
        liveLine("Run failed" + (data.error ? ": " + esc(data.error) : "."), "bad");
      }
      break;

    case "planning_started":
      live.phase = "plan";
      live.streamed = data.streaming;
      setLiveState("warn", "planning", data.provider + " · " + data.model);
      startReadingTimer(data.time * 1000);
      liveLine("Planning with <strong>" + esc(data.provider + "/" + data.model) + "</strong> — "
        + esc((data.targets || []).length) + " targets in the catalog, prompt "
        + esc(data.prompt_chars) + " chars"
        + (data.streaming ? "" : ". This provider does not stream; the plan appears when it is complete."));
      break;

    case "round_started":
      appendText("live-model", "\n\n— round " + data.round + " —\n");
      liveLine("Round " + esc(data.round) + ": planning what is still missing.", "warn");
      break;

    case "round_failed":
      liveLine("Round " + esc(data.round) + " failed: " + esc(data.error), "bad");
      break;

    case "review_started":
      live.phase = "review";
      appendText("live-model", "\n\n— review —\n");
      setLiveState("warn", "reviewing", data.provider ? data.provider + " · " + data.model : "");
      startReadingTimer(data.time * 1000);
      break;

    case "review":
      liveLine(data.decision === "continue"
        ? "Review: more work needed — " + esc(data.next_request)
        : "Review: done" + (data.fallback ? " (summary written from the results)" : ""),
        data.decision === "continue" ? "warn" : "ok");
      break;

    case "answer":
      showAnswer(data.text);
      break;

    case "model_output":
      stopReadingTimer(live.phase === "review" ? "writing the answer" : "writing the plan");
      if (data.channel === "thinking") {
        $("live-thinking-box").classList.remove("hidden");
        appendText("live-thinking", data.text);
      } else {
        appendText("live-model", data.text);
      }
      break;

    case "model_answer":
      stopReadingTimer("");
      if (!live.streamed) appendText("live-model", data.text);
      liveLine("Model answered (" + esc(data.text.length) + " chars).");
      break;

    case "plan":
      setLiveState("warn", "running", "dispatching " + data.steps.length + " step(s)");
      liveLine("Plan <span class=\"mono\">" + esc(data.workflow_id) + "</span> ("
        + esc(data.execution_mode) + ")" + (data.explanation ? ": " + esc(data.explanation) : ""));
      data.steps.forEach(function (step) {
        const after = step.depends_on && step.depends_on.length
          ? " · after " + esc(step.depends_on.join(", ")) : "";
        const args = step.arguments && Object.keys(step.arguments).length
          ? " · <span class=\"mono\">" + esc(JSON.stringify(step.arguments)) + "</span>" : "";
        liveLine("&nbsp;&nbsp;" + esc(step.step_id) + " → <strong>" + esc(step.agent_id)
          + "</strong>." + esc(step.capability) + after + args
          + '<div class="quiet">' + esc(step.task) + "</div>");
      });
      break;

    case "step_started":
      liveLine("▶ " + esc(data.step_id) + " → <strong>" + esc(data.agent_id) + "</strong> ("
        + esc(data.target_kind) + ", " + esc(data.capability) + ")"
        + (data.attempt > 1 ? " · attempt " + esc(data.attempt) : ""));
      break;

    case "step_finished": {
      const ok = data.status === "ok";
      const output = data.output
        ? "<details><summary class=\"small\">output</summary><pre class=\"output\">"
          + esc(data.output) + "</pre></details>" : "";
      liveLine((ok ? "✓ " : "✗ ") + esc(data.step_id) + " " + esc(data.agent_id) + " · "
        + esc(data.status) + " · " + esc(data.duration_ms) + " ms"
        + (data.error ? '<div class="bad">' + esc(data.error) + "</div>" : "") + output,
        ok ? "ok" : "bad");
      break;
    }

    case "log":
      liveLine(esc(data.level) + " " + esc(data.logger) + ": " + esc(data.message),
        data.level === "ERROR" ? "bad" : data.level === "WARNING" ? "warn" : "quiet", true);
      break;

    case "finished":
      stopFollowing();
      loadRuns();
      loadDetail();
      break;
  }
}

/** Count the seconds before the model's first word.
 *
 * A local model reads the whole prompt before it writes anything — on a CPU
 * that is 30 to 60 silent seconds, which looks exactly like a hang. Saying
 * what it is doing, with a clock, is the difference. */
function startReadingTimer(since) {
  stopReadingTimer();

  live.timer = setInterval(function () {
    const seconds = Math.round((Date.now() - since) / 1000);
    $("live-phase").textContent = "the model is reading the prompt · " + seconds + "s"
      + (seconds > 20 ? " (normal on a computer without a GPU)" : "");
  }, 1000);
}

function stopReadingTimer(phase) {
  if (live.timer) clearInterval(live.timer);
  live.timer = null;
  if (phase !== undefined) $("live-phase").textContent = phase;
}

function stopFollowing() {
  stopReadingTimer();
  if (live.source) live.source.close();
  live.source = null;
}

/** Show the live stream of a run, replaying what it has done so far. */
function followRun(sessionId) {
  if (live.session === sessionId && live.source) return;

  stopFollowing();
  live.session = sessionId;
  live.started = 0;
  live.streamed = false;
  $("live-model").textContent = "";
  $("live-thinking").textContent = "";
  $("live-thinking-box").classList.add("hidden");
  $("live-log").innerHTML = "";
  $("live").classList.remove("hidden");
  setLiveState("idle", "live", "");

  const source = new EventSource("/workflow/" + encodeURIComponent(sessionId) + "/events");
  let heard = false;

  LIVE_KINDS.forEach(function (kind) {
    source.addEventListener(kind, function (event) { heard = true; onLiveEvent(event); });
  });

  // The server closes a stream that has nothing to say, and EventSource would
  // reconnect forever. A stream that ended without a word is a run this
  // process has no live record of: one from before a restart.
  source.addEventListener("error", function () {
    if (!heard) {
      stopFollowing();
      $("live").classList.add("hidden");
    }
  });

  live.source = source;
}

$("live-logs").addEventListener("change", function () {
  $("live-log").classList.toggle("no-logs", !$("live-logs").checked);
});

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
    result.textContent = "";
    toast("Accepted. Follow it on the right as it runs.", "ok");
    loadRuns();
    selectRun(data.session_id);
  } catch (error) {
    result.className = "small reason";
    result.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("request").addEventListener("keydown", function (event) {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    $("run").click();
  }
});

$("run-examples").addEventListener("click", function (event) {
  const example = event.target.closest("[data-example]");
  if (!example) return;

  $("request").value = example.dataset.example;
  $("request").focus();
});

// ── help ────────────────────────────────────────────────────────────────────

document.querySelectorAll(".use-sample").forEach(function (button) {
  button.addEventListener("click", function () {
    const sample = document.querySelector('[data-sample="' + button.dataset.for + '"]');
    state.editing = null;
    startAdding();
    $("entry").value = sample.textContent;
    showTab("targets");
    openEditor();
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
/** Fill the model suggestions. A datalist rather than a select: OpenRouter
 * offers hundreds of models, and typing part of a name filters them, while a
 * provider that cannot list its models still accepts any name typed. */
function renderModelList(models, error) {
  setRows($("settings-model-list"), (models || []).map(function (name) {
    return '<option value="' + esc(name) + '"></option>';
  }).join(""));

  $("settings-model-note").textContent = error
    || (models && models.length > 1 ? models.length + " available · type to search" : "");
}

function providerInfo(name) {
  const list = (state.settings && state.settings.providers) || [];
  return list.find(function (item) { return item.name === name; }) || null;
}

/** Show or hide the key field, and say what the chosen provider needs. */
function renderProviderNeeds(name) {
  const info = providerInfo(name);
  const field = $("settings-key-field");
  const note = $("settings-provider-note");

  if (!info || !info.key_env) {
    field.classList.add("hidden");
    note.textContent = info && !info.available ? info.error : "Runs on this computer; no key needed.";
    return;
  }

  field.classList.remove("hidden");
  $("settings-key-label").textContent = "API key (" + info.key_env + ")";
  $("settings-key").placeholder = info.key_set ? "already set — paste to replace" : "paste the key here";
  note.textContent = info.key_set ? "Key found." : "Needs a key before it can plan.";
}

function renderProviders(settings) {
  const select = $("settings-provider-select");

  setRows(select, (settings.providers || []).map(function (info) {
    const suffix = info.key_env && !info.key_set ? " — key needed" : "";
    return '<option value="' + esc(info.name) + '"' + (info.available ? "" : " disabled") + ">"
      + esc(info.label + suffix) + "</option>";
  }).join(""));

  select.value = settings.provider;
  renderProviderNeeds(settings.provider);
  renderProviderCards(settings.providers || [], settings.provider);
}

/** The provider choice as cards: what each is and what it still needs, at a
 * glance, which a drop-down cannot say. The hidden select stays the source
 * of truth, so the rest of the form reads it exactly as before. */
function renderProviderCards(providers, chosen) {
  setRows($("provider-cards"), providers.map(function (info) {
    const [title, detail] = String(info.label).split(" — ");
    let needs = "No key needed";
    let tone = "ok";

    if (!info.available) {
      needs = "Not installed";
      tone = "warn";
    } else if (info.key_env) {
      needs = info.key_set ? "Key set" : "Needs " + info.key_env;
      tone = info.key_set ? "ok" : "warn";
    }

    return '<button type="button" class="provider-card" role="radio" data-provider="'
      + esc(info.name) + '" aria-checked="' + String(info.name === chosen) + '"'
      + (info.available ? "" : " disabled") + ">"
      + '<span class="name">' + esc(title) + "</span>"
      + (detail ? '<span class="small muted">' + esc(detail) + "</span>" : "")
      + '<span class="needs ' + tone + '">' + esc(needs) + "</span>"
      + "</button>";
  }).join(""));
}

$("provider-cards").addEventListener("click", function (event) {
  const card = event.target.closest("[data-provider]");
  if (!card || card.disabled) return;

  const select = $("settings-provider-select");
  select.value = card.dataset.provider;
  $("provider-cards").querySelectorAll("[data-provider]").forEach(function (candidate) {
    candidate.setAttribute("aria-checked", String(candidate === card));
  });
  // The change handler below asks the provider for its models; `bubbles` so
  // the form also notices it has unsaved changes.
  select.dispatchEvent(new Event("change", { bubbles: true }));
});

// ── the answer model ───────────────────────────────────────────────────────

/** Show the key field only when the chosen provider still needs a key. */
function renderReviewNeeds(name) {
  const info = name ? providerInfo(name) : null;
  const needsKey = Boolean(info && info.key_env && !info.key_set);

  $("review-key-field").classList.toggle("hidden", !needsKey);
  $("review-model-field").classList.toggle("hidden", !name);
  if (needsKey) $("review-key-label").textContent = "API key (" + info.key_env + ")";
}

function renderReview(settings) {
  const review = settings.review || {};

  setRows($("review-provider"),
    '<option value="">Same as planning</option>'
    + (settings.providers || []).filter(function (info) { return info.available; })
      .map(function (info) {
        return '<option value="' + esc(info.name) + '">'
          + esc(String(info.label).split(" — ")[0]) + "</option>";
      }).join(""));

  $("review-provider").value = review.provider || "";
  $("review-model").value = review.model || "";
  $("review-key").value = "";
  renderReviewNeeds(review.provider || "");

  if (review.provider) loadReviewModels(review.provider);
}

async function loadReviewModels(name) {
  try {
    const data = await api("/settings/models?provider=" + encodeURIComponent(name));
    setRows($("review-model-list"), (data.models || []).map(function (model) {
      return '<option value="' + esc(model) + '"></option>';
    }).join(""));
    $("review-model-note").textContent = data.models_error
      || (data.models.length > 1 ? data.models.length + " available" : "");
  } catch (error) {
    $("review-model-note").textContent = error.message;
  }
}

$("review-provider").addEventListener("change", function () {
  const name = $("review-provider").value;
  const current = state.settings && state.settings.review
    && state.settings.review.provider === name;

  $("review-model").value = current ? state.settings.review.model : "";
  renderReviewNeeds(name);
  if (name) loadReviewModels(name);
});

function renderModels(settings) {
  renderProviders(settings);
  $("settings-model").value = settings.model;
  renderModelList(settings.models, settings.models_error);
}

$("settings-provider-select").addEventListener("change", async function () {
  const name = $("settings-provider-select").value;
  const current = state.settings && state.settings.provider === name;

  renderProviderNeeds(name);
  $("settings-model").value = current ? state.settings.model : "";
  $("settings-model-note").textContent = "Asking " + name + " which models it has…";

  try {
    const data = await api("/settings/models?provider=" + encodeURIComponent(name));
    renderModelList(data.models, data.models_error);
  } catch (error) {
    renderModelList([], error.message);
  }
});

/** Show which model and workspace the next run will use, on the Run page. */
function renderPlannerSummary(settings) {
  const chip = $("planner-chip");
  chip.className = "chip idle";
  chip.textContent = settings.provider + " · " + settings.model;

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
    if (state.tab === "settings" && !formIsFocused() && !state.settingsDirty) {
      renderModels(settings);
      renderReview(settings);
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
  const key = $("settings-key").value.trim();

  button.disabled = true;
  result.textContent = "Saving…";

  try {
    const settings = await api("/settings", {
      method: "PUT",
      body: JSON.stringify({
        provider: $("settings-provider-select").value,
        api_key: key || null,
        model: $("settings-model").value.trim(),
        temperature: Number($("settings-temperature").value),
        request_timeout: Number($("settings-timeout").value),
        workspace_path: $("settings-workspace-path").value,
        workspace_entry_point: $("settings-workspace-entry").value,
        review_provider: $("review-provider").value,
        review_model: $("review-model").value.trim(),
        review_api_key: $("review-key").value.trim() || null,
      }),
    });

    state.settings = settings;
    state.settingsDirty = false;
    $("settings-key").value = "";
    renderReview(settings);
    renderModels(settings);
    renderPlannerSummary(settings);
    result.textContent = "";

    // A local model is being read into memory right now if it changed, and a
    // run started before that finishes waits for it.
    toast("Saved — in force from the next run."
      + (previousModel !== settings.model ? "\nLoading " + settings.model + "…" : ""), "ok");
  } catch (error) {
    result.className = "small reason";
    result.textContent = error.message;
    toast(error.message, "bad");
  } finally {
    button.disabled = !(state.settings && state.settings.writable);
  }
}

$("settings-save").addEventListener("click", saveSettings);
$("settings-reload").addEventListener("click", function () {
  $("settings-result").textContent = "";
  document.activeElement.blur();
  state.settingsDirty = false;
  loadSettings({});
});

// Once something is changed, the refresh timer stops repainting the form
// until it is saved or discarded. Checking focus alone was not enough: pick a
// provider, click anywhere else, and the next refresh put the old one back.
["input", "change"].forEach(function (type) {
  $("view-settings").addEventListener(type, function () { state.settingsDirty = true; });
});

// ── shell ───────────────────────────────────────────────────────────────────

const TABS = ["workflows", "targets", "settings", "help"];
const TITLES = { workflows: "Run", targets: "Targets", settings: "Settings", help: "How to use" };
// The address names the page, so a reload or a shared link lands where it was.
const ROUTES = { workflows: "run", targets: "targets", settings: "settings", help: "help" };

function showTab(name, options) {
  if (TABS.indexOf(name) === -1) name = "workflows";
  state.tab = name;

  TABS.forEach(function (candidate) {
    $("view-" + candidate).classList.toggle("hidden", candidate !== name);
    $("tab-" + candidate).setAttribute("aria-selected", String(candidate === name));
  });

  $("page-title").textContent = TITLES[name];
  document.title = TITLES[name] + " · KIO1 Console";

  if (!(options && options.fromAddress)) {
    history.replaceState(null, "", "#/" + ROUTES[name]);
  }

  refresh();
}

function tabFromAddress() {
  const route = window.location.hash.replace(/^#\/?/, "");
  return TABS.find(function (name) { return ROUTES[name] === route; }) || "workflows";
}

TABS.forEach(function (name) {
  $("tab-" + name).addEventListener("click", function () { showTab(name); });
});

window.addEventListener("hashchange", function () {
  showTab(tabFromAddress(), { fromAddress: true });
});

// ── theme ───────────────────────────────────────────────────────────────────

const THEME_KEY = "kio1.theme";

function storedTheme() {
  try { return localStorage.getItem(THEME_KEY) || "system"; } catch { return "system"; }
}

/** Light, dark, or whatever the system says — remembered in this browser. */
function applyTheme(choice) {
  const root = document.documentElement;

  if (choice === "light" || choice === "dark") {
    root.dataset.theme = choice;
  } else {
    delete root.dataset.theme;
    choice = "system";
  }

  try { localStorage.setItem(THEME_KEY, choice); } catch { /* storage blocked */ }

  document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
    button.setAttribute("aria-checked", String(button.dataset.themeChoice === choice));
  });
}

document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
  button.addEventListener("click", function () { applyTheme(button.dataset.themeChoice); });
});

function isDark() {
  const chosen = document.documentElement.dataset.theme;
  if (chosen) return chosen === "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

// ── the menu ────────────────────────────────────────────────────────────────

function setRailCollapsed(collapsed) {
  const root = document.documentElement;

  if (collapsed) root.dataset.rail = "collapsed"; else delete root.dataset.rail;
  try { localStorage.setItem("kio1.rail", collapsed ? "collapsed" : "open"); } catch { /* ignore */ }

  const label = collapsed ? "Expand the menu" : "Collapse the menu";
  $("rail-collapse").title = label;
  $("rail-collapse").setAttribute("aria-label", label);
}

$("rail-collapse").addEventListener("click", function () {
  setRailCollapsed(document.documentElement.dataset.rail !== "collapsed");
});

// ── toasts ──────────────────────────────────────────────────────────────────

/** A short message in the corner that goes away on its own. */
function toast(message, kind) {
  const item = document.createElement("div");
  item.className = "toast " + (kind || "");
  item.textContent = message;
  $("toasts").appendChild(item);

  const linger = kind === "bad" ? 7000 : 3600;

  window.setTimeout(function () {
    item.classList.add("leaving");
    window.setTimeout(function () { item.remove(); }, 250);
  }, linger);
}

// ── questions ───────────────────────────────────────────────────────────────

/** Ask something in a dialog instead of window.prompt or window.confirm.
 *
 * Resolves to the typed value (or "" when there is no input), or null when
 * the reader cancels. */
function ask(options) {
  const dialog = $("ask");
  const input = $("ask-input");

  $("ask-title").textContent = options.title || "";
  $("ask-text").textContent = options.text || "";
  $("ask-ok").textContent = options.ok || "OK";
  $("ask-ok").classList.toggle("danger", Boolean(options.danger));
  $("ask-ok").classList.toggle("primary", !options.danger);
  input.classList.toggle("hidden", !options.input);
  input.value = options.value || "";
  input.placeholder = options.placeholder || "";

  return new Promise(function (resolve) {
    dialog.addEventListener("close", function onClose() {
      dialog.removeEventListener("close", onClose);
      resolve(dialog.returnValue === "ok" ? (options.input ? input.value : "") : null);
    });

    dialog.returnValue = "";
    dialog.showModal();
    (options.input ? input : $("ask-ok")).focus();
  });
}

$("set-token").addEventListener("click", async function () {
  const value = await ask({
    title: "Administration token",
    text: "The value of KIO1_REGISTRY_TOKEN on the server. start.py opens the "
      + "console with it already filled in. Kept in this browser tab only.",
    input: true,
    value: token(),
    placeholder: "paste the token",
    ok: "Use this token",
  });

  if (value === null) return;

  try { sessionStorage.setItem(TOKEN_KEY, value.trim()); } catch { /* private mode */ }

  toast("Token updated.", "ok");
  refresh();
});

// ── copying ─────────────────────────────────────────────────────────────────

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    toast(message || "Copied.", "ok");
  } catch {
    toast("This browser did not allow copying.", "warn");
  }
}

/** Give every code block under `root` a copy button, once. */
function addCopyButtons(root) {
  (root || document).querySelectorAll("pre").forEach(function (pre) {
    if (pre.parentElement.classList.contains("copyable")) return;

    const wrapper = document.createElement("div");
    wrapper.className = "copyable";
    pre.parentElement.insertBefore(wrapper, pre);
    wrapper.appendChild(pre);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-button";
    button.title = "Copy";
    button.setAttribute("aria-label", "Copy");
    button.innerHTML = '<svg class="icon"><use href="#i-copy"/></svg>';
    button.addEventListener("click", function () { copyText(pre.textContent); });
    wrapper.appendChild(button);
  });
}

// ── command palette ─────────────────────────────────────────────────────────

/** Everything the palette can do, built fresh each time it opens so it
 * includes the targets and runs known right now. */
function paletteCommands() {
  const commands = [
    { label: "Go to Run", hint: "page", run: function () { showTab("workflows"); } },
    { label: "Go to Targets", hint: "page", run: function () { showTab("targets"); } },
    { label: "Go to Settings", hint: "page", run: function () { showTab("settings"); } },
    { label: "Go to How to use", hint: "page", run: function () { showTab("help"); } },
    { label: "New request", hint: "run", run: function () { showTab("workflows"); $("request").focus(); } },
    { label: "Add a target", hint: "targets", run: function () { showTab("targets"); startAdding(); openEditor(); } },
    { label: "Change the model", hint: "settings", run: function () { showTab("settings"); } },
    { label: "Switch to " + (isDark() ? "light" : "dark") + " theme", hint: "theme",
      run: function () { applyTheme(isDark() ? "light" : "dark"); } },
    { label: "Follow the system theme", hint: "theme", run: function () { applyTheme("system"); } },
    { label: "Set the administration token", hint: "access", run: function () { $("set-token").click(); } },
  ];

  lastAgents.forEach(function (agent) {
    commands.push({
      label: "Edit target " + agent.agent_id,
      hint: agent.kind,
      run: function () { showTab("targets"); startEditing(agent.agent_id); },
    });
  });

  lastRuns.slice(0, 15).forEach(function (run) {
    commands.push({
      label: "Open run: " + (run.request_text || "").slice(0, 60),
      hint: run.status,
      run: function () { showTab("workflows"); selectRun(run.session_id); },
    });
  });

  return commands;
}

const palette = { commands: [], shown: [], index: 0 };

function renderPalette() {
  const text = $("palette-input").value.trim().toLowerCase();
  const words = text.split(/\s+/).filter(Boolean);

  palette.shown = palette.commands.filter(function (command) {
    const haystack = (command.label + " " + command.hint).toLowerCase();
    return words.every(function (word) { return haystack.indexOf(word) !== -1; });
  }).slice(0, 40);

  palette.index = Math.min(palette.index, Math.max(palette.shown.length - 1, 0));

  $("palette-list").innerHTML = palette.shown.map(function (command, index) {
    return '<li role="option" data-index="' + index + '" aria-selected="'
      + String(index === palette.index) + '">' + esc(command.label)
      + '<span class="hint">' + esc(command.hint) + "</span></li>";
  }).join("") || '<li class="empty">Nothing matches.</li>';

  const selected = $("palette-list").querySelector('[aria-selected="true"]');
  if (selected) selected.scrollIntoView({ block: "nearest" });
}

function openPalette() {
  palette.commands = paletteCommands();
  palette.index = 0;
  $("palette-input").value = "";
  renderPalette();
  $("palette").showModal();
  $("palette-input").focus();
}

function runPaletteCommand(index) {
  const command = palette.shown[index];
  if (!command) return;

  $("palette").close();
  command.run();
}

$("open-palette").addEventListener("click", openPalette);
$("palette-input").addEventListener("input", function () { palette.index = 0; renderPalette(); });

$("palette-input").addEventListener("keydown", function (event) {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    const count = palette.shown.length || 1;
    palette.index = (palette.index + step + count) % count;
    renderPalette();
  } else if (event.key === "Enter") {
    event.preventDefault();
    runPaletteCommand(palette.index);
  }
});

$("palette-list").addEventListener("click", function (event) {
  const item = event.target.closest("[data-index]");
  if (item) runPaletteCommand(Number(item.dataset.index));
});

// Clicking the dimmed backdrop closes either dialog.
["palette", "ask"].forEach(function (id) {
  $(id).addEventListener("click", function (event) {
    if (event.target === $(id)) $(id).close("cancel");
  });
});

document.addEventListener("keydown", function (event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if ($("palette").open) $("palette").close(); else openPalette();
  }
});

// ── refreshing ──────────────────────────────────────────────────────────────

function refresh() {
  if (state.tab === "targets") {
    loadTargets();
    loadSaveLocation();
  } else if (state.tab === "workflows") {
    loadRuns();
    loadDetail();
  } else if (state.tab === "settings") {
    loadSettings({ quiet: true });
  }

  // The top bar shows which model will write the plan on every page: it is
  // the setting a run most often goes wrong on.
  if (state.tab !== "settings") loadSettings({ quiet: true });
}

applyTheme(storedTheme());
setRailCollapsed(document.documentElement.dataset.rail === "collapsed");
startAdding();
addCopyButtons($("view-help"));
showTab(tabFromAddress(), { fromAddress: true });
window.setInterval(refresh, REFRESH_MS);
