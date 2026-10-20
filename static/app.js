(() => {
  "use strict";

  const messagesEl = document.getElementById("messages");
  const formEl = document.getElementById("chat-form");
  const inputEl = document.getElementById("query-input");
  const fileInputEl = document.getElementById("file-input");
  const uploadStatusEl = document.getElementById("upload-status");

  function escapeHtml(value) {
    return value
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // Tokenizes an already-pretty-printed JSON string and wraps each token in
  // a span so CSS can color keys/strings/numbers/booleans/null distinctly.
  function highlightJson(jsonString) {
    const escaped = escapeHtml(jsonString);
    const tokenPattern =
      /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

    return escaped.replace(tokenPattern, (match) => {
      let cssClass = "json-number";
      if (match.startsWith("&quot;") || match.startsWith('"')) {
        cssClass = /:\s*$/.test(match) ? "json-key" : "json-string";
      } else if (match === "true" || match === "false") {
        cssClass = "json-boolean";
      } else if (match === "null") {
        cssClass = "json-null";
      }
      return `<span class="${cssClass}">${match}</span>`;
    });
  }

  // Recognizes the KIO1 workflow-plan shape so it can be rendered as step
  // cards instead of raw JSON; anything else falls back to highlightJson.
  function isWorkflowPlan(parsed) {
    return Boolean(
      parsed &&
        typeof parsed === "object" &&
        typeof parsed.workflow_id === "string" &&
        typeof parsed.execution_mode === "string" &&
        Array.isArray(parsed.steps)
    );
  }

  function renderPlanStep(step, index) {
    const stepId = escapeHtml(String(step?.step_id ?? index + 1));
    const agentId = escapeHtml(String(step?.agent_id ?? "?"));
    const task = escapeHtml(String(step?.task ?? ""));
    const capability =
      step && step.capability
        ? `<span class="step-capability">${escapeHtml(String(step.capability))}</span>`
        : "";

    return `
      <li class="plan-step">
        <span class="step-index">${index + 1}</span>
        <div class="step-body">
          <div class="step-meta">
            <span class="step-agent">${agentId}</span>
            ${capability}
            <span class="step-id">${stepId}</span>
          </div>
          <p class="step-task">${task}</p>
        </div>
      </li>`;
  }

  function renderWorkflowPlan(plan, rawJson) {
    const stepsHtml = plan.steps.map(renderPlanStep).join("");
    const explanation =
      typeof plan.explanation === "string" && plan.explanation
        ? `<p class="plan-explanation">${escapeHtml(plan.explanation)}</p>`
        : "";

    return `
      <div class="plan-card">
        <div class="plan-header">
          <span class="plan-id">${escapeHtml(plan.workflow_id)}</span>
          <span class="plan-mode">${escapeHtml(plan.execution_mode)}</span>
        </div>
        <ol class="plan-steps">${stepsHtml}</ol>
        ${explanation}
        <details class="plan-raw">
          <summary>View raw JSON</summary>
          <pre>${highlightJson(rawJson)}</pre>
        </details>
      </div>`;
  }

  function renderFormattedResponse(formattedJson) {
    let parsed;
    try {
      parsed = JSON.parse(formattedJson);
    } catch {
      parsed = null;
    }

    if (isWorkflowPlan(parsed)) {
      return renderWorkflowPlan(parsed, formattedJson);
    }
    return `<pre>${highlightJson(formattedJson)}</pre>`;
  }

  function appendMessage(role, { text, html, isError = false } = {}) {
    const wrapper = document.createElement("div");
    wrapper.className = `message message-${role}`;

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (html !== undefined) {
      bubble.classList.add("bubble-json");
      if (isError) bubble.classList.add("bubble-error");
      bubble.innerHTML = html;
    } else {
      if (isError) bubble.classList.add("bubble-error");
      bubble.textContent = text ?? "";
    }

    wrapper.appendChild(bubble);
    messagesEl.appendChild(wrapper);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setUploadStatus(message, isError) {
    uploadStatusEl.textContent = message;
    uploadStatusEl.classList.toggle("error", Boolean(isError));
  }

  async function sendQuery(query) {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });

    let data;
    try {
      data = await response.json();
    } catch {
      data = { error: `Unexpected response (HTTP ${response.status}).` };
    }

    if (data.formatted) {
      appendMessage("assistant", { html: renderFormattedResponse(data.formatted) });
      return;
    }

    if (data.raw) {
      appendMessage("assistant", {
        text: `${data.error ?? "Could not format response."}\n\n${data.raw}`,
        isError: true,
      });
      return;
    }

    appendMessage("assistant", {
      text: data.error ?? `Request failed (HTTP ${response.status}).`,
      isError: true,
    });
  }

  formEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = inputEl.value.trim();
    if (!query) return;

    appendMessage("user", { text: query });
    inputEl.value = "";
    inputEl.disabled = true;
    formEl.querySelector("button").disabled = true;

    try {
      await sendQuery(query);
    } catch {
      appendMessage("assistant", {
        text: "Network error: could not reach the orchestrator.",
        isError: true,
      });
    } finally {
      inputEl.disabled = false;
      formEl.querySelector("button").disabled = false;
      inputEl.focus();
    }
  });

  inputEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      formEl.requestSubmit();
    }
  });

  fileInputEl.addEventListener("change", async () => {
    const file = fileInputEl.files[0];
    if (!file) return;

    const body = new FormData();
    body.append("file", file);

    setUploadStatus(`Uploading ${file.name}...`, false);

    try {
      let response;
      try {
        response = await fetch("/api/upload", { method: "POST", body });
      } catch {
        setUploadStatus("Network error: could not reach the orchestrator.", true);
        return;
      }

      let data;
      try {
        data = await response.json();
      } catch {
        data = { error: `Upload failed (HTTP ${response.status}).` };
      }

      if (!response.ok || data.error) {
        setUploadStatus(data.error ?? "Upload failed.", true);
      } else {
        setUploadStatus(`Saved as assets/${data.filename}`, false);
      }
    } finally {
      fileInputEl.value = "";
    }
  });
})();
