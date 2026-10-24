// The run details drawer, and the stream it is fed by.
//
// A run takes a minute or more with a local model. The stream at
// /workflow/<id>/events says what it is doing meanwhile: the model's answer as
// it is written, each step as it starts and ends, and KIO1's own log lines.
// The drawer shows all of it; the chat shows only what a person needs, and
// hears the same events through `subscribe`.

import { $, addCopyButtons, api, copyText, esc, setRows, state, statusChip } from "./core.js";

const LIVE_KINDS = [
  "status", "planning_started", "model_output", "model_answer", "plan",
  "step_started", "step_finished", "log", "finished",
  "round_started", "round_failed", "review_started", "review", "answer",
  "approval_required", "approval", "question", "question_answered",
];

export const live = {
  source: null, session: null, started: 0, streamed: false, timer: null,
  phase: "",  // what the run is doing, in a few words; the chat shows it too
  phaseName: "",  // "plan" or "review": which model call the output belongs to
};

const listeners = [];

/** Hear every event of the run being followed, and every change of phase
 * (the reading clock ticks once a second). Called with the event's data, or
 * with null when only the phase moved. */
export function subscribe(listener) {
  listeners.push(listener);
}

function notify(data) {
  listeners.forEach(function (listener) { listener(data); });
}

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

function setPhase(text) {
  live.phase = text;
  $("live-phase").textContent = text;
}

function setLiveState(kind, label, phase) {
  $("live-state").className = "chip " + kind;
  $("live-state").textContent = label;
  if (phase !== undefined) setPhase(phase);
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
      live.phaseName = "plan";
      live.streamed = data.streaming;
      setLiveState("warn", "planning", "");
      startReadingTimer(data.time * 1000, "reading the request");
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
      live.phaseName = "review";
      appendText("live-model", "\n\n— review —\n");
      setLiveState("warn", "reviewing", "");
      startReadingTimer(data.time * 1000, "reading the results");
      break;

    case "review":
      liveLine(data.decision === "continue"
        ? "Review: more work needed — " + esc(data.next_request)
        : "Review: done" + (data.fallback ? " (summary written from the results)" : ""),
        data.decision === "continue" ? "warn" : "ok");
      break;

    case "question":
      stopReadingTimer("");
      setLiveState("warn", "waiting", "waiting for your answer");
      liveLine("Question: " + esc(data.question), "warn");
      break;

    case "question_answered":
      setLiveState("warn", "planning", "");
      liveLine("Answer: " + esc(data.answer), "ok");
      break;

    case "approval_required":
      stopReadingTimer("");
      setLiveState("warn", "waiting", "waiting for your approval");
      liveLine("Waiting for approval" + (data.guarded && data.guarded.length
        ? " — " + esc(data.guarded.join(", ")) : "") + ".", "warn");
      break;

    case "approval":
      liveLine(data.approved ? "Plan approved." : "Plan not approved.", data.approved ? "ok" : "bad");
      if (data.approved) setLiveState("warn", "running", "");
      break;

    case "model_output":
      if (live.timer || !live.phase) {
        stopReadingTimer(live.phaseName === "review" ? "writing the answer" : "writing the plan");
      }
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
      setLiveState("warn", "running", "running " + data.steps.length + " step" + (data.steps.length === 1 ? "" : "s"));
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
      loadSteps();
      break;
  }

  notify(data);
}

/** Count the seconds before the model's first word.
 *
 * A local model reads the whole prompt before it writes anything — on a CPU
 * that is 30 to 60 silent seconds, which looks exactly like a hang. Saying
 * what it is doing, with a clock, is the difference. */
function startReadingTimer(since, what) {
  stopReadingTimer();

  const tick = function () {
    const seconds = Math.max(0, Math.round((Date.now() - since) / 1000));
    setPhase(what + " · " + seconds + " s");
    notify(null);
  };

  tick();
  live.timer = setInterval(tick, 1000);
}

function stopReadingTimer(phase) {
  if (live.timer) clearInterval(live.timer);
  live.timer = null;
  if (phase !== undefined) setPhase(phase);
}

export function stopFollowing() {
  stopReadingTimer();
  if (live.source) live.source.close();
  live.source = null;
}

/** Show the live stream of a run, replaying what it has done so far. */
export function followRun(sessionId) {
  if (live.session === sessionId && live.source) return;

  stopFollowing();
  live.session = sessionId;
  live.started = 0;
  live.streamed = false;
  live.phaseName = "";
  $("live-model").textContent = "";
  $("live-thinking").textContent = "";
  $("live-thinking-box").classList.add("hidden");
  $("live-log").innerHTML = "";
  $("detail-id").textContent = sessionId ? sessionId.slice(0, 8) + "…" : "";
  setLiveState("idle", "live", "");

  if (!sessionId) return;

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
      setLiveState("idle", "recorded", "");
    }
  });

  live.source = source;
}

// ── the drawer ──────────────────────────────────────────────────────────────

export function drawerOpen() {
  return $("drawer").classList.contains("open");
}

export function setDrawer(open) {
  $("drawer").classList.toggle("open", open);
  placeDrawer();
  $("details-toggle").setAttribute("aria-expanded", String(open));
  if (open) loadSteps();
}

