/* Ask UI: council, ask mode, switchView, composer, attachments, rich actions — extracted from app.js */
import { loadAgentHooks } from "./agent-hooks.js";
import { api } from "./api-client.js";
import { appendChatBubble, finalizeChatSession, loadChats, sendChatMessage } from "./chat.js";
import { loadCodeServers } from "./code-intel.js";
import { escapeHtml, on, setElementValue, setTextContent, showSnackbar } from "./dom-utils.js";
import { ASK_MODE_HINTS } from "./i18n.js";
import { initMcpMasterDetail, loadMcpMasterDetail } from "./mcp_master_detail.js";
import { syncIngestSourcesWithMcp } from "./memory-ingest.js";
import { loadMemoryCandidates, loadMemoryLifecycle, renderMemoryFiles } from "./memory-panel.js";
import { loadProjectFiles, loadTasks, refreshWorkspace, runSearch, scheduleGraphLoad } from "./projects.js";
import { loadAnalytics, loadProviderRuns, loadProviders } from "./providers.js";
import { searchPalette } from "./search-palette.js";
import { loadRouterSettings, loadSettings } from "./settings.js";
import { ASK_MODES, SETUP_VIEWS, projectParam, state, t, titleByView } from "./state.js";
import { showError } from "./ui.js";
import { workspaceChat } from "./workspace-chat.js";

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
  syncAskMemoryAdviceBadge();
}

function syncAskMemoryAdviceBadge() {
  const on = state.askMemoryAdvice !== false;
  const label = t("ask.memoryAdvice.badge");
  const title = t("ask.memoryAdvice.badgeTitle");
  for (const id of ["#ask-memory-advice-badge", "#workspace-memory-advice-badge"]) {
    const el = document.querySelector(id);
    if (!el) continue;
    el.hidden = !on;
    el.textContent = label;
    el.title = title;
  }
}
window.syncAskMemoryAdviceBadge = syncAskMemoryAdviceBadge;
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
function commandFromRichCodeButton(button) {
  const wrap = button.closest(".rich-code-wrap");
  return String(wrap?.querySelector("pre.rich-code code")?.textContent || "").replace(/\s+$/, "");
}
function commandForTerminal(text) {
  const lines = String(text || "").split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith("#"));
  if (lines.length <= 1) return lines[0] || String(text || "").trim();
  return lines.join(" && ");
}
function handleRichCodeAction(event) {
  const copyBtn = event.target.closest("[data-rich-code-copy]");
  if (copyBtn) {
    event.preventDefault();
    const command = commandFromRichCodeButton(copyBtn);
    if (!command) return true;
    copyInstallCommand(command).then(() => {
      showSnackbar(t("terminal.copied"), "success");
      copyBtn.classList.add("is-copied");
      window.setTimeout(() => copyBtn.classList.remove("is-copied"), 1200);
    }).catch(showError);
    return true;
  }
  const runBtn = event.target.closest("[data-rich-code-run]");
  if (runBtn) {
    event.preventDefault();
    const command = commandForTerminal(commandFromRichCodeButton(runBtn));
    if (!command) return true;
    import("./terminal.js").then(({ openTerminalModal }) => {
      openTerminalModal({ command, autoRun: false });
    }).catch(showError);
    return true;
  }
  return false;
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
function switchView(view, options = {}) {
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
  let pending = null;
  if (target === "chat") {
    loadProviders();
    pending = loadChats({ openDefault: !options.newThread });
    loadCouncil();
    syncAskMode();
  }
  if (target === "memory") {
    loadMemoryCandidates();
    renderMemoryFiles();
    scheduleGraphLoad();
    loadMemoryLifecycle();
    loadAnalytics();
    syncIngestSourcesWithMcp().catch(() => {});
  }
  if (target === "tasks") loadTasks();
  if (target === "providers") { loadProviders(); loadProviderRuns(); loadRouterSettings(); loadSettings(); }
  if (target === "mcp") { loadMcpMasterDetail(); initMcpMasterDetail(); }
  if (target === "hooks") loadAgentHooks().catch(showError);
  if (target === "code") { loadCodeServers(); }
  if (target === "settings") loadSettings();
  return pending;
}
function autoGrowChatInput() {
  const input = document.querySelector("#chat-message");
  if (!input) return;
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
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
  const html = state.attachments.map(file => {
    const isImage = String(file.mime || "").startsWith("image/");
    const binary = !isImage && file.text_extracted === false;
    const thumb = isImage && file.preview ? `<img class="attachment-thumb" src="${escapeHtml(file.preview)}" alt="">` : "";
    return `<span class="attachment-chip"${binary ? ' data-binary="1"' : ""}${isImage ? ' data-image="1"' : ""}>${thumb}<span class="attachment-name">${escapeHtml(file.name)}</span><span class="attachment-size">${escapeHtml(formatBytes(file.size))}</span><button type="button" class="attachment-remove" data-remove-file="${escapeHtml(file.id)}" aria-label="Remove">&times;</button></span>`;
  }).join("");
  for (const selector of ["#chat-attachments", "#workspace-chat-attachments"]) {
    const box = document.querySelector(selector);
    if (!box) continue;
    box.hidden = state.attachments.length === 0;
    box.innerHTML = html;
  }
}

function isImageFile(file) {
  if (!file) return false;
  if (String(file.type || "").startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp|bmp|tiff?|heic|heif)$/i.test(file.name || "");
}

function screenshotFileName(file) {
  const raw = String(file?.name || "").trim();
  if (raw && raw !== "blob" && /\.[a-z0-9]{2,8}$/i.test(raw) && raw.toLowerCase() !== "image.png") {
    return raw;
  }
  const mime = String(file?.type || "image/png").split(";")[0].trim().toLowerCase();
  const extByMime = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/webp": "webp", "image/bmp": "bmp", "image/tiff": "tiff", "image/heic": "heic",
  };
  const ext = extByMime[mime] || (mime.startsWith("image/") ? mime.slice(6).replace("jpeg", "jpg") : "png");
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
  return `screenshot-${stamp}.${ext}`;
}

