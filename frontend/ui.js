/* Language/theme/density, errors, provider status helpers, result cards — extracted from app.js */
function syncLanguageMenu() {
  const current = LANGUAGE_META[state.language] || LANGUAGE_META.en;
  const flag = document.querySelector("#language-current-flag");
  const button = document.querySelector("#language-btn");
  if (flag) flag.textContent = current.flag;
  if (button) button.title = `Language: ${current.label}`;
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
  setButton("#refresh-files", "action.refresh");
  setButton("#build-file-context", "action.buildContext");
  setButton("#load-git-diff", "action.gitDiff");
  setPlaceholder("#project-name", "placeholder.projectName");
  setPlaceholder("#project-root-path", "placeholder.projectRoot");
  setPlaceholder("#context-query", "placeholder.context");
  setPlaceholder("#selected-file-path", "placeholder.selectedFile");
  setText("#settings-view .panel:nth-of-type(1) h3", "settings.bundle");
  setText("#settings-view .panel:nth-of-type(2) h3", "settings.ui");
  setText("#settings-view .panel:nth-of-type(3) h3", "settings.embeddings");
  setText("#settings-view .panel:nth-of-type(4) h3", "settings.security");
  labelPrefix("#theme-select", "settings.theme");
  labelPrefix("#density-select", "settings.density");
  labelPrefix("#settings-language-select", "settings.language");
  labelPrefix("#short-term-ttl", "settings.shortTtl");
  labelPrefix("#archive-after", "settings.archive");
  labelPrefix("#delete-after", "settings.delete");
  labelPrefix("#promote-after-hits", "settings.promote");
  labelPrefix("#embedding-provider", "settings.embeddingProvider");
  labelPrefix("#embedding-model", "settings.embeddingModel");
  labelPrefix("#embedding-dimensions", "settings.embeddingDims");
  labelPrefix("#vector-pool", "settings.vectorPool");
  labelPrefix("#vector-min-score", "settings.vectorMinScore");
  labelPrefix("#vector-query-timeout", "settings.vectorTimeout");
  const embeddingsEnabledLabel = document.querySelector("#embeddings-enabled")?.closest("label");
  if (embeddingsEnabledLabel) embeddingsEnabledLabel.lastChild.textContent = ` ${t("settings.embeddingsEnabled")}`;
  const reindexStartupLabel = document.querySelector("#reindex-on-startup")?.closest("label");
  if (reindexStartupLabel) reindexStartupLabel.lastChild.textContent = ` ${t("settings.reindexStartup")}`;
  setButton("#embeddings-form button[type='submit']", "settings.saveEmbeddings");
  setButton("#embeddings-rebuild", "settings.rebuildEmbeddings");
  const memoryLabel = document.querySelector("#memory-enabled")?.closest("label");
  if (memoryLabel) memoryLabel.lastChild.textContent = ` ${t("settings.memory")}`;
  const lifecycleLabel = document.querySelector("#lifecycle-enabled")?.closest("label");
  if (lifecycleLabel) lifecycleLabel.lastChild.textContent = ` ${t("settings.lifecycle")}`;
  const refreshLabel = document.querySelector("#refresh-on-access")?.closest("label");
  if (refreshLabel) refreshLabel.lastChild.textContent = ` ${t("settings.refresh")}`;
  const autoRescanLabel = document.querySelector("#auto-rescan-on-startup")?.closest("label");
  if (autoRescanLabel) autoRescanLabel.lastChild.textContent = ` ${t("settings.autoRescan")}`;
  setButton("#ingest-memory", "autoscan.ingest");
  setButton("#rescan-memory-all", "autoscan.rescanAll");
  setButton("#settings-form button", "action.saveSettings");
  setText("#terminal-panel-title", "terminal.title");
  setText("#terminal-panel-hint", "terminal.hint");
  setButton("#terminal-open", "terminal.external");
  setText("#terminal-view .panel:nth-of-type(2) h3", "terminal.history");
  setText("#code-run-insight", "code.action.analyzeFile");
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
  document.querySelectorAll("[data-i18n-title]").forEach(el => {
    el.title = t(el.dataset.i18nTitle);
  });
  setText("#workspace-ask-title", "workspace.ask.title");
  document.querySelectorAll(".workspace-surface-btn [data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    if (key) el.textContent = t(key);
  });
  setText("#editor-tab-empty-hint", "workspace.editor.empty");
  setPlaceholder("#file-editor", "workspace.editor.placeholder");
  syncCodeFileHint();
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
    language: state.language,
    onboarding_complete: state.onboardingComplete,
    ...partial,
  };
  if (partial.onboarding_complete !== undefined) state.onboardingComplete = partial.onboarding_complete;
  await api("/api/settings", { method: "PATCH", body: JSON.stringify({ ui }) });
}
async function saveWorkspaceSettings(partial = {}) {
  const workspace = {
    current_project_id: state.projectId || "architectos",
    ...partial
  };
  await api("/api/settings", { method: "PATCH", body: JSON.stringify({ workspace }) });
}
function showError(error) {
  const message = error && error.message ? error.message : String(error || "Unexpected error");
  const output = document.querySelector("#context-output");
  if (output) output.textContent = message;
  showSnackbar(message, "error");
}
function providerStatusClass(status, enabled) {
  if (!enabled) return "disabled";
  if (["configured", "ok"].includes(status)) return "ready";
  if (["missing_credentials", "missing_endpoint", "missing_cli", "missing_command", "missing_model", "unreachable", "error"].includes(status)) return "error";
  return "planned";
}
function providerHint(provider) {
  if (provider.last_check && provider.last_check.message) return provider.last_check.message;
  if (provider.id === "openai") return "Set OPENAI_API_KEY, then test this provider.";
  if (provider.id === "azure-openai") return "Set AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT, then set Model to your Azure deployment name. You can also set AZURE_OPENAI_DEPLOYMENT.";
  if (provider.id === "anthropic") return "Set ANTHROPIC_API_KEY, choose a Claude model, then test this provider.";
  if (provider.id === "gemini-cli") return "Antigravity (agy): Sign in with Google, then Test. Gemini CLI is deprecated.";
  if (provider.id === "codex-cli") return "Sign in with ChatGPT in the terminal (like Codex email login), then Test.";
  if (provider.id === "openrouter") {
    if (provider.available_models && provider.available_models.length) return `${provider.available_models.length} OpenRouter model(s) discovered.`;
    return "Set OPENROUTER_API_KEY, refresh models, then choose a model slug.";
  }
  if (provider.id === "ollama") {
    if (provider.available_models && provider.available_models.length) return `${provider.available_models.length} local model(s) discovered.`;
    return "Start Ollama, refresh models, then choose one.";
  }
  if (provider.provider_type === "cli") return "Install the CLI, confirm PATH, then test this provider.";
  return "Configure and test this provider before enabling auto-route.";
}