// ── beside the chat, or over it ─────────────────────────────────────────────

const DRAWER_WIDTH_KEY = "kio1.drawer.width";
const DRAWER_MIN = 340;
const DRAWER_MAX = 1100;
// Narrower than this and an answer breaks a word per line.
const CHAT_MIN = 420;
const DOCK_FROM = 1200;

/** Tell the stylesheet whether the open drawer sits beside the chat or over it.
 *
 * Beside it while the chat keeps a readable width, so the drawer hides
 * nothing. Widened past that — someone reading a long output — it goes over
 * the chat, as it does on a narrow screen.
 */
function placeDrawer() {
  const root = document.documentElement;

  if (!drawerOpen()) {
    delete root.dataset.drawer;
    return;
  }

  const chatStart = $("threads").getBoundingClientRect().right;
  const fits = window.innerWidth >= DOCK_FROM
    && window.innerWidth - chatStart - $("drawer").getBoundingClientRect().width >= CHAT_MIN;

  root.dataset.drawer = fits ? "docked" : "over";
}

/** Set the drawer's width, between a readable minimum and nearly the window. */
function setDrawerWidth(width, remember) {
  const max = Math.max(DRAWER_MIN, Math.min(DRAWER_MAX, window.innerWidth - 48));
  const clamped = Math.round(Math.min(max, Math.max(DRAWER_MIN, width)));

  document.documentElement.style.setProperty("--drawer-width", clamped + "px");
  placeDrawer();
  if (remember) {
    try { localStorage.setItem(DRAWER_WIDTH_KEY, String(clamped)); } catch { /* storage blocked */ }
  }
  return clamped;
}

window.addEventListener("resize", placeDrawer);

function drawerWidth() {
  return $("drawer").getBoundingClientRect().width;
}

try {
  const saved = Number(localStorage.getItem(DRAWER_WIDTH_KEY));
  if (saved) setDrawerWidth(saved, false);
} catch { /* storage blocked: the default width */ }

$("drawer-resize").addEventListener("pointerdown", function (event) {
  event.preventDefault();
  const handle = event.currentTarget;
  // Captured so the drag keeps going when the pointer leaves the thin edge.
  try { handle.setPointerCapture(event.pointerId); } catch { /* a synthetic event */ }
  document.documentElement.classList.add("drawer-resizing");

  function move(moved) { setDrawerWidth(window.innerWidth - moved.clientX, false); }
  function stop() {
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", stop);
    handle.removeEventListener("pointercancel", stop);
    document.documentElement.classList.remove("drawer-resizing");
    setDrawerWidth(drawerWidth(), true);
  }

  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
});

// The keyboard way to do the same: arrows widen and narrow it.
$("drawer-resize").addEventListener("keydown", function (event) {
  const step = event.shiftKey ? 120 : 40;
  if (event.key === "ArrowLeft") setDrawerWidth(drawerWidth() + step, true);
  else if (event.key === "ArrowRight") setDrawerWidth(drawerWidth() - step, true);
  else return;
  event.preventDefault();
});

/** Fill the drawer's step table with what was recorded for the run. */
export async function loadSteps() {
  if (!drawerOpen()) return;

  if (!state.selected) {
    setRows($("steps-body"), '<tr><td colspan="4" class="muted small">No run yet.</td></tr>');
    return;
  }

  try {
    const detail = await api("/workflow/" + encodeURIComponent(state.selected));
    const steps = detail.steps || [];

    setRows($("steps-body"), steps.map(function (step) {
      const output = step.output
        ? '<details><summary>Output</summary><pre class="output">'
          + esc(JSON.stringify(step.output, null, 2)) + "</pre></details>"
        : "";
      const error = step.error ? '<div class="reason">' + esc(step.error) + "</div>" : "";
      // Error and output go on a row of their own, as wide as the table.
      const more = error || output
        ? '<tr class="step-more"><td colspan="4">' + error + output + "</td></tr>"
        : "";

      return '<tr class="' + (more ? "step-main" : "") + '">'
        + '<td class="mono small">' + esc(step.step_id) + "</td>"
        + '<td><span class="target-name">' + esc(step.agent_id) + "</span>"
          + '<div class="mono small muted">' + esc(step.capability) + "</div></td>"
        + "<td>" + statusChip(step.status) + "</td>"
        + '<td class="num small">' + esc(step.duration_ms) + " ms</td>"
        + "</tr>" + more;
    }).join("") || '<tr><td colspan="4" class="muted small">No steps recorded yet.</td></tr>');

    addCopyButtons($("steps-body"));
  } catch (error) {
    setRows($("steps-body"), '<tr><td colspan="4" class="reason">' + esc(error.message) + "</td></tr>");
  }
}

$("live-logs").addEventListener("change", function () {
  $("live-log").classList.toggle("no-logs", !$("live-logs").checked);
});

$("details-toggle").addEventListener("click", function () { setDrawer(!drawerOpen()); });
$("drawer-close").addEventListener("click", function () { setDrawer(false); });

$("detail-id").addEventListener("click", function () {
  if (state.selected) copyText(state.selected, "Session id copied.");
});

document.addEventListener("keydown", function (event) {
  if (event.key === "Escape" && drawerOpen() && !document.querySelector("dialog[open]")) setDrawer(false);
});
