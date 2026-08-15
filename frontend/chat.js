/* Chat/Ask thread: list, bubbles, send, council — extracted from app.js */
import {
  beginAgentActivity, finalizeAgentActivity, refreshAgentActivityTitle,
  stopAgentActivityTimer, updateAgentActivity, updateCouncilActivity,
} from "./agent-activity.js";
import { api, authHeaders, readSseEvents } from "./api-client.js";
import { autoGrowChatInput, closeChatMenu, renderAttachments, switchView, syncAskMode } from "./ask-ui.js";
import { escapeHtml, setElementDisabled, showSnackbar } from "./dom-utils.js";
import { loadMemoryCandidates } from "./memory-panel.js";
import { loadProviderRuns } from "./providers.js";
import { ArchitectOSRich } from "./rich-response.js";
import { projectParam, state, t } from "./state.js";
import { showError } from "./ui.js";
import { workspaceChat } from "./workspace-chat.js";

async function loadChats(options = {}) {
  const openDefault = options.openDefault !== false;
  const payload = await api(`/api/chats?project_id=${projectParam()}&limit=80`);
  const list = document.querySelector("#chat-list");
  list.innerHTML = "";
  for (const chat of payload.chats) {
    const el = document.createElement("article");
    el.className = "result";
    const keeper = chat.keeper_status || {};
    const rolling = (chat.context_summaries || []).find(item => item.summary_type === "rolling") || {};
    const statusLabel = chat.session_status === "complete"
      ? "session complete"
      : (keeper.status || "");
    const status = statusLabel ? `<span class="badge">${escapeHtml(statusLabel)}</span>` : "";
    const summary = rolling.summary_text ? `<p class="chat-summary-preview">${escapeHtml(String(rolling.summary_text).split("\n").slice(-1)[0] || "")}</p>` : "";
    const finalizeBtn = chat.session_status === "complete"
      ? `<button type="button" data-chat-finalize="${escapeHtml(chat.id)}">Re-summarize</button>`
      : `<button type="button" data-chat-finalize="${escapeHtml(chat.id)}">End session</button>`;
    el.innerHTML = `<strong>${escapeHtml(chat.title)}</strong><p>${Number(chat.message_count || 0)} messages ${status}</p>${summary}<div class="chat-card-actions"><button type="button" data-chat-context="${escapeHtml(chat.id)}">Context</button>${finalizeBtn}</div>`;
    el.addEventListener("click", event => {
      if (event.target.closest("button")) return;
      openChat(chat.id).catch(showError);
    });
    list.appendChild(el);
  }
  if (openDefault && !state.chatId && payload.chats[0]) await openChat(payload.chats[0].id);
  else if (!state.chatId) updateChatEmpty();
  syncAskHeaderActions();
}
async function fetchChat(chatId) {
  const payload = await api(`/api/chats/${encodeURIComponent(chatId)}`);
  return payload.chat;
}
async function openChat(chatId) {
  const chat = await fetchChat(chatId);
  state.chatId = chat.id;
  renderChat(chat);
  syncAskHeaderActions();
  return chat;
}
function syncAskHeaderActions() {
  setElementDisabled("#chat-end-session", !state.chatId);
}
function startNewAskThread({ notify = true } = {}) {
  state.chatId = "";
  state.lastFailedMessage = "";
  state.attachments = [];
  renderAttachments();
  const thread = document.querySelector("#chat-thread");
  if (thread) {
    thread.innerHTML = "";
    updateChatEmpty();
  }
  if (typeof workspaceChat !== "undefined" && workspaceChat.showEmptyState) workspaceChat.showEmptyState();
  const input = document.querySelector("#chat-message");
  if (input) {
    input.value = "";
    autoGrowChatInput();
    input.focus();
  }
  syncAskHeaderActions();
  if (notify) showSnackbar("Started a new Ask thread. The previous one stays in Dialogs.", "info");
}
async function finalizeChatSession(chatId, trigger = "manual") {
  const result = await api(`/api/chats/${encodeURIComponent(chatId)}/finalize`, {
    method: "POST",
    body: JSON.stringify({ force: true, trigger, project_id: state.projectId }),
  });
  await loadChats({ openDefault: false });
  await loadMemoryCandidates();
  return result;
}
const CHAT_SUGGESTIONS = [
  "Explain adapter routing",
  "Summarize project memory",
  "What are the current tasks?",
  "Review the architecture decisions",
];
function chatEmptyHtml() {
  const chips = CHAT_SUGGESTIONS.map(text => `<button type="button" class="chat-suggestion" data-suggest="${escapeHtml(text)}">${escapeHtml(text)}</button>`).join("");
  return `<div id="chat-empty" class="chat-empty"><div class="chat-empty-inner"><div class="chat-empty-mark">AO</div><p class="chat-empty-title">Ask ArchitectOS using project memory</p><p class="chat-empty-sub">Pick the model below, or start with a suggestion.</p><div class="chat-suggestions">${chips}</div></div></div>`;
}
function updateChatEmpty() {
  const thread = document.querySelector("#chat-thread");
  if (!thread) return;
  if (!thread.querySelector(".message")) {
    if (!thread.querySelector("#chat-empty")) thread.innerHTML = chatEmptyHtml();
  } else {
    const empty = thread.querySelector("#chat-empty");
    if (empty) empty.remove();
  }
}
function renderChat(chat) {
  const thread = document.querySelector("#chat-thread");
  thread.innerHTML = "";
  (chat.messages || []).forEach((message, index) => appendChatBubble(message.role, message.text, false, message.provider, {
    chatId: chat.id,
    messageIndex: index,
    favorite: Boolean(message.favorite),
    retrievalRating: Number(message.retrieval_rating || 0),
    memoryHitIds: Array.isArray(message.memory_hit_ids) ? message.memory_hit_ids : [],
    toolTrace: Array.isArray(message.tool_trace) ? message.tool_trace : [],
    structured: message.structured || null,
    rawText: message.raw_text || "",
    usage: message.usage || null,
  }));
  updateChatEmpty();
  thread.scrollTop = thread.scrollHeight;
  if (typeof workspaceChat !== "undefined" && workspaceChat.renderFromChat) {
    workspaceChat.renderFromChat(chat);
  }
}
function providerLabel(provider) {
  if (!provider) return "";
  const selected = provider.selected || {};
  const label = selected.label || provider.label || provider.id || "";
  const model = selected.model || provider.model || "";
  if (model && label && !String(label).toLowerCase().includes(String(model).toLowerCase())) {
    return `${label} · ${model}`;
  }
  return label || model || "";
}
function formatTokenCount(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 1000000) return `${(n / 1000000).toFixed(n >= 10000000 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(Math.round(n));
}
function formatUsageCost(cost) {
  const n = Number(cost);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n === 0) return "$0";
  if (n < 0.001) return `$${n.toFixed(5)}`;
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(3)}`;
}
function formatUsageLabel(usage) {
  if (!usage || typeof usage !== "object") return "";
  const prompt = Number(usage.prompt_tokens || 0);
  const completion = Number(usage.completion_tokens || 0);
  const total = Number(usage.total_tokens || (prompt + completion));
  if (!prompt && !completion && !total) return "";
  const parts = [
    `${formatTokenCount(prompt)} in`,
    `${formatTokenCount(completion)} out`,
  ];
  if (total) parts.push(`${formatTokenCount(total)} tot`);
  const cost = formatUsageCost(usage.cost_usd);
  if (cost) parts.push(usage.cost_estimated ? `~${cost}` : cost);
  return parts.join(" · ");
}
function setBubbleUsage(el, usage) {
  if (!el) return;
  let footer = el.querySelector(".message-usage");
  const label = formatUsageLabel(usage);
  if (!label) {
    if (footer) footer.remove();
    return;
  }
  if (!footer) {
    footer = document.createElement("div");
    footer.className = "message-usage";
    const actions = el.querySelector(".message-actions");
    if (actions) el.insertBefore(footer, actions);
    else el.appendChild(footer);
  }
  footer.textContent = label;
  footer.title = t("chat.usageTitle");
}
function setAgentActivityModel(bubble, provider) {
  const panel = bubble && bubble.querySelector(".agent-activity");
  if (!panel) return;
  const label = providerLabel(provider);
  if (label) panel.dataset.modelLabel = label;
  refreshAgentActivityTitle(panel);
}
function setBubbleProvider(el, provider) {
  const persona = el && el._persona;
  if (!persona) return;
  const label = providerLabel(provider) || "AI";
  const routing = (provider && provider.routing) || {};
  const tag = String(routing.role || routing.strategy || "").trim();
  const reason = String(routing.reason || "").trim();
  persona.title = reason || label;
  persona.innerHTML = `<span class="msg-avatar">${escapeHtml((label.trim().charAt(0) || "A").toUpperCase())}</span><span class="msg-persona-name">${escapeHtml(label)}</span>${tag ? `<span class="msg-persona-tag">${escapeHtml(tag)}</span>` : ""}`;
  setAgentActivityModel(el, provider);
}
function isProviderError(provider) {
  const status = String((provider && provider.status) || "");
  return status === "error" || status === "approval_required";
}
function providerErrorAction(text) {
  const value = String(text || "").toLowerCase();
  if (value.includes("approval")) {
    return { label: "Enable CLI runs", action: "cli" };
  }
  return { label: "Configure providers", action: "providers" };
}
function handleProviderErrorAction(action) {
  if (action === "cli") {
    const checkbox = document.querySelector("#chat-approve-cli");
    if (checkbox) checkbox.checked = true;
    showSnackbar("CLI runs enabled for this dialog. Send your message again.", "info");
    return;
  }
  if (action === "retry-auto") {
    retryChatWithAuto(state.lastFailedMessage).catch(showError);
    return;
  }
  switchView("providers");
}
function askProviderId() {
  if (state.askMode === "memory") return "local-memory";
  if (state.askMode === "memory-mcp") {
    const value = document.querySelector("#chat-provider")?.value || "auto";
    return value === "local-memory" ? "auto" : value;
  }
  return document.querySelector("#chat-provider")?.value || "auto";
}
function usedExplicitProvider() {
  const providerValue = askProviderId();
  return providerValue && providerValue !== "auto" && providerValue !== "local-memory"
    && state.askMode !== "memory";
}
async function retryChatWithAuto(message) {
  const text = String(message || "").trim();
  if (!text) {
    showSnackbar("No message to retry.", "info");
    return;
  }
  const selects = [document.querySelector("#chat-provider"), document.querySelector("#workspace-chat-provider")].filter(Boolean);
  for (const select of selects) select.value = "auto";
  syncAskMode();
  const input = document.querySelector("#chat-message");
  if (input) {
    input.value = text;
    autoGrowChatInput();
  }
  showSnackbar(t("provider.retryAuto"), "info");
  await sendChatMessage(new Event("submit"));
}
function renderProviderErrorBubble(el, text, provider) {
  el.classList.remove("streaming");
  el.classList.add("error");
  el.textContent = "";
  const body = document.createElement("div");
  body.className = "provider-error-text";
  body.textContent = text || "The selected provider could not respond.";
  el.appendChild(body);
  const cta = providerErrorAction(text);
  const actions = document.createElement("div");
  actions.className = "provider-error-actions";
  if (usedExplicitProvider()) {
    const retryButton = document.createElement("button");
    retryButton.type = "button";
    retryButton.className = "btn btn-primary btn-sm";
    retryButton.textContent = t("provider.retryAuto");
    retryButton.addEventListener("click", () => handleProviderErrorAction("retry-auto"));
    actions.appendChild(retryButton);
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-secondary btn-sm";
  button.textContent = cta.label;
  button.addEventListener("click", () => handleProviderErrorAction(cta.action));
  actions.appendChild(button);
  el.appendChild(actions);
}
function getMessageTextElement(bubble) {
  return bubble.querySelector(".message-text") || bubble;
}
function renderAssistantRichContent(bubble, text, structuredHint) {
  const textNode = getMessageTextElement(bubble);
  if (!textNode) return;
  if (ArchitectOSRich && typeof ArchitectOSRich.renderInto === "function") {
    ArchitectOSRich.renderInto(textNode, text || "", structuredHint || null);
    return;
  }
  textNode.textContent = text || "";
}
function appendChatBubble(role, text, streaming = false, provider = null, meta = {}) {
  const thread = document.querySelector("#chat-thread");
  const empty = thread.querySelector("#chat-empty");
  if (empty) empty.remove();
  let persona = null;
  if (role === "assistant") {
    persona = document.createElement("div");
    persona.className = "msg-persona";
    thread.appendChild(persona);
  }
  const el = document.createElement("div");
  el.className = `message ${role}${streaming ? " streaming" : ""}`;
  if (role === "assistant") {
    const textNode = document.createElement("div");
    textNode.className = "message-text";
    el.appendChild(textNode);
    if (streaming) {
      textNode.textContent = text || "";
    } else {
      renderAssistantRichContent(el, meta.rawText || text || "", meta.structured || null);
    }
    if (!streaming && Array.isArray(meta.toolTrace) && meta.toolTrace.length) {
      finalizeAgentActivity(el, meta.toolTrace);
    }
    if (!streaming && meta.chatId && Number.isInteger(meta.messageIndex)) {
      const actions = document.createElement("div");
      actions.className = "message-actions";
      const rating = Number(meta.retrievalRating || 0);
      const upActive = rating > 0 ? "active" : "";
      const downActive = rating < 0 ? "active" : "";
      actions.innerHTML = [
        `<button type="button" class="message-action-btn ${meta.favorite ? "active" : ""}" data-favorite-message="${escapeHtml(meta.chatId)}" data-message-index="${escapeHtml(String(meta.messageIndex))}" title="${meta.favorite ? "Saved as favorite" : "Save reply as memory candidate"}">★</button>`,
        `<button type="button" class="message-action-btn ${upActive}" data-feedback-rating="1" data-feedback-chat="${escapeHtml(meta.chatId)}" data-message-index="${escapeHtml(String(meta.messageIndex))}" title="Memory helped">👍</button>`,
        `<button type="button" class="message-action-btn ${downActive}" data-feedback-rating="-1" data-feedback-chat="${escapeHtml(meta.chatId)}" data-message-index="${escapeHtml(String(meta.messageIndex))}" title="Memory did not help">👎</button>`,
      ].join("");
      el.appendChild(actions);
    }
    if (!streaming) setBubbleUsage(el, meta.usage);
  } else {
    el.textContent = text || "";
  }
  thread.appendChild(el);
  if (persona) { el._persona = persona; setBubbleProvider(el, provider); }
  if (role === "assistant" && !streaming && isProviderError(provider)) {
    renderProviderErrorBubble(el, text, provider);
  }
  thread.scrollTop = thread.scrollHeight;
  return el;
}
async function sendChatMessage(event, options = {}) {
  event.preventDefault();
  const workspaceMirror = Boolean(options.workspaceMirror);
  const input = document.querySelector("#chat-message");
  const message = input.value.trim();
  if (!message) return;
  if (state.askMode === "council") {
    await runAskCouncil(message);
    return;
  }
  const providerValue = askProviderId();
  state.lastFailedMessage = message;
  const payload = {
    project_id: state.projectId,
    chat_id: state.chatId,
    message,
    provider_id: providerValue,
    ask_mode: state.askMode,
    remember: document.querySelector("#chat-remember").checked,
    allow_cli: document.querySelector("#chat-approve-cli").checked,
    attachments: state.attachments.map(file => file.id),
  };
  input.value = "";
  autoGrowChatInput();
  closeChatMenu();
  state.attachments = [];
  renderAttachments();
  appendChatBubble("user", message);
  const assistant = appendChatBubble("assistant", "", true);
  beginAgentActivity(assistant);
  if (workspaceMirror && typeof workspaceChat !== "undefined") {
    workspaceChat.beginStream(message);
  }
  try {
    const response = await fetch("/api/chat/message/stream", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(payload) });
    if (!response.ok || !response.body) {
      const fallback = await response.json();
      throw new Error(fallback.error || `Stream failed: ${response.status}`);
    }
    let finalChat = null;
    for await (const event of readSseEvents(response)) {
      {
        if (event.type === "start") {
          state.activeRunId = event.run_id || "";
          setElementDisabled("#chat-stop", !state.activeRunId);
          if (event.provider) setBubbleProvider(assistant, event.provider);
          beginAgentActivity(assistant);
          if (workspaceMirror) workspaceChat.onStreamStart?.(event);
        } else if (event.type === "delta") {
          getMessageTextElement(assistant).textContent += event.text || "";
          if (workspaceMirror) workspaceChat.onStreamDelta?.(event.text || "");
        } else if (event.type === "progress") {
          updateAgentActivity(assistant, event);
          if (workspaceMirror) workspaceChat.onStreamProgress?.(event);
        } else if (event.type === "done") {
          assistant.classList.remove("streaming");
          state.activeRunId = "";
          setElementDisabled("#chat-stop", true);
          if (event.provider) setBubbleProvider(assistant, event.provider);
          if (event.usage) setBubbleUsage(assistant, event.usage);
          if (isProviderError(event.provider)) {
            state.lastFailedMessage = message;
            renderProviderErrorBubble(assistant, event.response || getMessageTextElement(assistant).textContent, event.provider);
          } else {
            const finalText = event.response || getMessageTextElement(assistant).textContent || "";
            const withStderr = event.raw && event.raw.stderr ? `${finalText}\n\nstderr: ${event.raw.stderr}` : finalText;
            renderAssistantRichContent(assistant, withStderr, event.structured || null);
            finalizeAgentActivity(assistant, event.tool_trace);
          }
          if (event.chat && event.chat.id) {
            state.chatId = event.chat.id;
            finalChat = event.chat;
            syncAskHeaderActions();
          }
          if (workspaceMirror) workspaceChat.onStreamDone?.(event);
        } else if (event.type === "error") {
          assistant.classList.remove("streaming");
          stopAgentActivityTimer(assistant.querySelector(".agent-activity"));
          state.lastFailedMessage = message;
          renderProviderErrorBubble(assistant, event.error || "stream error", { status: "error" });
          if (workspaceMirror) workspaceChat.onStreamError?.(event.error || "stream error");
        }
        document.querySelector("#chat-thread").scrollTop = document.querySelector("#chat-thread").scrollHeight;
      }
    }
    if (workspaceMirror) {
      if (finalChat) workspaceChat.renderFromChat(finalChat);
      else if (typeof workspaceChat.refreshThread === "function") await workspaceChat.refreshThread({ allowEmpty: false });
    } else if (typeof workspaceChat !== "undefined" && workspaceChat.refreshThread) {
      await workspaceChat.refreshThread();
    }
  } catch (error) {
    const text = error?.message || t("error.serverUnreachable");
    state.lastFailedMessage = message;
    renderProviderErrorBubble(assistant, `⚠️ ${text}`, { status: "error" });
    if (workspaceMirror) workspaceChat.onStreamError?.(text);
  } finally {
    assistant.classList.remove("streaming");
    state.activeRunId = "";
    setElementDisabled("#chat-stop", true);
    stopAgentActivityTimer(assistant.querySelector(".agent-activity"));
  }
  await loadChats({ openDefault: false });
  await loadProviderRuns();
}

