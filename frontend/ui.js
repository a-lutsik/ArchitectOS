/* Language/theme/density, errors, provider status helpers, result cards — extracted from app.js */
import { api } from "./api-client.js";
import { syncAskMode } from "./ask-ui.js";
import { escapeHtml, labelPrefix, setButton, setPlaceholder, setText, setTextContent, showSnackbar, trapFocus } from "./dom-utils.js";
import { syncGraphViewChrome } from "./graph.js";
import { LANGUAGE_META, RTL_LANGUAGES, translations } from "./i18n.js";
import { refreshWorkspace, runSearch } from "./projects.js";
import { state, syncProjectTerminology, t, titleByView } from "./state.js";

function syncLanguageMenu() {
  const current = LANGUAGE_META[state.language] || LANGUAGE_META.en;
  const flag = document.querySelector("#language-current-flag");
  const button = document.querySelector("#language-btn");
  if (flag) flag.textContent = current.flag;
  if (button) button.title = t("language.current").replace("{label}", current.label);
  document.querySelectorAll("[data-language-option]").forEach(option => {
    const active = option.dataset.languageOption === state.language;
    option.classList.toggle("active", active);
    option.setAttribute("aria-checked", active ? "true" : "false");
  });
}
function applyLanguage(language) {
  state.language = translations[language] ? language : "en";
  document.documentElement.lang = state.language;
  document.documentElement.dir = RTL_LANGUAGES.has(state.language) ? "rtl" : "ltr";
  document.body.classList.toggle("rtl", RTL_LANGUAGES.has(state.language));
  document.querySelectorAll("#settings-language-select").forEach(select => { select.value = state.language; });
  document.querySelectorAll(".nav-item").forEach(item => {
    if (item.hidden) return;
    const label = item.querySelector(".nav-item-label");
    if (label) label.textContent = t(`view.${item.dataset.view}`);
    else item.textContent = t(`view.${item.dataset.view}`);
  });
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const key = el.dataset.i18n;
    if (key) el.textContent = t(key);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
    const key = el.dataset.i18nPlaceholder;
    if (key) el.setAttribute("placeholder", t(key));
  });
  document.querySelectorAll("[data-ask-mode]").forEach(btn => {
    const mode = btn.dataset.askMode;
    btn.textContent = t(`ask.mode.${mode}`);
  });
  syncAskMode();
  const active = document.querySelector(".nav-item.active");
  setTextContent("#view-title", t(titleByView[(active && active.dataset.view) || "workspace"]));
  setText(".eyebrow", "app.eyebrow");
  setText("#workspace-view .panel:nth-of-type(1) h3", "workspace.context");
  setText("#workspace-view .panel:nth-of-type(2) h3", "workspace.projectFiles");
  setText("#workspace-view .panel:nth-of-type(3) h3", "workspace.selectedFile");
  setText("#workspace-view .panel:nth-of-type(4) h3", "workspace.favorites");
  setButton("#save-project", "action.connectFolder");
  setButton("#init-project", "action.connectAndIndex");
  setText("#project-folder-title", "folder.title");
  setText("#project-folder-subtitle", "folder.subtitle");
  setText("#project-folder-more-hint", "folder.moreHint");
  setButton("#context-form button", "action.build");
  setButton("#build-file-context", "action.buildContext");
  setButton("#load-git-diff", "action.gitDiff");
  setPlaceholder("#project-name", "placeholder.projectName");
  setPlaceholder("#project-root-path", "placeholder.projectRoot");
  setPlaceholder("#context-query", "placeholder.context");
  setPlaceholder("#selected-file-path", "placeholder.selectedFile");
  labelPrefix("#theme-select", "settings.theme");
  labelPrefix("#density-select", "settings.density");
  labelPrefix("#settings-language-select", "settings.language");
  labelPrefix("#short-term-ttl", "settings.shortTtl");
  labelPrefix("#archive-after", "settings.archive");
  labelPrefix("#delete-after", "settings.delete");
  labelPrefix("#promote-after-hits", "settings.promote");
  labelPrefix("#chat-memory-mode", "settings.chatMemoryMode");
  labelPrefix("#chat-candidate-ttl", "settings.chatCandidateTtl");
  labelPrefix("#chat-session-idle", "settings.chatSessionIdle");
  labelPrefix("#embedding-provider", "settings.embeddingProvider");
  labelPrefix("#embedding-model", "settings.embeddingModel");
  labelPrefix("#embedding-dimensions", "settings.embeddingDims");
  labelPrefix("#embedding-account-id", "settings.embeddingAccountId");
  labelPrefix("#embedding-base-url", "settings.embeddingBaseUrl");
  labelPrefix("#embedding-api-key", "settings.embeddingApiKey");
  labelPrefix("#vector-pool", "settings.vectorPool");
  labelPrefix("#vector-min-score", "settings.vectorMinScore");
  labelPrefix("#vector-query-timeout", "settings.vectorTimeout");
  labelPrefix("#search-relevance-floor", "settings.relevanceFloor");
  const embeddingsEnabledLabel = document.querySelector("#embeddings-enabled")?.closest("label");
  if (embeddingsEnabledLabel) {
    const title = embeddingsEnabledLabel.querySelector(".setting-toggle-title");
    if (title) title.textContent = t("settings.embeddingsEnabled");
    else if (embeddingsEnabledLabel.lastChild?.nodeType === Node.TEXT_NODE) {
      embeddingsEnabledLabel.lastChild.textContent = ` ${t("settings.embeddingsEnabled")}`;
    }
  }
  setButton("#embeddings-form button[type='submit']", "settings.saveEmbeddings");
  setButton("#embeddings-connection-form button[type='submit']", "settings.saveEmbeddings");
  setButton("#embeddings-test", "settings.embeddingTest");
  setButton("#embeddings-rebuild", "settings.rebuildEmbeddings");
  setText("#vector-runtime-title", "settings.vectorRuntime");
  setText("#vector-runtime-why", "settings.vectorRuntimeWhy");
  setText("#vector-runtime-hint", "settings.vectorRuntimeHint");
  setText("#vector-runtime-frozen", "settings.vectorRuntimeFrozen");
  setButton("#vector-runtime-check", "settings.vectorRuntimeCheck");
  setButton("#vector-runtime-fix", "settings.vectorRuntimeFix");
  setButton("#settings-form button[type='submit']", "action.saveSettings");
  setButton("#ingest-memory", "autoscan.ingest");
  setButton("#rescan-memory-all", "autoscan.rescanAll");
  setText("#code-analyze-project", "code.action.analyze");
  setText("#code-connect-folder", "action.connectFolder");
  const expandBtn = document.querySelector("#graph-expand");
  if (expandBtn) {
    expandBtn.setAttribute("aria-label", t("graph.expand"));
    expandBtn.title = t("graph.tip.expand");
  }
  setText("#graph-fit-expanded", "graph.fit");
  setText("#graph-close-expand", "graph.closeExpand");
  setText(".memory-lifecycle-heading h4", "lifecycle.title");
  setText(".memory-lifecycle-desc", "lifecycle.desc");
  setText("#run-memory-decay", "lifecycle.runDecay");
  const toolbar = document.querySelector(".file-tree-toolbar");
  if (toolbar) toolbar.setAttribute("aria-label", t("files.toolbar"));
  const dotfilesBtn = document.querySelector("#toggle-dotfiles");
  if (dotfilesBtn) {
    dotfilesBtn.dataset.i18nTitle = state.showDotfiles ? "files.tip.hideDotfiles" : "files.tip.showDotfiles";
  }
  document.querySelectorAll("[data-i18n-title]").forEach(el => {
    const text = t(el.dataset.i18nTitle);
    if (el.hasAttribute("data-tooltip")) {
      el.setAttribute("data-tooltip", text);
      el.setAttribute("aria-label", text);
      el.removeAttribute("title");
    } else {
      el.title = text;
    }
  });
  syncGraphViewChrome();
  setText("#workspace-ask-title", "workspace.ask.title");
  document.querySelectorAll(".workspace-surface-btn [data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    if (key) el.textContent = t(key);
  });
  setText("#editor-tab-empty-hint", "workspace.editor.empty");
  setPlaceholder("#file-editor", "workspace.editor.placeholder");
  syncProjectTerminology();
  syncLanguageMenu();
}
function resolveTheme(theme) {
  if (theme === "dark" || theme === "light") return theme;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(theme) {
  state.theme = theme || "system";
  const resolved = resolveTheme(state.theme);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved;
}
function applyDensity(density) {
  document.documentElement.dataset.density = density === "compact" ? "compact" : "comfortable";
}
if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (state.theme === "system") applyTheme("system"); });
}
async function saveUiSettings(partial = {}) {
  const ui = {
    theme: document.querySelector("#theme-select")?.value || state.theme || "system",
    density: document.querySelector("#density-select")?.value || "comfortable",
    memory_enabled: document.querySelector("#memory-enabled") ? document.querySelector("#memory-enabled").checked : true,
    ask_memory_advice: document.querySelector("#ask-memory-advice")
      ? document.querySelector("#ask-memory-advice").checked
      : state.askMemoryAdvice !== false,
    language: state.language,
    onboarding_complete: state.onboardingComplete,
    ...partial,
  };
  if (partial.onboarding_complete !== undefined) state.onboardingComplete = partial.onboarding_complete;
  if (ui.ask_memory_advice !== undefined) state.askMemoryAdvice = ui.ask_memory_advice !== false;
  await api("/api/settings", { method: "PATCH", body: JSON.stringify({ ui }) });
  if (typeof window.syncAskMemoryAdviceBadge === "function") window.syncAskMemoryAdviceBadge();
}
async function saveWorkspaceSettings(partial = {}) {
  const workspace = {
    current_project_id: state.projectId || "architectos",
    ...partial
  };
  await api("/api/settings", { method: "PATCH", body: JSON.stringify({ workspace }) });
}
function showError(error) {
  const message = error && error.message ? error.message : String(error || t("error.unexpected"));
  const output = document.querySelector("#context-output");
  if (output) output.textContent = message;
  showSnackbar(message, "error");
}
function providerIsReady(provider) {
  if (!provider || !provider.enabled) return false;
  const last = provider.last_check && typeof provider.last_check === "object" ? provider.last_check : null;
  const errorStatuses = ["missing_credentials", "missing_endpoint", "missing_cli", "missing_command", "missing_model", "unreachable", "error"];
  const readyStatuses = ["configured", "ok", "available", "ready", "fallback"];
  if (last) {
    const lastStatus = String(last.status || "").toLowerCase();
    if (last.ready === false) return false;
    if (errorStatuses.includes(lastStatus)) return false;
    if (last.ready === true) return true;
  }
  const status = String(provider.status || "").toLowerCase();
  if (errorStatuses.includes(status)) return false;
  return readyStatuses.includes(status);
}
function providerStatusClass(provider) {
  if (!provider || !provider.enabled) return "disabled";
  if (providerIsReady(provider)) return "ready";
  const last = provider.last_check || {};
  const status = String(last.status || provider.status || "").toLowerCase();
  if (["missing_credentials", "missing_endpoint", "missing_cli", "missing_command", "missing_model", "unreachable", "error"].includes(status)) return "error";
  return "planned";
}
function providerStatusLabel(provider) {
  const tone = providerStatusClass(provider);
  if (tone === "ready") return t("providers.status.ready");
  if (tone === "disabled") return t("providers.status.disabled");
  const status = String((provider.last_check && provider.last_check.status) || provider.status || "");
  if (status === "missing_credentials") return t("providers.status.missingCredentials");
  if (status === "missing_model") return t("providers.status.missingModel");
  if (tone === "error") return t("providers.status.error");
  return t("providers.status.planned");
}
function providerHint(provider) {
  if (provider.last_check && provider.last_check.message) return provider.last_check.message;
  if (provider.id === "openai") return t("providers.hint.openai");
  if (provider.id === "azure-openai") return t("providers.hint.azureOpenai");
  if (provider.id === "anthropic") return t("providers.hint.anthropic");
  if (provider.id === "gemini-cli") return t("providers.hint.geminiCli");
  if (provider.id === "codex-cli") return t("providers.hint.codexCli");
  if (provider.id === "openrouter") {
    if (provider.available_models && provider.available_models.length) return t("providers.hint.openrouterDiscovered").replace("{count}", String(provider.available_models.length));
    return t("providers.hint.openrouter");
  }
  if (provider.id === "ollama") {
    if (provider.available_models && provider.available_models.length) return t("providers.hint.ollamaDiscovered").replace("{count}", String(provider.available_models.length));
    return t("providers.hint.ollama");
  }
  if (provider.provider_type === "cli") return t("providers.hint.cli");
  return t("providers.hint.default");
}

