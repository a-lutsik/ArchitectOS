function bindEvents() {
  bindChatComposer();
  initVoiceMemory();
  onAll(".nav-item", "click", item => switchView(item.currentTarget.dataset.view));
  on("#project-select", "change", event => { state.projectId = event.target.value; saveWorkspaceSettings({ current_project_id: state.projectId }).catch(showError); state.selectedFile = ""; state.memoryFiles = []; resetGraphFilters(); renderMemoryFiles(); syncProjectSwitcherLabel(); syncProjectTerminology(); syncProjectFields(); refreshWorkspace(); loadProjectFiles(); scheduleGraphLoad(); });
  on("#settings-language-select", "change", event => { applyLanguage(event.target.value); saveUiSettings({ language: state.language }).catch(showError); });
  on("#theme-select", "change", event => applyTheme(event.target.value));
  on("#density-select", "change", event => applyDensity(event.target.value));
  on("#open-project-folder-modal", "click", openProjectFolderModal);
  on("#close-project-folder-modal", "click", closeProjectFolderModal);
  onAll("[data-close-project-folder]", "click", closeProjectFolderModal);
  on("#browse-project-folder", "click", () => browseProjectFolder().catch(error => { setProjectFolderStatus(error.message, "error"); }));
  on("#save-project", "click", () => saveProjectFromFolder().then(project => { setProjectFolderStatus(project.message || "Project folder applied.", "ok"); closeProjectFolderModal(); }).catch(showError));
  on("#init-project", "click", () => initProjectFromFolder().then(() => { setProjectFolderStatus("Project indexed.", "ok"); showSnackbar("Project indexed successfully.", "success"); closeProjectFolderModal(); }).catch(showError));
  on("#context-form", "submit", event => { event.preventDefault(); buildContext(document.querySelector("#context-query")?.value || "memory context").catch(showError); });
  on("#memory-form", "submit", async event => { event.preventDefault(); await api("/api/memory", { method: "POST", body: JSON.stringify({ project_id: state.projectId, label: document.querySelector("#memory-label")?.value || "", type: document.querySelector("#memory-type")?.value || "Note", scope: document.querySelector("#memory-scope")?.value || "project", text: document.querySelector("#memory-text")?.value || "" }) }); event.target.reset(); await refreshWorkspace(); scheduleGraphLoad(); });
  on("#memory-file-picker", "click", () => document.querySelector("#memory-file-input")?.click());
  on("#memory-file-input", "change", event => { handleMemoryFileSelect(event.target.files).catch(showError); event.target.value = ""; });
  on("#memory-file-import", "click", () => importMemoryFiles().catch(showError));
  on("#ingest-memory", "click", () => ingestMemorySources().catch(showError));
  on("#rescan-memory-all", "click", () => rescanAllMemorySources().catch(showError));
  on("#ingest-select-all", "click", () => setIngestSourcesSelected(true));
  on("#ingest-unselect-all", "click", () => setIngestSourcesSelected(false));
  document.querySelectorAll(".ingest-source").forEach(input => {
    input.addEventListener("change", syncBoardsOptionsVisibility);
  });
  document.querySelectorAll(".boards-type-option").forEach(input => {
    input.addEventListener("change", syncBoardsTypeSummary);
  });
  on("#boards-types-select-all", "click", () => {
    document.querySelectorAll(".boards-type-option").forEach(input => { input.checked = true; });
    syncBoardsTypeSummary();
  });
  on("#boards-types-unselect-all", "click", () => {
    document.querySelectorAll(".boards-type-option").forEach(input => { input.checked = false; });
    syncBoardsTypeSummary();
  });
  on("#refresh-candidates", "click", () => loadMemoryCandidates().catch(showError));
  on("#memory-tab-graph", "click", () => switchMemoryTab("graph"));
  on("#memory-tab-list", "click", () => switchMemoryTab("list"));
  on("#memory-list-tier", "change", () => loadMemoryList().catch(showError));
  on("#memory-list-state", "change", () => loadMemoryList().catch(showError));
  let memoryListSearchTimer = null;
  on("#memory-list-search", "input", () => {
    window.clearTimeout(memoryListSearchTimer);
    memoryListSearchTimer = window.setTimeout(() => loadMemoryList().catch(showError), 250);
  });
  on("#candidate-status-filter", "change", () => {
    syncCandidateBatchActions();
    loadMemoryCandidates().catch(showError);
  });
  on("#candidates-accept-pending", "click", () => batchUpdateCandidates({ action: "promote", status: "candidate" }).catch(showError));
  on("#candidates-accept-non-duplicates", "click", () => batchUpdateCandidates({ action: "promote", status: "candidate", exclude_duplicates: true }).catch(showError));
  on("#candidates-reject-duplicates", "click", () => batchUpdateCandidates({ action: "reject", status: "duplicate", reason: "Batch rejected duplicate" }).catch(showError));
  on("#candidates-reject-pending", "click", () => batchUpdateCandidates({ action: "reject", status: "candidate", reason: "Batch rejected pending" }).catch(showError));
  on("#candidates-accept-filtered", "click", () => batchUpdateCandidates({ action: "promote", ...candidateBatchFilterFromUi() }).catch(showError));
  on("#candidates-reject-filtered", "click", () => batchUpdateCandidates({ action: "reject", reason: "Batch rejected filtered", ...candidateBatchFilterFromUi() }).catch(showError));
  document.addEventListener("click", event => {
    const more = document.querySelector(".candidates-more");
    if (!more || !more.open) return;
    if (more.contains(event.target)) return;
    more.open = false;
  });
  syncCandidateBatchActions();
  on("#run-memory-decay", "click", async () => { const summary = document.querySelector("#memory-decay-summary"); if (summary) summary.textContent = "checking..."; const payload = await api("/api/memory/decay/run", { method: "POST", body: JSON.stringify({ project_id: state.projectId }) }); if (summary) summary.textContent = `${payload.changed} memory item(s) updated`; await runSearch(document.querySelector("#search-query")?.value || "memory"); await loadAnalytics(); await loadMemoryLifecycle(); });
  on("#memory-lifecycle-state-filter", "change", () => loadMemoryLifecycleItems().catch(showError));
  on("#memory-lifecycle-tier-filter", "change", () => loadMemoryLifecycleItems().catch(showError));
  // Tasks are created via the inline TODO composer in loadTasks().
  on("#chat-form", "submit", event => sendChatMessage(event).catch(showError));
  on("#chat-stop", "click", () => cancelActiveRun().catch(showError));
  on("#chat-new", "click", () => startNewAskThread());
  on("#chat-end-session", "click", () => {
    const chatId = state.chatId;
    if (!chatId) return;
    setElementDisabled("#chat-end-session", true);
    finalizeChatSession(chatId)
      .then(result => {
        const label = result?.candidate?.label || (result?.skipped ? "Session closed (no new candidate)" : "Session summarized");
        showSnackbar(label, result?.candidate ? "success" : "info");
        startNewAskThread({ notify: false });
      })
      .catch(error => {
        syncAskHeaderActions();
        showError(error);
      });
  });
  on("#refresh-files", "click", () => loadProjectFiles().catch(showError));
  on("#toggle-dotfiles", "click", () => {
    state.showDotfiles = !state.showDotfiles;
    const btn = document.querySelector("#toggle-dotfiles");
    if (btn) {
      btn.classList.toggle("active", state.showDotfiles);
      btn.setAttribute("aria-pressed", String(state.showDotfiles));
      btn.title = state.showDotfiles ? "Hide hidden (dot) files" : "Show hidden (dot) files";
    }
    loadProjectFiles().catch(showError);
  });
  on("#build-file-context", "click", () => buildSelectedFileContext().catch(showError));
  on("#load-git-diff", "click", () => loadGitDiff().catch(showError));
  on("#refresh-graph", "click", () => loadGraph().catch(showError));
  on("#export-bundle", "click", async () => { const bundle = await api(`/api/bundle/export?project_id=${projectParam()}`); const box = document.querySelector("#bundle-box"); if (box) box.value = JSON.stringify(bundle, null, 2); });
  on("#import-bundle", "click", async () => { const box = document.querySelector("#bundle-box"); if (!box) throw new Error("Bundle input is not available."); const payload = JSON.parse(box.value); await api("/api/bundle/import", { method: "POST", body: JSON.stringify(payload) }); await loadProjects(); await refreshWorkspace(); });
  on("#settings-form", "submit", async event => { event.preventDefault(); await api("/api/settings", { method: "PATCH", body: JSON.stringify({ ui: { theme: document.querySelector("#theme-select")?.value || state.theme, density: document.querySelector("#density-select")?.value || "comfortable", memory_enabled: document.querySelector("#memory-enabled")?.checked ?? true, language: document.querySelector("#settings-language-select")?.value || state.language, onboarding_complete: state.onboardingComplete }, memory_lifecycle: { enabled: document.querySelector("#lifecycle-enabled")?.checked ?? true, refresh_on_access: document.querySelector("#refresh-on-access")?.checked ?? true, auto_rescan_on_startup: document.querySelector("#auto-rescan-on-startup")?.checked ?? true, chat_memory_mode: document.querySelector("#chat-memory-mode")?.value || "strict", chat_candidate_ttl_days: Number(document.querySelector("#chat-candidate-ttl")?.value || 7), chat_session_idle_minutes: Number(document.querySelector("#chat-session-idle")?.value || 30), chat_store_facts_only: document.querySelector("#chat-store-facts-only")?.checked ?? true, short_term_ttl_days: Number(document.querySelector("#short-term-ttl")?.value || 14), archive_after_days: Number(document.querySelector("#archive-after")?.value || 30), delete_after_days: Number(document.querySelector("#delete-after")?.value || 0), promote_after_hits: Number(document.querySelector("#promote-after-hits")?.value || 5) } }) }); applyTheme(document.querySelector("#theme-select")?.value || state.theme); applyDensity(document.querySelector("#density-select")?.value || "comfortable"); applyLanguage(document.querySelector("#settings-language-select")?.value || state.language); });
  on("#embeddings-form", "submit", async event => { event.preventDefault(); await saveEmbeddingsSettings().catch(showError); });
  on("#embeddings-rebuild", "click", () => rebuildEmbeddingsIndex().catch(showError));
  on("#embedding-provider", "change", () => {
    refreshEmbeddingModelOptions();
    const modelSelect = document.querySelector("#embedding-model");
    const dims = document.querySelector("#embedding-dimensions");
    const catalog = state.embeddingCatalog || [];
    const providerId = document.querySelector("#embedding-provider")?.value;
    const provider = catalog.find(item => item.id === providerId);
    const model = (provider?.models || []).find(item => item.id === modelSelect?.value) || (provider?.models || [])[0];
    if (dims && model?.default_dims) dims.value = model.default_dims;
  });
  on("#embedding-model", "change", () => {
    const providerId = document.querySelector("#embedding-provider")?.value;
    const modelId = document.querySelector("#embedding-model")?.value;
    const catalog = state.embeddingCatalog || [];
    const provider = catalog.find(item => item.id === providerId) || catalog.find(item => (item.models || []).some(m => m.id === modelId));
    const model = (provider?.models || []).find(item => item.id === modelId);
    const dims = document.querySelector("#embedding-dimensions");
    if (dims && model?.default_dims) dims.value = model.default_dims;
  });
  on("#security-preview-run", "click", () => runSecurityPreview().catch(showError));
  on("#router-form", "submit", event => { event.preventDefault(); saveRouterSettings().catch(showError); });
  on("#connect-env-providers", "click", () => connectEnvProviders().catch(showError));
  on("#test-all-providers", "click", () => testAllProviders().catch(showError));
  document.querySelectorAll("[data-router-preset]").forEach(btn => {
    btn.addEventListener("click", () => {
      const preset = ROUTER_PRESETS[btn.dataset.routerPreset];
      if (!preset) return;
      const strategyMap = { cheap: "cost", fast: "speed", quality: "quality", balanced: "balanced" };
      setRouterWeights(preset, strategyMap[btn.dataset.routerPreset] || "balanced");
    });
  });
  ["#router-w-quality", "#router-w-cost", "#router-w-speed", "#router-w-availability"].forEach(sel => {
    const el = document.querySelector(sel);
    if (el) el.addEventListener("input", syncRouterWeightLabels);
  });
  syncRouterWeightLabels();
  // legacy task-form removed; creation is inline on the board
  const legacyTaskForm = document.querySelector("#task-form");
  if (legacyTaskForm) legacyTaskForm.remove();
  on("#router-preview-form", "submit", event => { event.preventDefault(); previewRouting(document.querySelector("#router-preview-query")?.value || "").catch(showError); });
  on("#code-run-insight", "click", () => runCodeInsight().catch(showError));
  on("#code-connect-folder", "click", openProjectFolderModal);
  on("#code-analyze-project", "click", () => analyzeCodeProject().catch(showError));
  document.querySelectorAll("[data-code-tab]").forEach(button => {
    button.addEventListener("click", () => {
      setCodeInsightTab(button.dataset.codeTab || "symbols");
    });
  });
  on("#code-use-selected-file", "click", () => {
    if (!fillCodeFileFromSelection(true)) showError(new Error(t("code.noFileSelected")));
  });
  on("#terminal-form", "submit", event => runTerminalCommand(event).catch(showError));
  on("#terminal-open", "click", () => openExternalTerminal().catch(showError));
  on("#terminal-clear", "click", () => { state.terminalHistory = []; renderTerminalHistory(); });
  document.addEventListener("click", event => {
    const codeInstall = event.target.closest("[data-code-install]");
    if (codeInstall) {
      event.preventDefault();
      installCodeLanguageServer(codeInstall.dataset.codeInstall, codeInstall).catch(showError);
      return;
    }
    const runButton = event.target.closest("[data-terminal-install-run]");
    if (runButton) {
      event.preventDefault();
      runInstallCommandInTerminal(runButton.dataset.terminalInstallRun).catch(showError);
      return;
    }
    const copyButton = event.target.closest("[data-install-copy], [data-terminal-install-copy]");
    if (copyButton) {
      event.preventDefault();
      const command = copyButton.dataset.command || copyButton.dataset.terminalInstallCopy || "";
      copyInstallCommand(command).catch(showError);
      return;
    }
    const agentButton = event.target.closest("[data-agent-install-command]");
    if (agentButton) {
      event.preventDefault();
      askAgentInstallCommand(agentButton.dataset.agentInstallCommand).catch(showError);
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && projectFolderModal()?.classList.contains("active")) closeProjectFolderModal();
  });
}