function clipboardFileKey(file) {
  if (!file) return "";
  return `${file.type || ""}|${file.size || 0}|${file.lastModified || 0}|${file.name || ""}`;
}

function clipboardImageFiles(clipboardData) {
  if (!clipboardData) return [];
  const files = [];
  const seen = new Set();
  const push = file => {
    if (!file || !isImageFile(file)) return;
    const key = clipboardFileKey(file);
    if (!key || seen.has(key)) return;
    seen.add(key);
    files.push(file);
  };
  if (clipboardData.files && clipboardData.files.length) {
    for (const file of clipboardData.files) push(file);
  }
  if (!files.length && clipboardData.items) {
    for (const item of clipboardData.items) {
      if (item.kind === "file") push(item.getAsFile());
    }
  }
  return files;
}

function isForeignPasteTarget(target) {
  if (!target || typeof target.closest !== "function") return false;
  if (target.closest("#file-editor, .cm-editor, [contenteditable='true']")) return true;
  const field = target.closest("input, textarea, select");
  if (!field) return false;
  return field.id !== "chat-message" && field.id !== "workspace-chat-input";
}

function askPasteSurfaceActive() {
  const chatView = document.querySelector("#chat-view");
  if (chatView && chatView.classList.contains("active")) return true;
  const workspace = document.querySelector("#workspace-view");
  if (!workspace || !workspace.classList.contains("active")) return false;
  const surface = document.getElementById("workspace-layout")?.dataset.surface;
  return surface === "ask" || surface === "split";
}

function pasteComposerField() {
  const chatView = document.querySelector("#chat-view");
  if (chatView && chatView.classList.contains("active")) {
    return document.querySelector("#chat-message");
  }
  return document.querySelector("#workspace-chat-input") || document.querySelector("#chat-message");
}

