// What the chat's message box adds to plain text: choosing a target with
// @Name, and attaching files.
//
// A mention is plain text in the message — "@CodeReviewer review this" — so
// what was asked reads the same in the conversation and over the API; the
// service finds the names and tells the planner. A file is uploaded as soon
// as it is chosen, and the message carries its id.

import { $, api, esc, toast } from "./core.js";

const MAX_BYTES = 2 * 1024 * 1024;  // as core/attachments.py
const AGENTS_FRESH_MS = 30000;

const composer = {
  agents: [],       // the targets that can be chosen, from /registry/agents
  agentsAt: 0,      // when they were read
  agentsError: "",
  shown: [],        // the targets in the open list
  index: 0,         // the one highlighted
  open: false,
  files: [],        // { key, name, size, id, state: "uploading" | "ready" }
};

// ── @Name ───────────────────────────────────────────────────────────────────

async function loadAgents() {
  if (Date.now() - composer.agentsAt < AGENTS_FRESH_MS) return;

  try {
    const data = await api("/registry/agents");
    composer.agents = (data.agents || []).filter(function (agent) {
      return agent.enabled !== false;
    });
    composer.agentsError = "";
  } catch (error) {
    composer.agentsError = error.status === 401 || error.status === 503
      ? "Listing targets needs the administration token."
      : error.message;
  }

  composer.agentsAt = Date.now();
}

/** The "@partial" just before the caret, or null when there is none. */
function mentionAtCaret() {
  const box = $("request");
  const before = box.value.slice(0, box.selectionStart);
  const match = /(^|\s)@([\w.-]*)$/.exec(before);

  return match ? { query: match[2], start: before.length - match[2].length - 1 } : null;
}

export function mentionOpen() {
  return composer.open;
}

function closeMentions() {
  composer.open = false;
  $("mention-list").classList.add("hidden");
}

function renderMentions() {
  const list = $("mention-list");

  if (!composer.shown.length) {
    list.innerHTML = '<li class="empty">' + esc(composer.agentsError || "No target by that name.") + "</li>";
    return;
  }

  list.innerHTML = composer.shown.map(function (agent, index) {
    return '<li role="option" data-mention="' + esc(agent.agent_id) + '" aria-selected="'
      + String(index === composer.index) + '">'
      + '<span class="target-name">' + esc(agent.agent_id) + "</span>"
      + '<span class="small muted">' + esc(agent.description || (agent.capabilities || []).join(", ")) + "</span>"
      + "</li>";
  }).join("");

  const selected = list.querySelector('[aria-selected="true"]');
  if (selected) selected.scrollIntoView({ block: "nearest" });
}

async function updateMentions() {
  const at = mentionAtCaret();

  if (!at) { closeMentions(); return; }

  await loadAgents();

  // The caret may have moved on while the list was loading.
  const still = mentionAtCaret();
  if (!still) { closeMentions(); return; }

  const query = still.query.toLowerCase();
  composer.shown = composer.agents.filter(function (agent) {
    return agent.agent_id.toLowerCase().indexOf(query) !== -1;
  }).sort(function (a, b) {
    // Names that start with what was typed first.
    return Number(b.agent_id.toLowerCase().indexOf(query) === 0)
      - Number(a.agent_id.toLowerCase().indexOf(query) === 0);
  }).slice(0, 8);
  composer.index = Math.min(composer.index, Math.max(composer.shown.length - 1, 0));
  composer.open = true;
  $("mention-list").classList.remove("hidden");
  renderMentions();
}

/** Replace the "@partial" at the caret with the chosen target's name. */
function choose(agentId) {
  const box = $("request");
  const at = mentionAtCaret();
  if (!at) return;

  const after = box.value.slice(box.selectionStart);
  const inserted = "@" + agentId + (after.charAt(0) === " " ? "" : " ");
  box.value = box.value.slice(0, at.start) + inserted + after;

  const caret = at.start + inserted.length;
  box.setSelectionRange(caret, caret);
  box.focus();
  closeMentions();
  box.dispatchEvent(new Event("input"));
}

$("request").addEventListener("input", function () {
  composer.index = 0;
  updateMentions();
});

$("request").addEventListener("click", updateMentions);
$("request").addEventListener("blur", function () {
  // After a click on the list has been handled.
  setTimeout(closeMentions, 150);
});