// Search Palette functionality

// Initialize search palette
searchPalette.bindEvents();

// Tab switching for Add Memory
function initAddMemoryTabs() {
  const tabButtons = document.querySelectorAll('.add-memory-tabs .tab-button');
  const tabContents = document.querySelectorAll('.add-memory-tabs .tab-content');

  tabButtons.forEach(button => {
    button.addEventListener('click', () => {
      const targetTab = button.dataset.tab;

      // Remove active class from all buttons and contents
      tabButtons.forEach(btn => btn.classList.remove('active'));
      tabContents.forEach(content => content.classList.remove('active'));

      // Add active class to clicked button and corresponding content
      button.classList.add('active');
      const targetContent = document.querySelector(`[data-tab-content="${targetTab}"]`);
      if (targetContent) {
        targetContent.classList.add('active');
      }
    });
  });
}

// Initialize tabs on load
initAddMemoryTabs();

async function bootstrap() {
  bindEvents();
  await loadSettings();
  await loadProjects();
  await loadProjectFiles();
  await loadProviders().catch(showError);
  renderMemoryFiles();
  buildContext("memory context builder").catch(showError);
  refreshWorkspace().catch(showError);
  scheduleGraphLoad();
  loadMemoryCandidates().catch(() => {}); // fills the Review badge in the nav
  await maybeShowOnboarding();
  syncStartupMemoryRescan().catch(() => {});
}
bootstrap().catch(showError);

