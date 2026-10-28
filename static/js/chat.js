// The Run page: a conversation with KIO1.
//
// Each request is a turn: what was asked, a card with the plan and its steps
// as they run, and the answer. A later request continues the conversation, so
// "now add type hints" is understood. What the model writes and the log lines
// stay in the run details drawer; the chat keeps only what a person needs.

import { attachmentIds, clearAttachments, readyCapabilities, uploading } from "./composer.js";
import { $, addCopyButtons, api, ask, copyText, esc, hooks, relativeTime, setRows, state, statusChip, toast } from "./core.js";
import { followRun, live, loadSteps, setDrawer, subscribe } from "./live.js";
import { setAmbient } from "./ambient.js";
import { renderMarkdown } from "./markdown.js";
import { addressDetail, setAddress, setTitle, showTab } from "./shell.js";

const chat = {
  conversations: [],  // the list on the left, newest first
  current: null,      // the open conversation's id, or null for a new chat
  detail: null,       // the open conversation with its runs
  pending: null,      // a request on its way, shown before the server has it
  sending: false,
  steps: {},          // session id -> its steps as heard live, before they are recorded
  modes: {},          // session id -> the plan's execution mode, as heard live
  open: new Map(),    // session id -> whether the reader opened or closed its plan card
  waiting: null,      // the run an approval or a question belongs to
  stopping: null,     // the run asked to stop, until it has
  phase: "",          // the phase last drawn, so streamed text does not redraw
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** A run the person stopped ended as they asked: it did not fail. */
function wasStopped(session) {
  return session.status === "failed" && session.error === "Stopped.";
}

function isActive(session) {
  return session.status === "running" || session.status === "pending";
}

// ── the conversation list ───────────────────────────────────────────────────

function isToday(value) {
  return new Date(value).toDateString() === new Date().toDateString();
}

function threadItem(conversation) {
  const id = esc(conversation.conversation_id);
  const active = conversation.conversation_id === chat.current ? " active" : "";

  return '<li class="thread' + active + '" data-conversation="' + id + '" tabindex="0">'
    + '<span class="title">' + esc(conversation.title || "New chat") + "</span>"
    + '<span class="when">' + esc(relativeTime(conversation.updated_at)) + "</span>"
    + '<button class="icon-button thread-delete" data-delete-conversation="' + id
      + '" title="Delete" aria-label="Delete this conversation">'
      + '<svg class="icon"><use href="#i-trash"/></svg></button>'
    + "</li>";
}

function threadGroup(name, items) {
  return items.length
    ? '<div class="group">' + name + "</div><ol>" + items.map(threadItem).join("") + "</ol>"
    : "";
}

function renderThreads() {
  const today = chat.conversations.filter(function (item) { return isToday(item.updated_at); });
  const earlier = chat.conversations.filter(function (item) { return !isToday(item.updated_at); });

  setRows($("thread-list"), threadGroup("Today", today) + threadGroup("Earlier", earlier)
    || '<p class="small muted thread-empty">No conversations yet.</p>');
}

async function loadConversations() {
  try {
    const data = await api("/conversations?limit=50");
    chat.conversations = data.conversations || [];
    renderThreads();
  } catch (error) {
    setRows($("thread-list"), '<p class="small reason">' + esc(error.message) + "</p>");
  }
}

function setThreadsOpen(open) {
  $("threads").classList.toggle("open", open);
}

// ── turns ───────────────────────────────────────────────────────────────────

const DOTS = {
  ok: ["ok", "✓"], error: ["bad", "✗"], running: ["run", "▶"],
  waiting: ["wait", "·"], skipped: ["wait", "–"], not_run: ["wait", "–"],
};

function shortDuration(ms) {
  if (!isFinite(ms) || ms <= 0) return "";
  if (ms < 1000) return ms + " ms";
  if (ms < 120000) return Math.round(ms / 1000) + " s";
  return Math.round(ms / 60000) + " min";
}

/** How long a run took — except one a restart cut off, which was closed when
 * KIO1 came back, so the gap measures the downtime, not the run. */
function durationOf(session) {
  if (/KIO1 stopped while/.test(session.error || "")) return "";
  return shortDuration(new Date(session.updated_at) - new Date(session.created_at));
}

/** Citations such as [Python] as small tags, so it is clear which target
 * said what. Given escaped prose only — code is set aside before this sees
 * it: `list[float]` is a type, not a target called "float". */
export function markSources(html) {
  return html.replace(
    /\[([A-Za-z0-9_.-]{1,40}(?:,\s*[A-Za-z0-9_.-]{1,40})*)\]/g,
    function (match, names) {
      return names.split(/,\s*/).map(function (name) {
        return '<span class="source">' + name + "</span>";
      }).join("");
    });
}

/** An answer as Markdown — lists, tables, code — with its sources marked. */
function formatAnswer(text) {
  return renderMarkdown(text, { citations: markSources });
}

const MODES = { sequential: "in order", parallel: "in parallel", mixed: "partly in parallel" };

/** One step of a plan, numbered, with what it was asked to do.
 *
 * `numbers` maps step ids to their number, so "after 1" can name a step a
 * person can find in the list rather than an id like "r2-s1". */
function stepItem(step, index, numbers) {
  const dot = DOTS[step.status] || DOTS.waiting;
  const after = (step.depends_on || [])
    .filter(function (id) { return numbers[id]; })
    .map(function (id) { return numbers[id]; });

  return '<li class="' + dot[0] + '">'
    + '<span class="step-number" title="' + esc(step.status) + '">'
      + (step.status === "error" ? dot[1] : index + 1) + "</span>"
    + '<div class="step-body">'
      + '<div class="step-head"><strong>' + esc(step.agent_id) + "</strong>"
        + ' <span class="small muted">· ' + esc(step.capability) + "</span>"
        + (after.length ? ' <span class="after">after ' + after.join(", ") + "</span>" : "")
      + "</div>"
      + (step.task ? '<div class="step-task" title="' + esc(step.task) + '">' + esc(step.task) + "</div>" : "")
      + (step.error ? '<div class="step-error">' + esc(step.error) + "</div>" : "")
    + "</div>"
    + '<span class="small mono muted">' + esc(shortDuration(step.duration_ms)) + "</span></li>";
}

/** The steps a run recorded, each with the steps the plan said it uses.
 *
 * Only declared dependencies: an edge inferred from execution order says
 * nothing a numbered list "in order" does not already say. */
function recordedSteps(item) {
  const uses = {};

  (item.lineage || []).forEach(function (edge) {
    if (edge.basis !== "declared_dependency") return;
    (uses[edge.consumed_by_step_id] = uses[edge.consumed_by_step_id] || []).push(edge.produced_by_step_id);
  });

  return (item.steps || []).map(function (step) {
    return Object.assign({}, step, { depends_on: uses[step.step_id] || [] });
  });
}

// What a run's error means for the person reading it, most specific first.
// The raw text stays one click away: it is what a bug report needs.
const EXPLAINED = [
  [/^Stopped\.$/, "You stopped this run."],
  [/KIO1 stopped while/, "KIO1 was restarted while this was running. Ask again."],
  [/plan was not approved/i, "The plan was cancelled."],
  [/question was not answered/i, "The question got no answer, so the request stopped."],
  [/needs more information|still needs more/i, "KIO1 needed more details. Ask again with them."],
  [/max_output_tokens|context window/i, "The model ran out of room before it finished. Ask again, or choose another model in Settings."],
  [/timed? ?out|TimeoutError/i, "The model took too long. A smaller or a cloud model is faster — see Settings."],
  [/\b401\b|\b403\b|api key|unauthori[sz]ed|authentication/i, "The model provider refused the key. Check it in Settings."],
  [/connection refused|actively refused|URLError|ConnectError|10061|unreachable/i, "The model could not be reached. Is Ollama running? See Settings."],
  [/json|plan requires|Workflow plan|A review must/i, "The model did not write a usable plan. Ask again, or try a larger model."],
];

/** A run's error as one plain sentence, with the raw text folded below it. */
function explainError(raw) {
  const found = EXPLAINED.find(function (entry) { return entry[0].test(raw); });
  const text = found ? found[1] : "Something went wrong.";

  return esc(text)
    + '<details class="error-detail"><summary>Details</summary><pre class="output">' + esc(raw) + "</pre></details>";
}

/** Why a run that failed failed, as markup, or nothing. */
function failureNote(item) {
  const session = item.session;
  const steps = item.steps || [];

  if (session.error) return explainError(session.error);
  if (session.status !== "failed") return "";
  if (!steps.length) return "This run failed before any step was recorded.";

  const unreachable = steps.some(function (step) {
    return (step.error || "").indexOf("unreachable") !== -1;
  });

  return unreachable ? "Some targets did not answer. Are they running? See Targets." : "";
}

function planCard(item, isLast) {
  const session = item.session;
  const id = session.session_id;
  const active = isActive(session);
  const following = active && live.session === id;
  const steps = following && chat.steps[id] ? chat.steps[id] : recordedSteps(item);
  const mode = steps.length > 1 ? MODES[(following && chat.modes[id]) || session.execution_mode] : "";
  const phase = active
    ? (following && live.phase) || (session.status === "pending" ? "queued" : "working…")
    : durationOf(session);
  // Open while it runs, so the steps can be watched, and when it failed, so
  // the step that broke is in view. A run that went well is one line: the
  // answer below it is what the reader came for.
  const open = chat.open.has(id) ? chat.open.get(id) : active || session.status === "failed";
  const numbers = {};
  steps.forEach(function (step, index) { numbers[step.step_id] = index + 1; });

  return '<details class="plan-card" data-session="' + esc(id) + '"' + (open ? " open" : "") + ">"
    + "<summary>" + statusChip(wasStopped(session) ? "stopped" : session.status)
      + (steps.length ? "<strong>" + steps.length + " step" + (steps.length === 1 ? "" : "s") + "</strong>" : "")
      + [mode, phase].filter(Boolean).map(function (part, index) {
        return '<span class="small muted">' + (steps.length || index ? "· " : "") + esc(part) + "</span>";
      }).join("")
    + "</summary>"
    + (steps.length
      ? '<ol class="timeline' + (steps.length > 1 ? " many" : "") + '">'
        + steps.map(function (step, index) { return stepItem(step, index, numbers); }).join("") + "</ol>"
      : "")
    + "</details>";
}

function turnHtml(item, isLast) {
  const session = item.session;
  const id = esc(session.session_id);
  const note = failureNote(item);
  const actions = isActive(session) ? "" : '<div class="msg-actions">'
    + (session.answer ? '<button class="icon-button" data-copy="' + id + '" title="Copy the answer" aria-label="Copy the answer"><svg class="icon"><use href="#i-copy"/></svg></button>' : "")
    + '<button class="icon-button" data-retry="' + id + '" title="Ask again" aria-label="Ask again"><svg class="icon"><use href="#i-retry"/></svg></button>'
    + '<button class="icon-button" data-details="' + id + '" title="Run details" aria-label="Run details"><svg class="icon"><use href="#i-panel"/></svg></button>'
    + "</div>";

  return userMessage(session.request_text, session.attachments)
    + '<div class="msg-kio">'
      + '<div class="who"><span class="mark">K1</span>KIO1</div>'
      + planCard(item, isLast)
      + (note ? '<div class="notice small' + (wasStopped(session) ? "" : " bad") + '">' + note + "</div>" : "")
      + (session.answer ? '<div class="answer-bubble">' + formatAnswer(session.answer) + "</div>" : "")
      + actions
    + "</div>";
}

/** What the person wrote, its @Name choices marked, and the files they
 * attached below it. An address like a@b.org is not a choice: a name only
 * counts at the start of a word, as the service reads it. */
function userMessage(text, files) {
  const marked = esc(text).replace(/(^|\s)@([\w.-]+)/g, function (match, space, name) {
    return space + '<span class="mention">@' + name + "</span>";
  });

  return '<div class="msg-user">' + marked + "</div>"
    + ((files || []).length
      ? '<div class="msg-files">' + files.map(function (file) {
          return '<span class="file-chip"><svg class="icon"><use href="#i-clip"/></svg>'
            + '<span class="name">' + esc(file.name) + "</span></span>";
        }).join("") + "</div>"
      : "");
}

function pendingHtml(request) {
  return userMessage(request)
    + '<div class="msg-kio"><div class="who"><span class="mark">K1</span>KIO1</div>'
    + '<div class="plan-card"><div class="summary">' + statusChip("pending")
    + '<span class="small muted">sending…</span></div></div></div>';
}

/** Offer only the examples something ready can do: a weather example with
 * no weather agent ends in an error, which is a poor first run. When the
 * agents cannot be listed, every example stays. */
async function showUsableExamples() {
  const ready = await readyCapabilities();

  document.querySelectorAll("#run-examples [data-needs]").forEach(function (button) {
    const usable = !ready || button.dataset.needs.split(" ").some(function (need) { return ready.has(need); });
    button.classList.toggle("hidden", !usable);
  });
}

/** Draw the turns, each in an element of its own.
 *
 * Only a turn whose markup changed is rewritten: the running one changes
 * every second while the model reads, and rewriting the whole conversation
 * would close the cards the reader opened and lose a text selection. */
function renderTurns() {
  const box = $("turns");
  const messages = $("messages");
  const items = chat.detail ? chat.detail.sessions || [] : [];
  const turns = items.map(function (item, index) {
    return ["s:" + item.session.session_id, turnHtml(item, index === items.length - 1 && !chat.pending)];
  });

  if (chat.pending) turns.push(["pending", pendingHtml(chat.pending)]);

  const atBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
  const before = box.children.length;

  turns.forEach(function (turn, index) {
    let element = box.children[index];

    if (!element || element.dataset.turn !== turn[0]) {
      element = document.createElement("div");
      element.className = "turn";
      element.dataset.turn = turn[0];
      box.insertBefore(element, box.children[index] || null);
    }

    setRows(element, turn[1]);
    // Code blocks in an answer get a copy button, as everywhere else.
    addCopyButtons(element);
  });

  while (box.children.length > turns.length) box.lastElementChild.remove();

  $("chat-empty").classList.toggle("hidden", turns.length > 0);
  setAmbient(!turns.length);
  if (chat.stopping && chat.stopping !== activeRun()) chat.stopping = null;
  renderSendButton();
  if (!turns.length) showUsableExamples();
  if (state.tab === "workflows") {
    setTitle(chat.detail ? chat.detail.conversation.title || "New chat" : "New chat");
  }

  // Follow the conversation down as it grows, unless the reader scrolled up
  // to read something earlier.
  if (atBottom || box.children.length !== before) messages.scrollTop = messages.scrollHeight;
}

/** The run under way in the open conversation, or null. */
function activeRun() {
  const items = chat.detail ? chat.detail.sessions || [] : [];
  const running = items.filter(function (item) { return isActive(item.session); }).pop();
  return running ? running.session.session_id : null;
}

/** Send becomes Stop while a run is under way in this conversation. */
function renderSendButton() {
  const button = $("run");
  const running = activeRun();
  const stopping = Boolean(running) && chat.stopping === running;
  const mode = running ? "stop" : "send";

  if (button.dataset.mode !== mode || button.dataset.stopping !== String(stopping)) {
    button.dataset.mode = mode;
    button.dataset.stopping = String(stopping);
    button.classList.toggle("stop", Boolean(running));
    button.innerHTML = running
      ? '<svg class="icon"><use href="#i-stop"/></svg>' + (stopping ? "Stopping…" : "Stop")
      : '<svg class="icon"><use href="#i-run"/></svg>Send';
    button.title = running ? "Stop this run" : "Send (Enter)";
  }

  button.disabled = chat.sending || stopping;
}

/** Ask the run to stop; it ends at its next safe point. */
async function stopRun(sessionId) {
  if (!sessionId) return;

  chat.stopping = sessionId;
  renderSendButton();

  try {
    await api("/workflow/" + encodeURIComponent(sessionId) + "/cancel", { method: "POST" });
  } catch (error) {
    // Already over: the next refresh shows how it ended.
    if (error.status !== 409) toast(error.message, "bad");
  }

  hideQuestion();
  hideApproval();
  loadConversation();
}

function findTurn(sessionId) {
  const items = chat.detail ? chat.detail.sessions || [] : [];
  return items.find(function (item) { return item.session.session_id === sessionId; }) || null;
}

// ── opening, starting, sending ──────────────────────────────────────────────

/** Show a run in the drawer and follow its stream. */
function select(sessionId) {
  state.selected = sessionId;
  followRun(sessionId);
  loadSteps();
}

export function newChat() {
  chat.current = null;
  chat.detail = null;
  select(null);
  hideApproval();
  hideQuestion();
  setAddress("workflows");
  setThreadsOpen(false);
  renderThreads();
  renderTurns();
  $("request").focus();
}

function openConversation(conversationId) {
  if (chat.current === conversationId) { setThreadsOpen(false); return; }

  chat.current = conversationId;
  chat.detail = null;
  hideApproval();
  hideQuestion();
  setAddress("workflows", conversationId);
  setThreadsOpen(false);
  renderThreads();
  renderTurns();
  loadConversation();
}

async function loadConversation(options) {
  // The address can name a conversation: a reload, a shared link, or the
  // back button lands where it was.
  const named = addressDetail();
  if (UUID.test(named) && named !== chat.current) {
    chat.current = named;
    chat.detail = null;
  }

  if (state.tab === "workflows") setAddress("workflows", chat.current || "");

  if (!chat.current) {
    chat.detail = null;
    renderTurns();
    return;
  }

  const id = chat.current;
  let detail;

  try {
    detail = await api("/conversations/" + encodeURIComponent(id));
  } catch (error) {
    if (chat.current !== id) return;
    if (error.status === 404) {
      toast("That conversation no longer exists.", "warn");
      newChat();
    } else {
      $("run-result").className = "small reason";
      $("run-result").textContent = error.message;
    }
    return;
  }

  if (chat.current !== id) return;
  if (options && options.sent) chat.pending = null;
  chat.detail = detail;

  const items = detail.sessions || [];
  const last = items.length ? items[items.length - 1].session : null;
  const ids = items.map(function (item) { return item.session.session_id; });

  // The run in the drawer belongs to this conversation, and a run still
  // going is followed so its card moves.
  if (last && (ids.indexOf(state.selected) === -1 || (isActive(last) && state.selected !== last.session_id))) {
    select(last.session_id);
  }

  if (last && isActive(last)) {
    loadWaiting(last.session_id);
  } else {
    hideApproval();
    hideQuestion();
  }

  renderTurns();
}

/** Send a request: the composer's with its files, or the one given with
 * the files given — "ask again" sends a turn's own. The composer is only
 * emptied when it is what was sent. */
export async function send(text, files) {
  const composing = text === undefined;
  const request = (composing ? $("request").value : text).trim();
  if (!request || chat.sending) return;

  if (composing && uploading()) {
    $("run-result").className = "small muted";
    $("run-result").textContent = "Waiting for the file to upload…";
    return;
  }

  const attachments = composing ? attachmentIds() : files || [];

  chat.sending = true;
  chat.pending = request;
  $("run-result").textContent = "";
  if (composing) {
    $("request").value = "";
    autoGrow();
  }
  renderTurns();

  try {
    const body = { request: request };
    if (chat.current) body.conversation_id = chat.current;
    if (attachments.length) body.attachments = attachments;

    const data = await api("/workflow/run", { method: "POST", body: JSON.stringify(body) });
    if (composing) clearAttachments();
    chat.current = data.conversation_id;
    select(data.session_id);
    await loadConversation({ sent: true });
  } catch (error) {
    if (composing) {
      $("request").value = request;
      autoGrow();
    }
    $("run-result").className = "small reason";
    $("run-result").textContent = error.message;
  } finally {
    chat.pending = null;
    chat.sending = false;
    renderTurns();
    loadConversations();
  }
}

/** Grow the box with what is typed, up to the height the stylesheet allows. */
function autoGrow() {
  const box = $("request");
  box.style.height = "auto";
  box.style.height = box.scrollHeight + "px";
}

export function loadChat() {
  loadConversations();
  loadConversation();
}

// ── approval and questions ──────────────────────────────────────────────────

/** Show a plan waiting for a yes, its marked targets highlighted. */
function showApproval(plan, expiresAt) {
  const box = $("detail-approval");
  const guarded = plan.guarded || [];
  const key = JSON.stringify([plan.workflow_id, (plan.steps || []).map(function (s) { return s.step_id; })]);

  if (box.dataset.key !== key) {
    setRows($("approval-steps"), (plan.steps || []).map(function (step) {
      const marked = guarded.indexOf(step.agent_id) !== -1;
      return "<li" + (marked ? ' class="guarded"' : "") + ">"
        + '<span class="target-name">' + esc(step.agent_id) + "</span>"
        + '<span class="mono small muted"> · ' + esc(step.capability) + "</span>"
        + '<div class="small">' + esc(step.task) + "</div></li>";
    }).join(""));
    box.dataset.key = key;
  }

  $("approval-expires").textContent = expiresAt
    ? "cancels itself at " + new Date(expiresAt * 1000).toLocaleTimeString() : "";
  box.classList.remove("hidden");
}

function hideApproval() {
  $("detail-approval").classList.add("hidden");
  delete $("detail-approval").dataset.key;
}

function showQuestion(text) {
  const box = $("detail-question");
  const fresh = box.classList.contains("hidden") || $("question-text").textContent !== text;

  $("question-text").textContent = text;
  box.classList.remove("hidden");

  // Focus only when it first appears: the refresh timer calls this every few
  // seconds, and stealing the caret back each time would fight the reader.
  if (fresh) $("question-answer").focus({ preventScroll: true });
}

function hideQuestion() {
  $("detail-question").classList.add("hidden");
}

/** Show what a running run is waiting for: an approval, or an answer —
 * including for a run opened after a reload, which missed the live event. */
async function loadWaiting(sessionId) {
  try {
    const pending = await api("/workflow/" + encodeURIComponent(sessionId) + "/waiting");
    if (!findTurn(sessionId)) return;

    chat.waiting = sessionId;

    if (pending.waiting && pending.kind === "question") {
      hideApproval();
      showQuestion(pending.plan.question);
    } else if (pending.waiting) {
      hideQuestion();
      showApproval(pending.plan, pending.expires_at);
    } else {
      hideApproval();
      hideQuestion();
    }
  } catch {
    hideApproval();
    hideQuestion();
  }
}

async function decideApproval(approve) {
  const session = chat.waiting;
  if (!session) return;

  $("approval-run").disabled = $("approval-cancel").disabled = true;

  try {
    await api("/workflow/" + encodeURIComponent(session) + "/approval", {
      method: "POST",
      body: JSON.stringify({ approve: approve }),
    });
    hideApproval();
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    $("approval-run").disabled = $("approval-cancel").disabled = false;
  }
}

$("approval-run").addEventListener("click", function () { decideApproval(true); });
$("approval-cancel").addEventListener("click", function () { decideApproval(false); });

$("detail-question").addEventListener("submit", async function (event) {
  event.preventDefault();

  const text = $("question-answer").value.trim();
  if (!text || !chat.waiting) return;

  $("question-send").disabled = true;

  try {
    await api("/workflow/" + encodeURIComponent(chat.waiting) + "/answer", {
      method: "POST",
      body: JSON.stringify({ text: text }),
    });
    $("question-answer").value = "";
    hideQuestion();
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    $("question-send").disabled = false;
  }
});

// ── what the stream says ────────────────────────────────────────────────────

subscribe(function (data) {
  const sessionId = live.session;
  const item = findTurn(sessionId);

  if (data) {
    switch (data.kind) {
      case "plan":
        // A later round adds its steps after the first round's.
        chat.steps[sessionId] = (data.round > 1 ? chat.steps[sessionId] || [] : []).concat(
          data.steps.map(function (step) {
            return {
              step_id: step.step_id,
              agent_id: step.agent_id,
              capability: step.capability,
              task: step.task,
              depends_on: step.depends_on || [],
              status: "waiting",
            };
          }));
        if (data.round <= 1) chat.modes[sessionId] = data.execution_mode;
        break;

      case "step_started":
      case "step_finished": {
        const step = (chat.steps[sessionId] || []).find(function (s) { return s.step_id === data.step_id; });
        if (step) {
          step.agent_id = data.agent_id || step.agent_id;
          step.status = data.kind === "step_started" ? "running" : data.status;
          if (data.duration_ms) step.duration_ms = data.duration_ms;
          if (data.error) step.error = data.error;
        }
        break;
      }

      case "status":
        if (item) item.session.status = data.status;
        break;

      case "answer":
        if (item) item.session.answer = data.text;
        break;

      case "approval_required":
        chat.waiting = sessionId;
        if (item) showApproval(data, data.time + data.timeout);
        break;

      case "approval":
        hideApproval();
        break;

      case "question":
        chat.waiting = sessionId;
        if (item) showQuestion(data.question);
        break;

      case "question_answered":
        hideQuestion();
        break;

      case "finished":
        loadConversation();
        loadConversations();
        break;

      case "model_output":
      case "log":
        // Streamed text belongs to the drawer; the chat only redraws when
        // it changes what the card says.
        if (live.phase === chat.phase) return;
        break;
    }
  }

  chat.phase = live.phase;
  if (item && state.tab === "workflows") renderTurns();
});

// ── events ──────────────────────────────────────────────────────────────────

$("run").addEventListener("click", function () {
  const running = activeRun();
  if (running) stopRun(running); else send();
});

$("question-stop").addEventListener("click", function () { stopRun(chat.waiting); });

$("request").addEventListener("keydown", function (event) {
  // Enter sends, Shift+Enter starts a new line. Not while an input method
  // is composing: there Enter picks the word.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    send();
  }
});