function providerLoginLabel(provider) {
  if (provider.id === "gemini-cli") return t("providers.login.google");
  if (provider.id === "codex-cli") return t("providers.login.chatgpt");
  return "";
}

function renderResults(container, hits) {
  if (!container) return;
  container.innerHTML = "";
  if (!hits.length) { container.innerHTML = `<div class="result"><strong>${escapeHtml(t("results.noMatches"))}</strong><p>${escapeHtml(t("results.noMatchesHint"))}</p></div>`; return; }
  for (const hit of hits) {
    const node = hit.node || hit;
    const favorite = Boolean(node.metadata && node.metadata.favorite);
    const el = document.createElement("article");
    el.className = "result";
    const meta = node.metadata || {};
    const tier = meta.memory_tier ? `<span class="badge">${escapeHtml(meta.memory_tier)}</span>` : "";
    const lifecycle = meta.lifecycle_state ? `<span class="badge">${escapeHtml(meta.lifecycle_state)}</span>` : "";
    const access = meta.access_count ? `<span class="badge">used ${escapeHtml(String(meta.access_count))}</span>` : "";
    const longTermButton = meta.memory_tier === "long_term" ? "" : `<button data-long-term="${escapeHtml(node.id)}" type="button">${escapeHtml(t("results.longTerm"))}</button>`;
    el.innerHTML = `<div class="row"><strong>${escapeHtml(node.label)}</strong><div class="provider-actions"><button data-fav="${escapeHtml(node.id)}">${favorite ? escapeHtml(t("results.starred")) : escapeHtml(t("results.star"))}</button>${longTermButton}</div></div><p>${escapeHtml(node.text)}</p><span class="badge">${escapeHtml(node.type)}</span><span class="badge">${escapeHtml(node.scope)}</span>${tier}${lifecycle}${access}${meta.memory_score ? `<span class="badge">memory ${escapeHtml(String(meta.memory_score))}</span>` : ""}${hit.score ? `<span class="badge">score ${hit.score}</span>` : ""}`;
    container.appendChild(el);
  }
  container.querySelectorAll("[data-fav]").forEach(button => button.addEventListener("click", async () => { await api(`/api/memory/${button.dataset.fav}/favorite`, { method: "PATCH", body: "{}" }); await refreshWorkspace(); await runSearch(document.querySelector("#search-query").value || "memory"); }));
  container.querySelectorAll("[data-long-term]").forEach(button => button.addEventListener("click", async () => { await api(`/api/memory/${button.dataset.longTerm}/promote-long-term`, { method: "POST", body: JSON.stringify({ reason: "ui" }) }); await refreshWorkspace(); await runSearch(document.querySelector("#search-query").value || "memory"); }));
}