// Language dropdown toggle
function initLanguageDropdown() {
  const languageBtn = document.getElementById('language-btn');
  const languageDropdown = document.querySelector('.language-dropdown');
  const languageMenu = document.getElementById('language-menu');

  if (!languageBtn || !languageDropdown || !languageMenu) return;

  const close = () => {
    languageDropdown.classList.remove('open');
    languageBtn.setAttribute('aria-expanded', 'false');
  };
  const open = () => {
    languageDropdown.classList.add('open');
    languageBtn.setAttribute('aria-expanded', 'true');
    const active = languageMenu.querySelector('[data-language-option].active') || languageMenu.querySelector('[data-language-option]');
    if (active) active.focus();
  };
  const toggle = () => {
    if (languageDropdown.classList.contains('open')) close();
    else open();
  };

  languageBtn.addEventListener('click', event => {
    event.stopPropagation();
    toggle();
  });

  languageMenu.addEventListener('click', event => {
    const option = event.target.closest('[data-language-option]');
    if (!option) return;
    applyLanguage(option.dataset.languageOption);
    saveUiSettings({ language: state.language }).catch(showError);
    close();
    languageBtn.focus();
  });

  languageMenu.addEventListener('keydown', event => {
    const options = Array.from(languageMenu.querySelectorAll('[data-language-option]'));
    const index = options.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      languageBtn.focus();
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      const next = options[(Math.max(index, 0) + direction + options.length) % options.length];
      next.focus();
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      document.activeElement.click();
    }
  });

  document.addEventListener('click', event => {
    if (!event.target.closest('.language-dropdown')) close();
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') close();
  });

  syncLanguageMenu();
}

// Initialize language dropdown
initLanguageDropdown();

// ============================================
// File Tree Rendering
// ============================================

// ============================================
// File Editor Logic
// ============================================


// Initialize file editor when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => fileEditor.init());
} else {
  fileEditor.init();
}

// Export for use in other modules
window.fileEditor = fileEditor;
// ============================================
// New Project Wizard Logic
// ============================================


// Initialize wizard when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => projectWizard.init());
} else {
  projectWizard.init();
}

// Export for use in other modules
window.projectWizard = projectWizard;
// ============================================
// Workspace Chat Panel Logic
// ============================================


if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => workspaceChat.init());
} else {
  workspaceChat.init();
}

window.workspaceChat = workspaceChat;
