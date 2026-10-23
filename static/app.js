// The console's entry point: it loads each page's module and keeps them
// fresh. ES modules, no build step — what is in static/ is what runs.
//
//   js/core.js      talking to the service, escaping, toasts, the dialog
//   js/shell.js     the menu, tabs and address, theme, token
//   js/chat.js      the Run page: conversations
//   js/live.js      the run details drawer and the event stream
//   js/targets.js   the Targets page, and the samples on How to use
//   js/settings.js  the Settings page
//   js/setup.js     first-run setup
//   js/palette.js   the command palette (Ctrl+K)

import { loadChat } from "./js/chat.js";
import { $, REFRESH_MS, addCopyButtons, hooks, state } from "./js/core.js";
import { loadSteps } from "./js/live.js";
import "./js/palette.js";
import { loadSettings } from "./js/settings.js";
import { maybeOpenSetup } from "./js/setup.js";
import { applyTheme, setRailCollapsed, showTab, storedTheme, tabFromAddress } from "./js/shell.js";
import { loadSaveLocation, loadTargets, startAdding } from "./js/targets.js";

// ── refreshing ──────────────────────────────────────────────────────────────

function refresh() {
  if (state.tab === "targets") {
    loadTargets();
    loadSaveLocation();
  } else if (state.tab === "workflows") {
    loadChat();
    loadSteps();
  } else if (state.tab === "settings") {
    loadSettings({ quiet: true });
  }

  // The top bar shows which model will write the plan on every page: it is
  // the setting a run most often goes wrong on.
  if (state.tab !== "settings") loadSettings({ quiet: true });
}

hooks.refresh = refresh;

applyTheme(storedTheme());
setRailCollapsed(document.documentElement.dataset.rail === "collapsed");
startAdding();
addCopyButtons($("view-help"));
showTab(tabFromAddress(), { fromAddress: true });
window.setInterval(refresh, REFRESH_MS);
maybeOpenSetup();
