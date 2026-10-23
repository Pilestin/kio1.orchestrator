// The command palette (Ctrl+K): every page and action, and the targets
// and conversations known right now.

import { $, esc, hooks } from "./core.js";
import { applyTheme, isDark, showTab } from "./shell.js";

// ── command palette ─────────────────────────────────────────────────────────

/** Everything the palette can do, built fresh each time it opens so it
 * includes the targets and conversations known right now. The pages add
 * their own through `hooks.paletteSources`. */
export function paletteCommands() {
  const commands = [
    { label: "Go to Run", hint: "page", run: function () { showTab("workflows"); } },
    { label: "Go to Targets", hint: "page", run: function () { showTab("targets"); } },
    { label: "Go to Settings", hint: "page", run: function () { showTab("settings"); } },
    { label: "Go to How to use", hint: "page", run: function () { showTab("help"); } },
    { label: "Change the model", hint: "settings", run: function () { showTab("settings"); } },
    { label: "Switch to " + (isDark() ? "light" : "dark") + " theme", hint: "theme",
      run: function () { applyTheme(isDark() ? "light" : "dark"); } },
    { label: "Follow the system theme", hint: "theme", run: function () { applyTheme("system"); } },
    { label: "Set the administration token", hint: "access", run: function () { $("set-token").click(); } },
  ];

  hooks.paletteSources.forEach(function (source) {
    commands.push.apply(commands, source());
  });

  return commands;
}

const palette = { commands: [], shown: [], index: 0 };

export function renderPalette() {
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

export function openPalette() {
  palette.commands = paletteCommands();
  palette.index = 0;
  $("palette-input").value = "";
  renderPalette();
  $("palette").showModal();
  $("palette-input").focus();
}

export function runPaletteCommand(index) {
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


document.addEventListener("keydown", function (event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if ($("palette").open) $("palette").close(); else openPalette();
  }
});