// Generic focus trapping for the modals whose open/close paths live in other
// modules (project settings, project wizard, MCP add-server, project folder,
// file find — plus the explicitly trapped graph modal and search palette,
// which manage their own traps). A MutationObserver watches each modal's
// open-state attribute: opening installs a trap and moves focus inside,
// closing releases it and restores focus to the element that opened it.
const MODAL_FOCUS_SPECS = [
  { id: "project-settings-modal", attr: "hidden", focus: "#settings-project-name" },
  { id: "new-project-wizard", attr: "hidden", focus: "#wizard-folder-path" },
  { id: "add-mcp-modal", attr: "hidden", focus: "#add-mcp-label" },
  { id: "projectFolderModal", attr: "aria-hidden", focus: "#project-root-path" },
  { id: "fileFindModal", attr: "aria-hidden", focus: "#file-find-query" },
];
const modalFocusReleases = new Map();
let lastFocusBeforeModal = null;

function watchedModalIsOpen(el, spec) {
  return spec.attr === "hidden" ? !el.hasAttribute("hidden") : el.getAttribute("aria-hidden") !== "true";
}

function syncModalFocusTraps() {
  for (const spec of MODAL_FOCUS_SPECS) {
    const el = document.getElementById(spec.id);
    if (!el) continue;
    const open = watchedModalIsOpen(el, spec);
    const release = modalFocusReleases.get(spec.id);
    if (open && !release) {
      modalFocusReleases.set(spec.id, trapFocus(el, lastFocusBeforeModal || undefined));
      if (!el.contains(document.activeElement)) {
        const target = el.querySelector(spec.focus) || el.querySelector("button, input, select, textarea");
        if (target) target.focus();
      }
    } else if (!open && release) {
      modalFocusReleases.delete(spec.id);
      release();
    }
  }
}

function initModalFocusWatch() {
  // Track the last element focused outside any open watched modal — that is
  // the trigger focus returns to when the modal closes.
  document.addEventListener("focusin", event => {
    for (const id of modalFocusReleases.keys()) {
      const el = document.getElementById(id);
      if (el && el.contains(event.target)) return;
    }
    lastFocusBeforeModal = event.target;
  });
  const observer = new MutationObserver(syncModalFocusTraps);
  observer.observe(document.body, { subtree: true, attributes: true, attributeFilter: ["hidden", "aria-hidden"] });
  syncModalFocusTraps();
}

if (typeof MutationObserver !== "undefined" && typeof document !== "undefined" && document.body) {
  initModalFocusWatch();
}

export {
  applyDensity, applyLanguage, applyTheme,
  providerHint, providerIsReady, providerLoginLabel, providerStatusClass, providerStatusLabel,
  renderResults, saveUiSettings, saveWorkspaceSettings, showError, syncLanguageMenu,
};
