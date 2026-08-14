/* Ask UI: council, ask mode, switchView, composer, attachments, rich actions — extracted from app.js */
async function loadCouncil() {
  let config;
  try {
    config = await api("/api/council");
  } catch (_err) {
    config = { models: [], default: [] };
  }
  const models = (Array.isArray(config.models) ? config.models : []).filter(m => m && m.id !== "auto");
  const defaults = new Set(Array.isArray(config.default) ? config.default : []);
  const container = document.querySelector("#ask-council-models");
  if (container) {
    if (!models.length) {
      container.innerHTML = `<p class="ask-team-sub">No API models connected yet. Add providers in Setup → Providers to use the council.</p>`;
    } else {
      container.innerHTML = models.map(model => {
        const disabled = !model.ready;
        const checked = model.ready && defaults.has(model.id);
        const status = model.ready ? "" : "unavailable";
        return `<label class="ask-council-model-chip${disabled ? " is-disabled" : ""}">
          <input class="ask-council-model" type="checkbox" value="${escapeHtml(model.id)}"${checked ? " checked" : ""}${disabled ? " disabled" : ""}>
          <span class="ask-council-model-name">${escapeHtml(model.label || model.id)}</span>
          ${status ? `<span class="ask-council-model-status">${escapeHtml(status)}</span>` : ""}
        </label>`;
      }).join("");
    }
  }
  const judge = document.querySelector("#ask-council-judge");
  if (judge) {
    const options = [`<option value="auto">Auto (router)</option>`].concat(
      models.map(model => `<option value="${escapeHtml(model.id)}"${model.ready ? "" : " disabled"}>${escapeHtml(model.label || model.id)}${model.ready ? "" : " (unavailable)"}</option>`)
    );
    judge.innerHTML = options.join("");
    judge.value = "auto";
  }
  document.querySelectorAll(".ask-council-model").forEach(input => {
    input.addEventListener("change", () => updateAskCouncilCount());
  });
  bindAskCouncilToggle();
  applyAskCouncilCollapsed(readAskCouncilCollapsed());
  updateAskCouncilCount();
}

function readAskCouncilCollapsed() {
  try {
    const raw = localStorage.getItem("architectos_ask_council_collapsed");
    if (raw == null) return true;
    return raw !== "0";
  } catch (_err) {
    return true;
  }
}

function writeAskCouncilCollapsed(collapsed) {
  try {
    localStorage.setItem("architectos_ask_council_collapsed", collapsed ? "1" : "0");
  } catch (_err) {
    /* ignore */
  }
}

function applyAskCouncilCollapsed(collapsed) {
  const panel = document.querySelector("#ask-council-options");
  const toggle = document.querySelector("#ask-council-toggle");
  const body = document.querySelector("#ask-council-body");
  if (!panel || !toggle || !body) return;
  panel.classList.toggle("is-collapsed", collapsed);
  body.hidden = collapsed;
  toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  writeAskCouncilCollapsed(collapsed);
}

function bindAskCouncilToggle() {
  const toggle = document.querySelector("#ask-council-toggle");
  if (!toggle || toggle.dataset.bound === "1") return;
  toggle.dataset.bound = "1";
  toggle.addEventListener("click", () => {
    const panel = document.querySelector("#ask-council-options");
    const next = !(panel && panel.classList.contains("is-collapsed"));
    applyAskCouncilCollapsed(next);
  });
}

function updateAskCouncilCount() {
  const countEl = document.querySelector("#ask-council-count");
  if (!countEl) return;
  const selected = [...document.querySelectorAll(".ask-council-model")].filter(el => el.checked).length;
  countEl.textContent = selected ? `${selected} model${selected === 1 ? "" : "s"}` : "auto panel";
}

