// The Settings page: which model plans, which one answers, and where the
// agents work.

import { $, api, esc, setConnection, setRows, state, toast } from "./core.js";

// ── settings ───────────────────────────────────────────────────

/** Fill the model list, keeping whatever is in force selectable.
 *
 * A provider that cannot be asked returns nothing, and the note says so
 * rather than leaving an empty list that reads as "there are no models".
 */
/** Fill the model suggestions. A datalist rather than a select: OpenRouter
 * offers hundreds of models, and typing part of a name filters them, while a
 * provider that cannot list its models still accepts any name typed. */
export function renderModelList(models, error) {
  setRows($("settings-model-list"), (models || []).map(function (name) {
    return '<option value="' + esc(name) + '"></option>';
  }).join(""));

  $("settings-model-note").textContent = error
    || (models && models.length > 1 ? models.length + " available · type to search" : "");
}

export function providerInfo(name) {
  const list = (state.settings && state.settings.providers) || [];
  return list.find(function (item) { return item.name === name; }) || null;
}

/** Show or hide the key field, and say what the chosen provider needs. */
export function renderProviderNeeds(name) {
  const info = providerInfo(name);
  const field = $("settings-key-field");
  const note = $("settings-provider-note");

  if (!info || !info.key_env) {
    field.classList.add("hidden");
    note.textContent = info && !info.available ? info.error : "Runs on this computer; no key needed.";
    return;
  }

  field.classList.remove("hidden");
  $("settings-key-label").textContent = "API key (" + info.key_env + ")";
  $("settings-key").placeholder = info.key_set ? "already set — paste to replace" : "paste the key here";
  note.textContent = info.key_set ? "Key found." : "Needs a key before it can plan.";
}

export function renderProviders(settings) {
  const select = $("settings-provider-select");

  setRows(select, (settings.providers || []).map(function (info) {
    const suffix = info.key_env && !info.key_set ? " — key needed" : "";
    return '<option value="' + esc(info.name) + '"' + (info.available ? "" : " disabled") + ">"
      + esc(info.label + suffix) + "</option>";
  }).join(""));

  select.value = settings.provider;
  renderProviderNeeds(settings.provider);
  renderProviderCards(settings.providers || [], settings.provider);
}

/** The provider choice as cards: what each is and what it still needs, at a
 * glance, which a drop-down cannot say. The hidden select stays the source
 * of truth, so the rest of the form reads it exactly as before. */
export function renderProviderCards(providers, chosen) {
  setRows($("provider-cards"), providers.map(function (info) {
    const [title, detail] = String(info.label).split(" — ");
    let needs = "No key needed";
    let tone = "ok";

    if (!info.available) {
      needs = "Not installed";
      tone = "warn";
    } else if (info.key_env) {
      needs = info.key_set ? "Key set" : "Needs " + info.key_env;
      tone = info.key_set ? "ok" : "warn";
    }

    return '<button type="button" class="provider-card" role="radio" data-provider="'
      + esc(info.name) + '" aria-checked="' + String(info.name === chosen) + '"'
      + (info.available ? "" : " disabled") + ">"
      + '<span class="name">' + esc(title) + "</span>"
      + (detail ? '<span class="small muted">' + esc(detail) + "</span>" : "")
      + '<span class="needs ' + tone + '">' + esc(needs) + "</span>"
      + "</button>";
  }).join(""));
}

$("provider-cards").addEventListener("click", function (event) {
  const card = event.target.closest("[data-provider]");
  if (!card || card.disabled) return;

  const select = $("settings-provider-select");
  select.value = card.dataset.provider;
  $("provider-cards").querySelectorAll("[data-provider]").forEach(function (candidate) {
    candidate.setAttribute("aria-checked", String(candidate === card));
  });
  // The change handler below asks the provider for its models; `bubbles` so
  // the form also notices it has unsaved changes.
  select.dispatchEvent(new Event("change", { bubbles: true }));
});

// ── the answer model ───────────────────────────────────────────────────────

/** Show the key field only when the chosen provider still needs a key. */
export function renderReviewNeeds(name) {
  const info = name ? providerInfo(name) : null;
  const needsKey = Boolean(info && info.key_env && !info.key_set);

  $("review-key-field").classList.toggle("hidden", !needsKey);
  $("review-model-field").classList.toggle("hidden", !name);
  if (needsKey) $("review-key-label").textContent = "API key (" + info.key_env + ")";
}

export function renderReview(settings) {
  const review = settings.review || {};

  setRows($("review-provider"),
    '<option value="">Same as planning</option>'
    + (settings.providers || []).filter(function (info) { return info.available; })
      .map(function (info) {
        return '<option value="' + esc(info.name) + '">'
          + esc(String(info.label).split(" — ")[0]) + "</option>";
      }).join(""));

  $("review-provider").value = review.provider || "";
  $("review-model").value = review.model || "";
  $("review-key").value = "";
  renderReviewNeeds(review.provider || "");

  if (review.provider) loadReviewModels(review.provider);
}

export async function loadReviewModels(name) {
  try {
    const data = await api("/settings/models?provider=" + encodeURIComponent(name));
    setRows($("review-model-list"), (data.models || []).map(function (model) {
      return '<option value="' + esc(model) + '"></option>';
    }).join(""));
    $("review-model-note").textContent = data.models_error
      || (data.models.length > 1 ? data.models.length + " available" : "");
  } catch (error) {
    $("review-model-note").textContent = error.message;
  }
}

