// The frame around the pages: the menu, the tabs and the address that
// names them, the theme, and the token button.

import { $, TOKEN_KEY, ask, hooks, state, toast, token } from "./core.js";

// ── shell ───────────────────────────────────────────────────────────────────

const TABS = ["workflows", "targets", "settings", "help"];
const TITLES = { workflows: "Run", targets: "Targets", settings: "Settings", help: "How to use" };
// The address names the page, so a reload or a shared link lands where it was.
// A page may add to it: the chat keeps the open conversation there.
const ROUTES = { workflows: "run", targets: "targets", settings: "settings", help: "help" };

export function showTab(name, options) {
  if (TABS.indexOf(name) === -1) name = "workflows";
  state.tab = name;

  TABS.forEach(function (candidate) {
    $("view-" + candidate).classList.toggle("hidden", candidate !== name);
    $("tab-" + candidate).setAttribute("aria-selected", String(candidate === name));
  });

  // The chat fills the window instead of scrolling as a page; the stylesheet
  // needs to know which page is showing to lay that out.
  document.documentElement.dataset.page = ROUTES[name];
  setTitle(TITLES[name]);

  if (!(options && options.fromAddress)) setAddress(name);

  hooks.shown.forEach(function (callback) { callback(name); });
  hooks.refresh();
}

/** Name the page in the top bar and the browser tab. */
export function setTitle(text) {
  $("page-title").textContent = text;
  document.title = text + " · KIO1 Console";
}

/** Write the page, and whatever it adds, into the address. */
export function setAddress(name, detail) {
  history.replaceState(null, "", "#/" + ROUTES[name] + (detail ? "/" + detail : ""));
}

export function tabFromAddress() {
  const route = window.location.hash.replace(/^#\/?/, "").split("/")[0];
  return TABS.find(function (name) { return ROUTES[name] === route; }) || "workflows";
}

/** What the page added to the address: `#/run/<this>`. */
export function addressDetail() {
  return window.location.hash.replace(/^#\/?/, "").split("/")[1] || "";
}

TABS.forEach(function (name) {
  $("tab-" + name).addEventListener("click", function () { showTab(name); });
});

window.addEventListener("hashchange", function () {
  showTab(tabFromAddress(), { fromAddress: true });
});

// ── theme ───────────────────────────────────────────────────────────────────

const THEME_KEY = "kio1.theme";

export function storedTheme() {
  try { return localStorage.getItem(THEME_KEY) || "system"; } catch { return "system"; }
}

/** Light, dark, or whatever the system says — remembered in this browser. */
export function applyTheme(choice) {
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

export function isDark() {
  const chosen = document.documentElement.dataset.theme;
  if (chosen) return chosen === "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

// ── the menu ────────────────────────────────────────────────────────────────

export function setRailCollapsed(collapsed) {
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
  hooks.refresh();
});

// Clicking the dimmed backdrop closes either dialog.
["palette", "ask", "setup"].forEach(function (id) {
  $(id).addEventListener("click", function (event) {
    if (event.target === $(id)) $(id).close("cancel");
  });
});