function openSetupNav(forceOpen = true) {
  const group = document.querySelector(".nav-group-setup");
  const toggle = document.querySelector("#nav-setup-toggle");
  const items = document.querySelector("#nav-setup-items");
  if (!group || !toggle || !items) return;
  const open = forceOpen === true ? true : forceOpen === false ? false : !group.classList.contains("open");
  group.classList.toggle("open", open);
  items.hidden = !open;
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
}
function syncAskMode(mode = state.askMode || "quick") {
  state.askMode = ASK_MODES.includes(mode) ? mode : "quick";
  document.querySelectorAll("[data-ask-mode]").forEach(btn => {
    const active = btn.dataset.askMode === state.askMode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  const hint = document.querySelector("#ask-mode-hint");
  if (hint) {
    const text = t(`ask.hint.${state.askMode}`) || ASK_MODE_HINTS[state.askMode];
    hint.textContent = text;
    hint.hidden = state.askMode === "council";
  }
  const options = document.querySelector("#ask-council-options");
  if (options) options.hidden = state.askMode !== "council";
  if (state.askMode === "council") updateAskCouncilCount();
  const provider = document.querySelector("#chat-provider");
  if (provider) {
    // Memory = forced local stub. Council uses its own model picker.
    const lockProvider = state.askMode === "memory" || state.askMode === "council";
    provider.disabled = lockProvider;
    if (state.askMode === "memory") provider.value = "local-memory";
    else if (state.askMode === "memory-mcp" && (provider.value === "local-memory" || !provider.value)) {
      provider.value = "auto";
    }
  }
}
async function copyInstallCommand(command) {
  if (!command) return;
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(command);
  } else {
    const textarea = document.createElement("textarea");
    textarea.value = command;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }
  const summary = document.querySelector("#code-summary");
  if (summary) summary.textContent = "command copied";
}
async function askAgentInstallCommand(command) {
  if (!command) return;
  syncAskMode("quick");
  switchView("chat");
  const message = [
    "Execute or safely guide this install command for the current project.",
    "Check the project context first, explain any risk, then run it only if it is appropriate.",
    "",
    `Command: ${command}`,
  ].join("\n");
  const input = document.querySelector("#chat-message");
  const approve = document.querySelector("#chat-approve-cli");
  if (input) { input.value = message; autoGrowChatInput(); }
  if (approve) approve.checked = true;
  document.querySelector("#chat-form")?.requestSubmit();
}
function switchView(view) {
  if (view === "analytics") {
    switchView("memory");
    return;
  }
  const target = view === "agents" ? "chat" : view;
  if (view === "agents") syncAskMode("council");
  if (SETUP_VIEWS.has(target)) openSetupNav(true);
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === target));
  document.querySelectorAll(".view").forEach(item => item.classList.remove("active"));
  const viewEl = document.querySelector(`#${target}-view`);
  if (viewEl) viewEl.classList.add("active");
  setTextContent("#view-title", t(titleByView[target]));
  if (target === "workspace") {
    refreshWorkspace();
    loadProjectFiles();
    if (typeof workspaceChat !== "undefined") {
      workspaceChat.syncProviders?.();
      workspaceChat.syncControlsFromAsk?.();
      workspaceChat.refreshThread?.();
    }
  }
  if (target === "chat") { loadProviders(); loadChats(); loadCouncil(); syncAskMode(); }
  if (target === "memory") {
    loadMemoryCandidates();
    renderMemoryFiles();
    scheduleGraphLoad();
    loadMemoryLifecycle();
    loadAnalytics();
    syncIngestSourcesWithMcp().catch(() => {});
  }
  if (target === "tasks") loadTasks();
  if (target === "providers") { loadProviders(); loadProviderRuns(); loadRouterSettings(); }
  if (target === "mcp") { loadMcpMasterDetail(); initMcpMasterDetail(); }
  if (target === "code") { fillCodeFileFromSelection(false); syncCodeFileHint(); syncCodeInsightTab(); loadCodeServers(); }
  if (target === "terminal") renderTerminalHistory();
  if (target === "settings") loadSettings();
}
function autoGrowChatInput() {
  const input = document.querySelector("#chat-message");
  if (!input) return;
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}
function closeChatMenu() {
  const menu = document.querySelector("#chat-menu");
  const plus = document.querySelector("#chat-plus");
  if (menu) menu.hidden = true;
  if (plus) plus.setAttribute("aria-expanded", "false");
}
function toggleChatMenu() {
  const menu = document.querySelector("#chat-menu");
  const plus = document.querySelector("#chat-plus");
  if (!menu) return;
  const open = menu.hidden;
  menu.hidden = !open;
  if (plus) plus.setAttribute("aria-expanded", String(open));
}
function formatBytes(size) {
  if (!size) return "0 B";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
function renderAttachments() {
  const box = document.querySelector("#chat-attachments");
  if (!box) return;
  box.hidden = state.attachments.length === 0;
  box.innerHTML = state.attachments.map(file => {
    const isImage = String(file.mime || "").startsWith("image/");
    const binary = !isImage && file.text_extracted === false;
    const thumb = isImage && file.preview ? `<img class="attachment-thumb" src="${escapeHtml(file.preview)}" alt="">` : "";
    return `<span class="attachment-chip"${binary ? ' data-binary="1"' : ""}${isImage ? ' data-image="1"' : ""}>${thumb}<span class="attachment-name">${escapeHtml(file.name)}</span><span class="attachment-size">${escapeHtml(formatBytes(file.size))}</span><button type="button" class="attachment-remove" data-remove-file="${escapeHtml(file.id)}" aria-label="Remove">&times;</button></span>`;
  }).join("");
}
function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
async function handleFileSelect(fileList) {
  const files = Array.from(fileList || []);
  for (const file of files) {
    try {
      const content = await readFileAsDataUrl(file);
      const result = await api("/api/files", { method: "POST", body: JSON.stringify({ project_id: state.projectId, name: file.name, content }) });
      if (result.file) {
        if (String(result.file.mime || "").startsWith("image/")) result.file.preview = content;
        state.attachments.push(result.file);
        renderAttachments();
      }
    } catch (error) { showError(error); }
  }
}
async function removeAttachment(fileId) {
  const file = state.attachments.find(item => item.id === fileId);
  state.attachments = state.attachments.filter(item => item.id !== fileId);
  renderAttachments();
  if (file) { try { await api("/api/files/delete", { method: "POST", body: JSON.stringify({ project_id: state.projectId, id: fileId }) }); } catch (error) { showError(error); } }
}
function bindChatComposer() {
  const input = document.querySelector("#chat-message");
  if (input) {
    input.addEventListener("input", autoGrowChatInput);
    input.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        document.querySelector("#chat-form").requestSubmit();
      }
    });
  }
  const plus = document.querySelector("#chat-plus");
  if (plus) plus.addEventListener("click", event => { event.stopPropagation(); toggleChatMenu(); });
  document.querySelectorAll("[data-ask-mode]").forEach(btn => {
    btn.addEventListener("click", () => syncAskMode(btn.dataset.askMode));
  });
  const setupToggle = document.querySelector("#nav-setup-toggle");
  if (setupToggle) setupToggle.addEventListener("click", () => openSetupNav());
  const fileInput = document.querySelector("#chat-file-input");
  const openPicker = () => { closeChatMenu(); if (fileInput) fileInput.click(); };
  const attach = document.querySelector("#chat-attach");
  if (attach) attach.addEventListener("click", openPicker);
  const attachMenu = document.querySelector("#chat-attach-menu");
  if (attachMenu) attachMenu.addEventListener("click", openPicker);
  if (fileInput) fileInput.addEventListener("change", event => { handleFileSelect(event.target.files).catch(showError); event.target.value = ""; });
  const attachmentsBox = document.querySelector("#chat-attachments");
  if (attachmentsBox) attachmentsBox.addEventListener("click", event => {
    const btn = event.target.closest("[data-remove-file]");
    if (btn) removeAttachment(btn.dataset.removeFile).catch(showError);
  });
  on("#chat-thread", "click", event => {
    const favoriteBtn = event.target.closest("[data-favorite-message]");
    if (favoriteBtn) {
      api(`/api/chats/${encodeURIComponent(favoriteBtn.dataset.favoriteMessage)}/favorite-message`, {
        method: "POST",
        body: JSON.stringify({ message_index: Number(favoriteBtn.dataset.messageIndex || 0), favorite: true }),
      }).then(async () => {
        await loadChats();
        await loadMemoryCandidates();
      }).catch(showError);
      return;
    }
    const feedbackBtn = event.target.closest("[data-feedback-rating]");
    if (feedbackBtn) {
      api("/api/memory/feedback", {
        method: "POST",
        body: JSON.stringify({
          project_id: state.projectId,
          chat_id: feedbackBtn.dataset.feedbackChat,
          message_index: Number(feedbackBtn.dataset.messageIndex || 0),
          rating: Number(feedbackBtn.dataset.feedbackRating || 0),
        }),
      }).then(async () => {
        await loadChats();
      }).catch(showError);
      return;
    }
    const richAction = event.target.closest("[data-rich-action]");
    if (richAction) {
      event.preventDefault();
      handleRichAction({
        type: richAction.dataset.richAction,
        id: richAction.dataset.richId,
        prompt: richAction.dataset.richPrompt,
        query: richAction.dataset.richQuery,
        label: richAction.textContent,
      }).catch(showError);
      return;
    }
    const richLink = event.target.closest("[data-rich-link]");
    if (richLink) {
      event.preventDefault();
      const href = String(richLink.getAttribute("data-rich-link") || richLink.getAttribute("href") || "");
      if (/^https?:/i.test(href)) {
        window.open(href, "_blank", "noopener");
        return;
      }
      const work = href.match(/^(?:ab|ado):\/\/?#?(\d+)/i) || href.match(/^AB#(\d+)/i);
      if (work) {
        handleRichAction({ type: "open_work_item", id: work[1] }).catch(showError);
        return;
      }
      const memory = href.match(/^memory:\/\/?#?(.+)$/i);
      if (memory) {
        handleRichAction({ type: "memory_get", id: memory[1] }).catch(showError);
        return;
      }
      return;
    }
    const chip = event.target.closest(".chat-suggestion");
    if (!chip || !input) return;
    input.value = chip.dataset.suggest || chip.textContent || "";
    autoGrowChatInput();
    input.focus();
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".composer-menu-wrap")) closeChatMenu();
  });
  on("#chat-list", "click", event => {
    const finalize = event.target.closest("[data-chat-finalize]");
    if (finalize) {
      finalizeChatSession(finalize.dataset.chatFinalize)
        .then(result => {
          const label = result?.candidate?.label || (result?.skipped ? "Session closed (no new candidate)" : "Session summarized");
          showSnackbar(label, result?.candidate ? "success" : "info");
        })
        .catch(showError);
      return;
    }
    const retry = event.target.closest("[data-chat-retry]");
    if (retry) {
      finalizeChatSession(retry.dataset.chatRetry, "manual_retry").catch(showError);
      return;
    }
    const context = event.target.closest("[data-chat-context]");
    if (context) {
      api(`/api/chats/${encodeURIComponent(context.dataset.chatContext)}/context-pack?project_id=${projectParam()}`)
        .then(pack => showContextPack(pack))
        .catch(showError);
    }
  });
  syncAskMode();
}