async function runAskCouncil(message) {
  const input = document.querySelector("#chat-message");
  const attachments = state.attachments.map(file => file.id);
  if (input) {
    input.value = "";
    autoGrowChatInput();
  }
  closeChatMenu();
  state.attachments = [];
  renderAttachments();
  appendChatBubble("user", message);
  const assistant = appendChatBubble("assistant", "⚖️ Convening the council...", true, { selected: { label: "Council" }, routing: { role: "council" } });
  beginAgentActivity(assistant);
  try {
    const models = [...document.querySelectorAll(".ask-council-model")].filter(el => el.checked).map(el => el.value);
    const response = await fetch("/api/council/run/stream", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        project_id: state.projectId,
        chat_id: state.chatId,
        message,
        models,
        judge_provider_id: document.querySelector("#ask-council-judge")?.value || "auto",
        allow_cli: Boolean(document.querySelector("#chat-approve-cli")?.checked),
        attachments,
      }),
    });
    if (!response.ok || !response.body) {
      const fallback = await response.json();
      throw new Error(fallback.error || `Stream failed: ${response.status}`);
    }
    for await (const event of readSseEvents(response)) {
      {
        if (event.type === "progress") {
          updateCouncilActivity(assistant, event);
        } else if (event.type === "done") {
          const result = event.result;
          assistant.classList.remove("streaming");
          if (result.chat && result.chat.id) {
            state.chatId = result.chat.id;
            syncAskHeaderActions();
          }
          const parts = [];
          if (result.synthesis) parts.push(result.synthesis);
          for (const answer of result.answers || []) {
            const label = answer.label || answer.provider_id;
            parts.push(`\n\n— ${label} —\n${answer.text || "(no answer)"}`);
          }
          const combined = result.response || parts.join("").trim() || "No model produced a response.";
          renderAssistantRichContent(assistant, combined, result.structured || null);
          const messages = result.chat && Array.isArray(result.chat.messages) ? result.chat.messages : [];
          const savedTrace = messages.length ? messages[messages.length - 1].tool_trace : [];
          finalizeAgentActivity(assistant, Array.isArray(savedTrace) ? savedTrace : []);
        }
        document.querySelector("#chat-thread").scrollTop = document.querySelector("#chat-thread").scrollHeight;
      }
    }
  } catch (error) {
    assistant.classList.remove("streaming");
    renderProviderErrorBubble(assistant, `Council failed: ${error.message || error}`, { status: "error" });
  }
  document.querySelector("#chat-thread").scrollTop = document.querySelector("#chat-thread").scrollHeight;
  await loadProviderRuns();
  if (typeof workspaceChat !== "undefined" && workspaceChat.refreshThread) {
    await workspaceChat.refreshThread();
  }
}
async function cancelActiveRun() {
  if (!state.activeRunId) return;
  await api(`/api/runs/${state.activeRunId}/cancel`, { method: "POST", body: "{}" });
  setElementDisabled("#chat-stop", true);
}

export {
  appendChatBubble, cancelActiveRun, fetchChat, finalizeChatSession,
  formatTokenCount, formatUsageCost, formatUsageLabel, getMessageTextElement,
  handleProviderErrorAction, isProviderError, loadChats, providerErrorAction,
  providerLabel, renderAssistantRichContent, renderProviderErrorBubble,
  sendChatMessage, setAgentActivityModel, setBubbleProvider, setBubbleUsage,
  startNewAskThread, syncAskHeaderActions, usedExplicitProvider,
};
