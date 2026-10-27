// Plumbing every page uses: talking to the service, escaping, the shared
// state, and the small pieces of UI — toasts, the dialog, copy buttons.
//
// The console talks only to the service that served it — no CDN, no font
// host, no analytics — so it works on a machine with no route to the internet,
// which is where an orchestrator like this usually lives.

export const TOKEN_KEY = "kio1.registry.token";
export const REFRESH_MS = 4000;

export const state = {
  tab: "workflows",
  selected: null,     // the run whose steps are open
  editing: null,      // the agent id being edited, or null when adding
  writable: true,
  settings: null,     // the last /settings payload, or null before it loads
  settingsDirty: false, // the settings form has unsaved changes
  editorMode: "agent",  // the target editor: "agent" (form) or "json"
};

/** What the entry module fills in: pages call these without importing the
 * pages that provide them, which would make every module import every other. */
export const hooks = {
  refresh: function () {},
  // Each returns commands for the palette: { label, hint, run }.
  paletteSources: [],
};

// ── plumbing ────────────────────────────────────────────────────────────────

/** Escape text someone else wrote before it reaches innerHTML. */
export function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function $(id) {
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
export function setRows(element, html) {
  if (rendered.get(element) === html) return;

  element.innerHTML = html;
  rendered.set(element, html);
}

export function token() {
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

export async function api(path, options) {
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

/** Colour the dot in the top bar; the word goes in its tooltip. */
export function setConnection(kind, label) {
  $("conn").className = "dot " + kind;
  $("conn").dataset.label = label;
  $("status").title = label + ($("status").dataset.detail ? "\n" + $("status").dataset.detail : "");
}

export function statusChip(value) {
  const kind = {
    ok: "ok", ready: "ok", completed: "ok", installed: "ok",
    running: "warn", pending: "warn",
    unchecked: "idle", skipped: "idle", not_run: "idle", disabled: "idle",
    error: "bad", failed: "bad", unreachable: "bad",
  }[value] || "idle";

  return '<span class="chip ' + kind + '">' + esc(value) + "</span>";
}

/** "3 min ago" for recent times, the date for older ones. */
export function relativeTime(value) {
  const then = new Date(value);
  const seconds = Math.round((Date.now() - then.getTime()) / 1000);

  if (isNaN(seconds)) return "";
  if (seconds < 45) return "just now";
  if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
  if (seconds < 86400) return Math.round(seconds / 3600) + " h ago";
  if (seconds < 7 * 86400) return Math.round(seconds / 86400) + " d ago";

  return then.toLocaleDateString();
}

// ── toasts ──────────────────────────────────────────────────────────────────

/** A short message in the corner that goes away on its own. */
export function toast(message, kind) {
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
export function ask(options) {
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

// ── copying ─────────────────────────────────────────────────────────────────

export async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    toast(message || "Copied.", "ok");
  } catch {
    toast("This browser did not allow copying.", "warn");
  }
}

/** Give every code block under `root` a copy button, once. */
export function addCopyButtons(root) {
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