function showContextPack(pack) {
  const rolling = pack.rolling_summary?.summary_text || "No rolling summary yet.";
  const decisions = pack.decision_log?.summary_text || "No decisions captured yet.";
  const pending = (pack.pending_candidates || []).map(item => `- ${item.label}`).join("\n") || "No pending chat candidates.";
  const text = `Rolling summary:\n${rolling}\n\nDecision log:\n${decisions}\n\nPending candidates:\n${pending}`;
  appendChatBubble("assistant", text, false, { id: "context-pack", label: "Context Pack" });
}
async function openMemorySearch(query) {
  const q = String(query || "").trim();
  if (!q) return;
  if (typeof searchPalette !== "undefined" && searchPalette.open) {
    searchPalette.open();
    const input = document.getElementById("paletteSearchInput");
    if (input) {
      input.value = q;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return;
  }
  switchView("memory");
  setElementValue("#search-query", q);
  await runSearch(q);
}
async function queueAskFollowUp(prompt, { send = false } = {}) {
  const text = String(prompt || "").trim();
  if (!text) return;
  switchView("chat");
  const input = document.querySelector("#chat-message");
  if (input) {
    input.value = text;
    autoGrowChatInput();
    input.focus();
  }
  if (send) {
    await sendChatMessage(new Event("submit"));
  } else {
    showSnackbar("Follow-up ready in Ask composer.", "info");
  }
}
async function handleRichAction(action) {
  const type = String(action.type || "").trim();
  const id = String(action.id || "").trim();
  if (type === "ask") {
    await queueAskFollowUp(action.prompt || action.label || "", { send: true });
    return;
  }
  if (type === "open_work_item" || type === "boards_get_item") {
    await openMemorySearch(id ? `AB#${id}` : "work item");
    showSnackbar(id ? `Searching memory for AB#${id}` : "Searching work items", "info");
    return;
  }
  if (type === "boards_search") {
    const query = String(action.query || id || "").trim();
    await queueAskFollowUp(query ? `Search Azure Boards for: ${query}` : "Search my Azure Boards work items", { send: true });
    return;
  }
  if (type === "memory_get") {
    await openMemorySearch(id || "memory");
    showSnackbar(id ? `Searching memory for ${id}` : "Searching memory", "info");
    return;
  }
  showSnackbar(`Unsupported action: ${type || "unknown"}`, "info");
}