function insertTextAtCursor(textarea, text) {
  if (!textarea || !text) return;
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  textarea.value = `${textarea.value.slice(0, start)}${text}${textarea.value.slice(end)}`;
  const pos = start + text.length;
  textarea.selectionStart = textarea.selectionEnd = pos;
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

async function filesFromClipboardApi() {
  if (!navigator.clipboard || typeof navigator.clipboard.read !== "function") return [];
  try {
    const items = await navigator.clipboard.read();
    const files = [];
    for (const item of items) {
      const type = (item.types || []).find(entry => String(entry).startsWith("image/"));
      if (!type) continue;
      const blob = await item.getType(type);
      files.push(new File([blob], screenshotFileName({ type, name: "" }), { type }));
    }
    return files;
  } catch (_err) {
    return [];
  }
}

function clipboardLooksLikeImage(clipboardData) {
  if (!clipboardData) return true;
  if (clipboardImageFiles(clipboardData).length) return true;
  const types = clipboardData.types ? [...clipboardData.types] : [];
  if (types.some(entry => {
    const value = String(entry);
    return value.startsWith("image/") || value === "Files" || value.startsWith("public.");
  })) return true;
  const text = clipboardData.getData("text/plain") || "";
  const html = clipboardData.getData("text/html") || "";
  if (text || html) return false;
  return types.length === 0;
}

async function handleComposerPaste(event) {
  if (isForeignPasteTarget(event.target) || !askPasteSurfaceActive()) return;
  const syncFiles = clipboardImageFiles(event.clipboardData);
  if (syncFiles.length) {
    event.preventDefault();
    const text = (event.clipboardData?.getData("text/plain") || "").trim();
    const looksLikeName = !text || /^image\.(png|jpe?g|gif|webp|bmp|tiff?)$/i.test(text)
      || syncFiles.some(file => text === String(file.name || "").trim());
    const field = event.target?.closest?.("textarea") || pasteComposerField();
    if (text && !looksLikeName) insertTextAtCursor(field, text);
    await handleFileSelect(syncFiles);
    showSnackbar(syncFiles.length === 1 ? "Screenshot attached" : `${syncFiles.length} images attached`, "success");
    return;
  }
  if (!clipboardLooksLikeImage(event.clipboardData)) return;
  const asyncFiles = await filesFromClipboardApi();
  if (!asyncFiles.length) return;
  await handleFileSelect(asyncFiles);
  showSnackbar(asyncFiles.length === 1 ? "Screenshot attached" : `${asyncFiles.length} images attached`, "success");
}

function bindAskClipboardPaste() {
  if (document.documentElement.dataset.askPasteBound === "1") return;
  document.documentElement.dataset.askPasteBound = "1";
  document.addEventListener("paste", event => {
    handleComposerPaste(event).catch(showError);
  });
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
  const incoming = Array.from(fileList || []);
  const seen = new Set(state.attachments.map(item => `${item.mime || ""}|${item.size || 0}|${item.name || ""}`));
  for (const file of incoming) {
    const name = isImageFile(file) ? screenshotFileName(file) : (file.name || "file");
    const key = `${file.type || ""}|${file.size || 0}|${name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    try {
      const content = await readFileAsDataUrl(file);
      const result = await api("/api/files", {
        method: "POST",
        body: JSON.stringify({ project_id: state.projectId, name, content, mime: file.type || "" }),
      });
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

function liveAssistantTurn(thread) {
  if (!thread) return null;
  const turns = [...thread.querySelectorAll(".ask-turn-assistant")];
  const last = turns.at(-1);
  if (!last) return null;
  const after = last.nextElementSibling;
  if (after && after.classList.contains("ask-turn-user")) return null;
  return last;
}

function syncQuestionCardState(card, live) {
  if (!card) return;
  card.classList.toggle("is-static", !live);
  card.querySelectorAll("input, button").forEach((el) => {
    el.disabled = !live;
  });
}

function placeAskQuestionCards() {
  const pairs = [
    ["#chat-thread", "#ask-question-slot"],
    ["#workspace-chat-thread", "#workspace-ask-question-slot"],
  ];
  for (const [threadSel, slotSel] of pairs) {
    const thread = document.querySelector(threadSel);
    const slot = document.querySelector(slotSel);
    if (!thread || !slot) continue;
    const liveTurn = liveAssistantTurn(thread);
    thread.querySelectorAll(".ask-question-card").forEach((card) => {
      syncQuestionCardState(card, false);
    });
    if (slot.querySelector(".ask-question-card")) {
      syncQuestionCardState(slot.querySelector(".ask-question-card"), false);
    }
    if (!liveTurn) {
      slot.hidden = true;
      slot.replaceChildren();
      continue;
    }
    const liveCard = liveTurn.querySelector(".ask-question-card") || slot.querySelector(".ask-question-card");
    if (!liveCard) {
      slot.hidden = true;
      slot.replaceChildren();
      continue;
    }
    syncQuestionCardState(liveCard, true);
    if (liveCard.parentElement !== slot) slot.replaceChildren(liveCard);
    slot.hidden = false;
  }
}

let placeQuestionTimer = 0;
function placeAskQuestionCardsSoon() {
  window.clearTimeout(placeQuestionTimer);
  placeQuestionTimer = window.setTimeout(placeAskQuestionCards, 0);
}

function selectedQuestionAnswer(form) {
  const checked = [...form.querySelectorAll("input[type='radio']:checked, input[type='checkbox']:checked")];
  if (!checked.length) return "";
  const otherField = form.querySelector(".ask-question-other-field");
  const labels = checked.map((input) => {
    if (input.value === "__other__") return String(otherField?.value || "").trim();
    return String(input.value || "").trim();
  }).filter(Boolean);
  return labels.join("\n");
}

function refreshQuestionContinue(form) {
  const continueBtn = form.querySelector(".ask-question-continue");
  if (!continueBtn || form.classList.contains("is-static")) return;
  const otherField = form.querySelector(".ask-question-other-field");
  const otherOn = Boolean(form.querySelector("input[value='__other__']:checked"));
  if (otherField) otherField.hidden = !otherOn;
  continueBtn.disabled = !selectedQuestionAnswer(form);
}

function bindAskQuestionCards() {
  if (document.documentElement.dataset.askQuestionBound === "1") return;
  document.documentElement.dataset.askQuestionBound = "1";
  document.addEventListener("aos-rich-rendered", () => placeAskQuestionCardsSoon());
  document.addEventListener("change", (event) => {
    const form = event.target.closest?.(".ask-question-card");
    if (!form || form.classList.contains("is-static")) return;
    refreshQuestionContinue(form);
    if (event.target.matches?.(".ask-question-other-field")) return;
    const otherOn = Boolean(form.querySelector("input[value='__other__']:checked"));
    if (otherOn) form.querySelector(".ask-question-other-field")?.focus();
  });
  document.addEventListener("input", (event) => {
    const form = event.target.closest?.(".ask-question-card");
    if (!form) return;
    refreshQuestionContinue(form);
  });
  document.addEventListener("submit", (event) => {
    const form = event.target.closest?.(".ask-question-card");
    if (!form || form.classList.contains("is-static")) return;
    event.preventDefault();
    const answer = selectedQuestionAnswer(form);
    if (!answer) return;
    form.querySelector(".ask-question-continue")?.setAttribute("disabled", "");
    const slot = form.closest(".ask-question-slot");
    if (slot) {
      slot.hidden = true;
      slot.replaceChildren();
    } else {
      form.remove();
    }
    queueAskFollowUp(answer, { send: true }).catch(showError);
  });
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
  for (const selector of ["#chat-attachments", "#workspace-chat-attachments"]) {
    const attachmentsBox = document.querySelector(selector);
    if (attachmentsBox) attachmentsBox.addEventListener("click", event => {
      const btn = event.target.closest("[data-remove-file]");
      if (btn) removeAttachment(btn.dataset.removeFile).catch(showError);
    });
  }
  bindAskClipboardPaste();
  bindAskQuestionCards();
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
    const connectProvider = event.target.closest("[data-connect-provider]");
    if (connectProvider) {
      switchView("providers");
      return;
    }
    const chip = event.target.closest(".chat-suggestion");
    if (!chip || !input) return;
    input.value = chip.dataset.suggest || chip.textContent || "";
    autoGrowChatInput();
    input.focus();
  });
  document.addEventListener("click", event => {
    if (handleRichCodeAction(event)) return;
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
  if (searchPalette?.openWithQuery) {
    searchPalette.openWithQuery(q);
    return;
  }
  switchView("memory");
  setElementValue("#search-query", q);
  await runSearch(q);
}
async function queueAskFollowUp(prompt, { send = false } = {}) {
  const text = String(prompt || "").trim();
  if (!text) return;
  const workspaceActive = document.querySelector("#workspace-view")?.classList.contains("active");
  const workspaceInput = document.querySelector("#workspace-chat-input");
  if (workspaceActive && workspaceInput && typeof workspaceChat !== "undefined") {
    workspaceInput.value = text;
    workspaceChat.autoResizeInput?.(workspaceInput);
    workspaceInput.focus();
    if (send) await workspaceChat.sendMessage();
    return;
  }
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

export {
  askAgentInstallCommand, autoGrowChatInput, bindChatComposer, closeChatMenu,
  copyInstallCommand, formatBytes, handleComposerPaste, handleFileSelect, loadCouncil,
  placeAskQuestionCards, queueAskFollowUp, readFileAsDataUrl, renderAttachments, switchView, syncAskMode,
  syncAskMemoryAdviceBadge,
};