$("request").addEventListener("input", autoGrow);

$("run-examples").addEventListener("click", function (event) {
  const example = event.target.closest("[data-example]");
  if (!example) return;

  $("request").value = example.dataset.example;
  autoGrow();
  $("request").focus();
});

$("new-chat").addEventListener("click", newChat);
$("threads-toggle").addEventListener("click", function () {
  setThreadsOpen(!$("threads").classList.contains("open"));
});

$("thread-list").addEventListener("click", async function (event) {
  const remove = event.target.closest("[data-delete-conversation]");

  if (remove) {
    const id = remove.dataset.deleteConversation;
    const found = chat.conversations.find(function (item) { return item.conversation_id === id; });
    const confirmed = await ask({
      title: "Delete this conversation?",
      text: (found && found.title ? "“" + found.title + "” and " : "It and ") + "every run in it are removed.",
      ok: "Delete",
      danger: true,
    });

    if (confirmed === null) return;

    try {
      await api("/conversations/" + encodeURIComponent(id), { method: "DELETE" });
      if (chat.current === id) newChat();
      loadConversations();
    } catch (error) {
      toast(error.status === 401 ? "Deleting needs the administration token." : error.message, "bad");
    }
    return;
  }

  const item = event.target.closest("[data-conversation]");
  if (item) openConversation(item.dataset.conversation);
});

