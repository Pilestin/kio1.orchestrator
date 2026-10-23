import { newChat, send } from "./chat.js";
import { $, api, esc, hooks, setRows, statusChip, toast } from "./core.js";
import { loadSettings } from "./settings.js";
import { showTab } from "./shell.js";

// ── setup ───────────────────────────────────────────────────────────────────
//
// First run: pick and download a model that suits this computer, point the
// agents at the code, and run something. Opens by itself once, then from the
// command palette.

const SETUP_KEY = "kio1.setup";
const setup = { timer: null, recommended: { planning: "", answers: "" } };

export function machineLine(hw, ollama) {
  const parts = [];
  if (hw.ram_total_gb) parts.push(hw.ram_total_gb + " GB memory");
  parts.push(hw.cpus + " cores");
  parts.push(hw.gpu ? hw.gpu + (hw.gpu_memory_gb ? " (" + hw.gpu_memory_gb + " GB)" : "") : "no GPU");
  if (ollama.running) parts.push("Ollama " + (ollama.version || "") + " running");
  return parts.join(" · ");
}

export function modelRow(model, pull) {
  let action;

  if (model.installed || (pull && pull.state === "done")) {
    action = statusChip("installed");
  } else if (pull && pull.state === "running") {
    action = '<progress max="100"' + (pull.percent !== null ? ' value="' + esc(pull.percent) + '"' : "")
      + "></progress>" + '<span class="small mono">' + esc(pull.percent !== null ? pull.percent + "%" : pull.status) + "</span>";
  } else if (!model.fits) {
    action = '<span class="small muted">needs ' + esc(model.min_ram_gb) + " GB</span>";
  } else {
    action = (pull && pull.state === "failed" ? '<span class="small reason">' + esc(pull.error) + "</span>" : "")
      + '<button class="action small" data-pull="' + esc(model.name) + '">Download</button>';
  }

  const classes = [model.recommended ? "recommended" : "", model.fits ? "" : "too-large"].join(" ").trim();

  return '<li class="' + classes + '"><div class="what">'
    + '<span><span class="target-name mono">' + esc(model.name) + "</span> "
    + '<span class="kind-tag">' + esc(model.role) + "</span></span>"
    + '<span class="small muted">' + esc(model.size_gb) + " GB · " + esc(model.note) + "</span></div>"
    + '<div class="action-cell">' + action + "</div></li>";
}

export async function renderSetup() {
  let system;

  try {
    system = await api("/system");
  } catch (error) {
    $("setup-machine").textContent = error.message;
    return;
  }

  const pulls = {};
  (system.pulls || []).forEach(function (pull) { if (!pulls[pull.model]) pulls[pull.model] = pull; });

  $("setup-machine").textContent = machineLine(system.hardware, system.ollama);

  const ollama = $("setup-ollama");
  if (!system.ollama.installed) {
    ollama.textContent = "Ollama is not installed. Get it from ollama.com/download, then run start.cmd again — or use a cloud model.";
    ollama.classList.remove("hidden");
  } else if (!system.ollama.running) {
    ollama.textContent = "Ollama is installed but not running. Start it, or run start.cmd again.";
    ollama.classList.remove("hidden");
  } else {
    ollama.classList.add("hidden");
  }

  setRows($("setup-models"), system.suggestions.map(function (model) {
    return modelRow(model, pulls[model.name]);
  }).join(""));

  const ready = function (model) {
    return model.installed || (pulls[model.name] && pulls[model.name].state === "done");
  };
  const usable = system.suggestions.filter(function (m) { return m.recommended && ready(m); });
  setup.recommended.planning = (usable.find(function (m) { return m.role !== "answers"; }) || {}).name || "";
  setup.recommended.answers = (usable.slice().reverse().find(function (m) { return m.role !== "planning"; }) || {}).name || "";
  $("setup-use").disabled = !setup.recommended.planning;

  if (document.activeElement !== $("setup-workspace")) $("setup-workspace").value = system.workspace || "";

  // Keep the bars moving while something downloads.
  const downloading = (system.pulls || []).some(function (pull) { return pull.state === "running"; });
  clearTimeout(setup.timer);
  if (downloading && $("setup").open) setup.timer = setTimeout(renderSetup, 1500);
}

export function openSetup() {
  if (!$("setup").open) $("setup").showModal();
  renderSetup();
}

export function closeSetup() {
  try { localStorage.setItem(SETUP_KEY, "done"); } catch { /* storage blocked */ }
  clearTimeout(setup.timer);
  if ($("setup").open) $("setup").close();
}

$("setup-close").addEventListener("click", closeSetup);
$("setup").addEventListener("close", function () {
  try { localStorage.setItem(SETUP_KEY, "done"); } catch { /* storage blocked */ }
});

$("setup-models").addEventListener("click", async function (event) {
  const button = event.target.closest("[data-pull]");
  if (!button) return;

  button.disabled = true;

  try {
    await api("/system/models/pull", { method: "POST", body: JSON.stringify({ model: button.dataset.pull }) });
  } catch (error) {
    toast(error.message, "bad");
  }

  renderSetup();
});

$("setup-use").addEventListener("click", async function () {
  const planning = setup.recommended.planning;
  const answers = setup.recommended.answers;
  if (!planning) return;

  try {
    await api("/settings", {
      method: "PUT",
      body: JSON.stringify({
        provider: "ollama",
        model: planning,
        review_provider: answers && answers !== planning ? "ollama" : "",
        review_model: answers && answers !== planning ? answers : "",
      }),
    });
    toast("Planning with " + planning + (answers && answers !== planning ? ", answering with " + answers : "") + ".", "ok");
    loadSettings({ quiet: true });
  } catch (error) {
    toast(error.message, "bad");
  }
});

$("setup-cloud").addEventListener("click", function () {
  closeSetup();
  showTab("settings");
});

$("setup-workspace-save").addEventListener("click", async function () {
  try {
    await api("/settings", {
      method: "PUT",
      body: JSON.stringify({ workspace_path: $("setup-workspace").value.trim() }),
    });
    toast("Saved.", "ok");
  } catch (error) {
    toast(error.message, "bad");
  }
});

$("setup-try").addEventListener("click", function () {
  closeSetup();
  showTab("workflows");
  newChat();
  send("Show me the recent commit history of this repository and which python version is installed.");
});

hooks.paletteSources.push(function () {
  return [{ label: "Set up KIO1", hint: "setup", run: openSetup }];
});

/** Open setup by itself once, on a first visit with a working token. */
export async function maybeOpenSetup() {
  let done = false;
  try { done = localStorage.getItem(SETUP_KEY) === "done"; } catch { done = true; }
  if (done) return;

  try {
    await api("/system");
    openSetup();
  } catch {
    // Without the token the console cannot set anything up; it asks later.
  }
}