function providerLoginLabel(provider) {
  if (provider.id === "gemini-cli") return "Sign in with Google";
  if (provider.id === "codex-cli") return "Sign in with ChatGPT";
  return "";
}

function renderResults(container, hits) {
  if (!container) return;
  container.innerHTML = "";
  if (!hits.length) { container.innerHTML = '<div class="result"><strong>No matches</strong><p>Scan project or add memory.</p></div>'; return; }
  for (const hit of hits) {
    const node = hit.node || hit;
    const favorite = Boolean(node.metadata && node.metadata.favorite);
    const el = document.createElement("article");
    el.className = "result";
    const meta = node.metadata || {};
    const tier = meta.memory_tier ? `<span class="badge">${escapeHtml(meta.memory_tier)}</span>` : "";
    const lifecycle = meta.lifecycle_state ? `<span class="badge">${escapeHtml(meta.lifecycle_state)}</span>` : "";
    const access = meta.access_count ? `<span class="badge">used ${escapeHtml(String(meta.access_count))}</span>` : "";
    const longTermButton = meta.memory_tier === "long_term" ? "" : `<button data-long-term="${escapeHtml(node.id)}" type="button">Long-term</button>`;
    el.innerHTML = `<div class="row"><strong>${escapeHtml(node.label)}</strong><div class="provider-actions"><button data-fav="${escapeHtml(node.id)}">${favorite ? "Starred" : "Star"}</button>${longTermButton}</div></div><p>${escapeHtml(node.text)}</p><span class="badge">${escapeHtml(node.type)}</span><span class="badge">${escapeHtml(node.scope)}</span>${tier}${lifecycle}${access}${meta.memory_score ? `<span class="badge">memory ${escapeHtml(String(meta.memory_score))}</span>` : ""}${hit.score ? `<span class="badge">score ${hit.score}</span>` : ""}`;
    container.appendChild(el);
  }
  container.querySelectorAll("[data-fav]").forEach(button => button.addEventListener("click", async () => { await api(`/api/memory/${button.dataset.fav}/favorite`, { method: "PATCH", body: "{}" }); await refreshWorkspace(); await runSearch(document.querySelector("#search-query").value || "memory"); }));
  container.querySelectorAll("[data-long-term]").forEach(button => button.addEventListener("click", async () => { await api(`/api/memory/${button.dataset.longTerm}/promote-long-term`, { method: "POST", body: JSON.stringify({ reason: "ui" }) }); await refreshWorkspace(); await runSearch(document.querySelector("#search-query").value || "memory"); }));
}