$("review-provider").addEventListener("change", function () {
  const name = $("review-provider").value;
  const current = state.settings && state.settings.review
    && state.settings.review.provider === name;

  $("review-model").value = current ? state.settings.review.model : "";
  renderReviewNeeds(name);
  if (name) loadReviewModels(name);
});

export function renderModels(settings) {
  renderProviders(settings);
  $("settings-model").value = settings.model;
  renderModelList(settings.models, settings.models_error);
}

$("settings-provider-select").addEventListener("change", async function () {
  const name = $("settings-provider-select").value;
  const current = state.settings && state.settings.provider === name;

  renderProviderNeeds(name);
  $("settings-model").value = current ? state.settings.model : "";
  $("settings-model-note").textContent = "Asking " + name + " which models it has…";

  try {
    const data = await api("/settings/models?provider=" + encodeURIComponent(name));
    renderModelList(data.models, data.models_error);
  } catch (error) {
    renderModelList([], error.message);
  }
});

/** Show which model and workspace the next run will use, on the Run page. */
export function renderPlannerSummary(settings) {
  const chip = $("planner-chip");
  chip.className = "chip idle";
  chip.textContent = settings.provider + " · " + settings.model;

  // The workspace goes in the tooltip: the chat keeps its screen for the
  // conversation, and this is where someone wondering "on what code?" looks.
  const workspace = settings.workspace;
  chip.title = "Which model writes the plan" + (workspace.path
    ? "\nWorkspace: " + workspace.path
      + (workspace.entry_point ? " · " + workspace.entry_point : "")
    : "");
}

export async function loadSettings(options) {
  const quiet = options && options.quiet;

  try {
    const settings = await api("/settings");
    state.settings = settings;
    // Reaching /settings proves the service is up and the token is accepted,
    // which is what the chip is reporting. It used to be set only by the
    // Targets tab, so every other tab sat at "connecting" forever.
    setConnection("ok", "connected");

    renderPlannerSummary(settings);

    // Only repaint the form when the reader is not in the middle of it: this
    // runs on the refresh timer, and overwriting a half-typed path would be
    // the console fighting whoever is using it.
    if (state.tab === "settings" && !formIsFocused() && !state.settingsDirty) {
      renderModels(settings);
      renderReview(settings);
      $("settings-temperature").value = settings.temperature;
      $("settings-timeout").value = settings.request_timeout;
      $("settings-workspace-path").value = settings.workspace.path;
      $("settings-workspace-entry").value = settings.workspace.entry_point;
      $("settings-provider").textContent = settings.provider;
      $("settings-prompt").textContent = settings.prompt_path;
      $("settings-path").textContent = settings.config_path;

      const warning = $("settings-readonly");

      if (settings.writable) {
        warning.classList.add("hidden");
      } else {
        warning.textContent = "Saving is turned off on this deployment "
          + "(KIO1_CONFIG_WRITABLE), so nothing here can be changed.";
        warning.classList.remove("hidden");
      }

      $("settings-save").disabled = !settings.writable;
    }
  } catch (error) {
    setConnection("bad", error.status === 401 ? "token" : "error");

    if (!quiet) {
      $("settings-result").textContent = error.message;
    }
  }
}

/** Report whether a settings control currently has the caret. */
export function formIsFocused() {
  const active = document.activeElement;

  return Boolean(active && active.closest && active.closest("#view-settings"));
}

export async function saveSettings() {
  const button = $("settings-save");
  const result = $("settings-result");

  const previousModel = state.settings ? state.settings.model : "";
  const key = $("settings-key").value.trim();

  button.disabled = true;
  result.textContent = "Saving…";

  try {
    const settings = await api("/settings", {
      method: "PUT",
      body: JSON.stringify({
        provider: $("settings-provider-select").value,
        api_key: key || null,
        model: $("settings-model").value.trim(),
        temperature: Number($("settings-temperature").value),
        request_timeout: Number($("settings-timeout").value),
        workspace_path: $("settings-workspace-path").value,
        workspace_entry_point: $("settings-workspace-entry").value,
        review_provider: $("review-provider").value,
        review_model: $("review-model").value.trim(),
        review_api_key: $("review-key").value.trim() || null,
      }),
    });

    state.settings = settings;
    state.settingsDirty = false;
    $("settings-key").value = "";
    renderReview(settings);
    renderModels(settings);
    renderPlannerSummary(settings);
    result.textContent = "";

    // A local model is being read into memory right now if it changed, and a
    // run started before that finishes waits for it.
    toast("Saved — in force from the next run."
      + (previousModel !== settings.model ? "\nLoading " + settings.model + "…" : ""), "ok");
  } catch (error) {
    result.className = "small reason";
    result.textContent = error.message;
    toast(error.message, "bad");
  } finally {
    button.disabled = !(state.settings && state.settings.writable);
  }
}

$("settings-save").addEventListener("click", saveSettings);
$("settings-reload").addEventListener("click", function () {
  $("settings-result").textContent = "";
  document.activeElement.blur();
  state.settingsDirty = false;
  loadSettings({});
});

// Once something is changed, the refresh timer stops repainting the form
// until it is saved or discarded. Checking focus alone was not enough: pick a
// provider, click anywhere else, and the next refresh put the old one back.
["input", "change"].forEach(function (type) {
  $("view-settings").addEventListener(type, function () { state.settingsDirty = true; });
});