$("thread-list").addEventListener("keydown", function (event) {
  const item = event.target.closest("[data-conversation]");
  if (item && event.target === item && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    openConversation(item.dataset.conversation);
  }
});

$("turns").addEventListener("click", function (event) {
  const button = event.target.closest("[data-copy], [data-retry], [data-details]");
  if (!button) return;

  const item = findTurn(button.dataset.copy || button.dataset.retry || button.dataset.details);
  if (!item) return;

  if (button.dataset.copy) copyText(item.session.answer, "Answer copied.");
  if (button.dataset.retry) {
    send(item.session.request_text, (item.session.attachments || []).map(function (file) {
      return file.attachment_id;
    }));
  }
  if (button.dataset.details) {
    select(item.session.session_id);
    setDrawer(true);
  }
});

// `toggle` does not bubble, so it is caught on the way down.
$("turns").addEventListener("toggle", function (event) {
  const card = event.target;
  if (card.classList && card.classList.contains("plan-card") && card.dataset.session) {
    chat.open.set(card.dataset.session, card.open);
  }
}, true);

hooks.paletteSources.push(function () {
  return [{ label: "New chat", hint: "run", run: function () { showTab("workflows"); newChat(); } }]
    .concat(chat.conversations.slice(0, 15).map(function (conversation) {
      return {
        label: "Open: " + (conversation.title || "New chat"),
        hint: "chat",
        run: function () { showTab("workflows"); openConversation(conversation.conversation_id); },
      };
    }));
});
