// How to use → Examples: working target definitions to add as they are.
//
// The examples live in static/examples.json rather than in this page, so
// tests/test_examples.py can check that every one is a definition the
// registry accepts — each has a button that saves it.

import { $, addCopyButtons, esc, hooks } from "./core.js";
import { addressDetail, setAddress, setTitle, showTab } from "./shell.js";
import { putInForm } from "./targets.js";

const KIND_LABELS = { http: "HTTP", cli: "CLI", mcp: "MCP", llm: "Agent", kio: "KIO" };

let examples = null;
const filter = { text: "", kind: "" };

function kindOf(example) {
  return example.entry.type || "kio";
}

async function loadExamples() {
  if (examples) return;

  try {
    const response = await fetch("/static/examples.json", { cache: "no-cache" });
    examples = await response.json();
  } catch (error) {
    examples = [];
    $("examples-list").innerHTML = '<p class="reason">Could not load the examples: '
      + esc(error.message) + "</p>";
    return;
  }

  renderExamples();
}

function matches(example) {
  if (filter.kind && kindOf(example) !== filter.kind) return false;
  if (!filter.text) return true;

  const entry = example.entry;
  return [example.title, example.summary, example.group, entry.agent_id, entry.description,
    (entry.capabilities || []).join(" ")]
    .join(" ").toLowerCase().indexOf(filter.text) !== -1;
}

function exampleCard(example) {
  const kind = kindOf(example);

  return '<article class="gallery-item">'
    + '<div class="gallery-head"><h3>' + esc(example.title) + "</h3>"
      + '<span class="kind-tag ' + kind + '">' + esc(KIND_LABELS[kind] || kind) + "</span></div>"
    + '<p class="hint">' + esc(example.summary) + "</p>"
    + "<pre>" + esc(JSON.stringify(example.entry, null, 2)) + "</pre>"
    + (example.needs ? '<p class="hint"><strong>Needs:</strong> ' + esc(example.needs) + "</p>" : "")
    + (example.note ? '<p class="hint">' + esc(example.note) + "</p>" : "")
    + (example.ask ? '<p class="hint"><strong>Try asking:</strong> ' + esc(example.ask) + "</p>" : "")
    + '<button class="action small" data-put-example="' + esc(example.id) + '">Put this in the form</button>'
    + "</article>";
}

function renderExamples() {
  const shown = examples.filter(matches);
  const groups = [];

  shown.forEach(function (example) {
    if (groups.indexOf(example.group) === -1) groups.push(example.group);
  });

  $("examples-count").textContent = shown.length === examples.length
    ? examples.length + " examples"
    : shown.length + " of " + examples.length;

  $("examples-list").innerHTML = groups.map(function (group) {
    return '<h3 class="section-label">' + esc(group) + "</h3>"
      + '<div class="examples-grid">'
      + shown.filter(function (example) { return example.group === group; }).map(exampleCard).join("")
      + "</div>";
  }).join("") || '<div class="empty-state"><svg class="icon"><use href="#i-empty"/></svg>Nothing matches the filter.</div>';

  addCopyButtons($("examples-list"));
}

/** Show the guide or the examples, whichever the address names. */
function showHelpPage() {
  const onExamples = addressDetail() === "examples";

  $("help-guide").classList.toggle("hidden", onExamples);
  $("help-examples").classList.toggle("hidden", !onExamples);

  if (onExamples) {
    setTitle("Examples");
    loadExamples();
  }
}

export function openExamples() {
  showTab("help");
  setAddress("help", "examples");
  showHelpPage();
  window.scrollTo(0, 0);
}

hooks.shown.push(function (name) {
  if (name === "help") showHelpPage();
});

document.addEventListener("click", function (event) {
  const opener = event.target.closest && event.target.closest("[data-open-examples]");

  if (opener) {
    event.preventDefault();
    openExamples();
    return;
  }

  const put = event.target.closest && event.target.closest("[data-put-example]");
  if (!put) return;

  const example = examples.find(function (item) { return item.id === put.dataset.putExample; });
  if (example) putInForm(JSON.stringify(example.entry, null, 2));
});

$("examples-back").addEventListener("click", function (event) {
  event.preventDefault();
  showTab("help");
});

$("examples-search").addEventListener("input", function () {
  filter.text = $("examples-search").value.trim().toLowerCase();
  if (examples) renderExamples();
});

$("examples-kind").addEventListener("click", function (event) {
  const button = event.target.closest("button[data-kind]");
  if (!button) return;

  filter.kind = button.dataset.kind;
  $("examples-kind").querySelectorAll("button").forEach(function (candidate) {
    candidate.setAttribute("aria-checked", String(candidate === button));
  });
  if (examples) renderExamples();
});

hooks.paletteSources.push(function () {
  return [{ label: "Browse target examples", hint: "help", run: openExamples }];
});