$("request").addEventListener("keydown", function (event) {
  if (!composer.open) return;

  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    const count = composer.shown.length || 1;
    composer.index = (composer.index + (event.key === "ArrowDown" ? 1 : -1) + count) % count;
    renderMentions();
  } else if ((event.key === "Enter" || event.key === "Tab") && composer.shown.length) {
    choose(composer.shown[composer.index].agent_id);
  } else if (event.key === "Escape") {
    closeMentions();
  } else {
    return;
  }

  // Handled here: Enter must choose a target, not send the message too.
  event.preventDefault();
  event.stopImmediatePropagation();
});

$("mention-list").addEventListener("mousedown", function (event) {
  const item = event.target.closest("[data-mention]");
  if (!item) return;

  event.preventDefault();  // keep the caret in the box
  choose(item.dataset.mention);
});

$("mention-button").addEventListener("click", function () {
  const box = $("request");
  const start = box.selectionStart;
  const before = box.value.slice(0, start);
  const insert = (before && !/\s$/.test(before) ? " " : "") + "@";

  box.value = before + insert + box.value.slice(box.selectionEnd);
  box.setSelectionRange(start + insert.length, start + insert.length);
  box.focus();
  box.dispatchEvent(new Event("input"));
});

// ── files ───────────────────────────────────────────────────────────────────

function sizeLabel(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function renderFiles() {
  const box = $("attachment-chips");

  box.classList.toggle("hidden", !composer.files.length);
  box.innerHTML = composer.files.map(function (file) {
    return '<span class="file-chip' + (file.state === "uploading" ? " uploading" : "") + '">'
      + '<svg class="icon"><use href="#i-clip"/></svg>'
      + '<span class="name">' + esc(file.name) + "</span>"
      + '<span class="muted">' + esc(file.state === "uploading" ? "…" : sizeLabel(file.size)) + "</span>"
      + '<button type="button" data-remove-file="' + esc(file.key) + '" title="Remove" aria-label="Remove '
        + esc(file.name) + '"><svg class="icon"><use href="#i-close"/></svg></button>'
      + "</span>";
  }).join("");
}

function readBase64(file) {
  return new Promise(function (resolve, reject) {
    const reader = new FileReader();
    reader.onload = function () { resolve(String(reader.result).split(",")[1] || ""); };
    reader.onerror = function () { reject(reader.error); };
    reader.readAsDataURL(file);
  });
}

async function attach(file) {
  if (file.size > MAX_BYTES) {
    toast(file.name + " is larger than " + sizeLabel(MAX_BYTES) + ".", "bad");
    return;
  }

  const entry = { key: String(Date.now()) + Math.random(), name: file.name, size: file.size, id: "", state: "uploading" };
  composer.files.push(entry);
  renderFiles();

  try {
    const stored = await api("/files", {
      method: "POST",
      body: JSON.stringify({ name: file.name, content_base64: await readBase64(file) }),
    });
    entry.id = stored.attachment_id;
    entry.state = "ready";
  } catch (error) {
    composer.files = composer.files.filter(function (item) { return item !== entry; });
    toast(file.name + ": " + error.message, "bad");
  }

  renderFiles();
}

/** Whether a file is still on its way up; sending waits for it. */
export function uploading() {
  return composer.files.some(function (file) { return file.state === "uploading"; });
}

/** The ids to send with the message. */
export function attachmentIds() {
  return composer.files.filter(function (file) { return file.state === "ready"; })
    .map(function (file) { return file.id; });
}

export function clearAttachments() {
  composer.files = [];
  renderFiles();
}

$("attach-button").addEventListener("click", function () { $("attach-input").click(); });

$("attach-input").addEventListener("change", function () {
  Array.from($("attach-input").files || []).forEach(attach);
  $("attach-input").value = "";
});

$("attachment-chips").addEventListener("click", function (event) {
  const button = event.target.closest("[data-remove-file]");
  if (!button) return;

  composer.files = composer.files.filter(function (file) { return file.key !== button.dataset.removeFile; });
  renderFiles();
});

// Files dropped anywhere on the conversation are attached to the next message.
const conversation = document.querySelector("#view-workflows .conversation");

conversation.addEventListener("dragover", function (event) {
  if (!event.dataTransfer || Array.from(event.dataTransfer.types).indexOf("Files") === -1) return;
  event.preventDefault();
  conversation.classList.add("dropping");
});

conversation.addEventListener("dragleave", function (event) {
  if (!conversation.contains(event.relatedTarget)) conversation.classList.remove("dropping");
});

conversation.addEventListener("drop", function (event) {
  conversation.classList.remove("dropping");
  if (!event.dataTransfer || !event.dataTransfer.files.length) return;

  event.preventDefault();
  Array.from(event.dataTransfer.files).forEach(attach);
  $("request").focus();
});
