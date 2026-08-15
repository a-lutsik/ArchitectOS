// Workspace Ask/chat panel. ES module.
import { beginAgentActivity, finalizeAgentActivity, stopAgentActivityTimer, updateAgentActivity } from "./agent-activity.js";
import { api } from "./api-client.js";
import { autoGrowChatInput, handleFileSelect, switchView, syncAskMode } from "./ask-ui.js";
import {
  fetchChat, getMessageTextElement, handleProviderErrorAction, isProviderError,
  providerErrorAction, providerLabel, renderAssistantRichContent, renderProviderErrorBubble,
  sendChatMessage, setAgentActivityModel, setBubbleProvider, setBubbleUsage,
  startNewAskThread, usedExplicitProvider,
} from "./chat.js";
import { escapeHtml, showSnackbar } from "./dom-utils.js";
import { projectParam, state, t } from "./state.js";
import { showError } from "./ui.js";

const workspaceChat = {
  isThinking: false,
  isSending: false,
  surface: "split",
  askWidth: 0,
  _streamEl: null,

  init() {
    this.surface = this.loadSurface();
    this.bindEvents();
    this.applySurface(this.surface, { persist: false });
    this.syncProviders();
    this.syncControlsFromAsk();
    this.refreshThread();
    this.syncFileChip();
  },

  loadSurface() {
    const saved = String(localStorage.getItem("workspace-surface-mode") || "").trim();
    if (["editor", "ask", "split"].includes(saved)) return saved;
    return window.matchMedia("(max-width: 768px)").matches ? "ask" : "split";
  },

  applySurface(mode, { persist = true } = {}) {
    const next = ["editor", "ask", "split"].includes(mode) ? mode : "split";
    this.surface = next;
    const layout = document.getElementById("workspace-layout");
    if (layout) layout.dataset.surface = next;
    document.querySelectorAll("[data-workspace-surface]").forEach((btn) => {
      const active = btn.dataset.workspaceSurface === next;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    if (persist) localStorage.setItem("workspace-surface-mode", next);
    if (next === "ask" || next === "split") {
      window.setTimeout(() => document.getElementById("workspace-chat-input")?.focus(), 0);
    }
  },

  bindEvents() {
    document.querySelectorAll("[data-workspace-surface]").forEach((btn) => {
      btn.addEventListener("click", () => this.applySurface(btn.dataset.workspaceSurface));
    });

    const chatForm = document.getElementById("workspace-chat-form");
    if (chatForm) {
      chatForm.addEventListener("submit", (e) => {
        e.preventDefault();
        this.sendMessage().catch(showError);
      });
    }

    const chatInput = document.getElementById("workspace-chat-input");
    if (chatInput) {
      chatInput.addEventListener("input", () => this.autoResizeInput(chatInput));
      chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          this.sendMessage().catch(showError);
        }
      });
    }

    const plusBtn = document.getElementById("workspace-chat-plus");
    const menu = document.getElementById("workspace-chat-menu");
    if (plusBtn && menu) {
      plusBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        menu.hidden = !menu.hidden;
      });
      document.addEventListener("click", (e) => {
        if (!e.target.closest(".chat-menu-wrap")) menu.hidden = true;
      });
      menu.addEventListener("click", (e) => {
        const action = e.target.closest("[data-action]")?.dataset.action;
        if (action === "attach") {
          this.openFilePicker();
          menu.hidden = true;
        } else if (action === "council") {
          this.openInAsk("council");
          menu.hidden = true;
        } else if (action === "open-ask") {
          this.openInAsk();
          menu.hidden = true;
        }
      });
    }

    const attachBtn = document.getElementById("workspace-chat-attach");
    if (attachBtn) attachBtn.addEventListener("click", () => this.openFilePicker());

    const fileInput = document.getElementById("workspace-chat-file-input");
    if (fileInput) fileInput.addEventListener("change", (e) => this.handleFileSelect(e).catch(showError));

    const clearBtn = document.getElementById("chat-clear-btn");
    if (clearBtn) clearBtn.addEventListener("click", () => this.clearChat());

    const expandBtn = document.getElementById("workspace-open-ask");
    if (expandBtn) expandBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      this.openInAsk();
    });

    const fileChip = document.getElementById("workspace-ask-focus-file");
    if (fileChip) {
      fileChip.addEventListener("click", () => {
        this.applySurface("editor");
        document.getElementById("file-editor")?.focus();
      });
    }

    const provider = document.getElementById("workspace-chat-provider");
    if (provider) provider.addEventListener("change", () => this.syncControlsToAsk());

    const remember = document.getElementById("workspace-chat-remember");
    if (remember) remember.addEventListener("change", () => this.syncControlsToAsk());

    const approve = document.getElementById("workspace-chat-approve-cli");
    if (approve) approve.addEventListener("change", () => this.syncControlsToAsk());

    this.bindAskResizer();

    document.addEventListener("keydown", (e) => {
      const meta = e.metaKey || e.ctrlKey;
      if (!meta || !e.shiftKey || String(e.key || "").toLowerCase() !== "a") return;
      if (e.target && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      e.preventDefault();
      const order = ["editor", "split", "ask"];
      const idx = order.indexOf(this.surface);
      this.applySurface(order[(idx + 1) % order.length]);
    });
  },

  bindAskResizer() {
    const resizer = document.getElementById("workspace-ask-resizer");
    const panel = document.getElementById("workspace-chat-panel");
    const stage = document.querySelector(".workspace-stage-body");
    if (!resizer || !panel || !stage) return;
    const saved = Number(localStorage.getItem("workspace-ask-width") || 0);
    if (saved >= 280) {
      panel.style.width = `${saved}px`;
      this.askWidth = saved;
    }
    let dragging = false;
    const onMove = (event) => {
      if (!dragging) return;
      const rect = stage.getBoundingClientRect();
      const width = Math.round(rect.right - event.clientX);
      const clamped = Math.max(280, Math.min(Math.floor(rect.width * 0.7), width));
      panel.style.width = `${clamped}px`;
      this.askWidth = clamped;
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove("is-dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      if (this.askWidth) localStorage.setItem("workspace-ask-width", String(this.askWidth));
    };
    resizer.addEventListener("mousedown", (event) => {
      if (this.surface !== "split") return;
      event.preventDefault();
      dragging = true;
      resizer.classList.add("is-dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    });
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  },

  syncFileChip() {
    const chip = document.getElementById("workspace-ask-focus-file");
    if (!chip) return;
    const path = String(state.selectedFile || "").trim();
    if (!path) {
      chip.hidden = true;
      chip.textContent = "";
      return;
    }
    const name = path.split(/[\\/]/).pop() || path;
    chip.hidden = false;
    chip.textContent = name;
    chip.title = path;
  },

  autoResizeInput(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
  },

  syncProviders() {
    const providerSelect = document.getElementById("workspace-chat-provider");
    const mainProviderSelect = document.getElementById("chat-provider");
    if (providerSelect && mainProviderSelect && mainProviderSelect.options.length) {
      const current = providerSelect.value || mainProviderSelect.value || "auto";
      providerSelect.innerHTML = mainProviderSelect.innerHTML;
      providerSelect.value = current;
      if (!providerSelect.value) providerSelect.value = "auto";
    }
  },

  syncControlsFromAsk() {
    const pairs = [
      ["#chat-provider", "#workspace-chat-provider"],
      ["#chat-remember", "#workspace-chat-remember"],
      ["#chat-approve-cli", "#workspace-chat-approve-cli"],
    ];
    for (const [fromSel, toSel] of pairs) {
      const from = document.querySelector(fromSel);
      const to = document.querySelector(toSel);
      if (!from || !to) continue;
      if (to.tagName === "SELECT") to.value = from.value;
      else to.checked = from.checked;
    }
  },

  syncControlsToAsk() {
    const pairs = [
      ["#workspace-chat-provider", "#chat-provider"],
      ["#workspace-chat-remember", "#chat-remember"],
      ["#workspace-chat-approve-cli", "#chat-approve-cli"],
    ];
    for (const [fromSel, toSel] of pairs) {
      const from = document.querySelector(fromSel);
      const to = document.querySelector(toSel);
      if (!from || !to) continue;
      if (from.tagName === "SELECT") to.value = from.value;
      else to.checked = from.checked;
    }
    if (typeof syncAskMode === "function") syncAskMode();
  },

  setStatus(text, tone = "") {
    const statusEl = document.getElementById("chat-status");
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = `chat-status-indicator ${tone}`.trim();
  },

  showEmptyState() {
    if (this.isSending) return;
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    chatThread.innerHTML = `
      <div class="chat-empty-state">
        <div class="chat-empty-icon">💬</div>
        <div class="chat-empty-text">${escapeHtml(t("workspace.ask.empty"))}</div>
        <div class="chat-empty-hint">${escapeHtml(t("workspace.ask.emptyHint"))}</div>
      </div>`;
  },

  renderFromChat(chat) {
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    const messages = (chat && chat.messages) || [];
    if (!messages.length) {
      this.showEmptyState();
      return;
    }
    const frag = document.createDocumentFragment();
    messages.forEach((message, index) => {
      frag.appendChild(this.buildThreadMessage(message, chat.id, index));
    });
    chatThread.replaceChildren(frag);
    this._streamEl = null;
    this._streamWrap = null;
    chatThread.scrollTop = chatThread.scrollHeight;
  },

  buildThreadMessage(message, chatId, index) {
    const role = message.role || "assistant";
    if (role === "user") {
      return this.buildMessageElement("user", message.text || "", false, null);
    }

    const wrap = document.createElement("div");
    wrap.className = "workspace-stream-block";

    const persona = document.createElement("div");
    persona.className = "msg-persona";
    wrap.appendChild(persona);

    const bubble = document.createElement("div");
    bubble.className = "message assistant workspace-ask-message";
    const textNode = document.createElement("div");
    textNode.className = "message-text";
    bubble.appendChild(textNode);
    bubble._persona = persona;
    wrap.appendChild(bubble);

    if (isProviderError(message.provider)) {
      setBubbleProvider(bubble, message.provider);
      renderProviderErrorBubble(bubble, message.text || "", message.provider);
    } else {
      setBubbleProvider(bubble, message.provider);
      renderAssistantRichContent(bubble, message.raw_text || message.text || "", message.structured || null);
      if (Array.isArray(message.tool_trace) && message.tool_trace.length) {
        finalizeAgentActivity(bubble, message.tool_trace);
      }
      setBubbleUsage(bubble, message.usage);
    }
    return wrap;
  },

  async refreshThread(options = {}) {
    const allowEmpty = options.allowEmpty !== false;
    try {
      const payload = await api(`/api/chats?project_id=${projectParam()}&limit=80`);
      const chats = payload.chats || [];
      const summary = (state.chatId && chats.find(item => item.id === state.chatId)) || null;
      if (summary) {
        const chat = await fetchChat(summary.id);
        state.chatId = chat.id;
        this.renderFromChat(chat);
        return;
      }
      if (allowEmpty && !this.isSending) this.showEmptyState();
    } catch (error) {
      if (allowEmpty && !this.isSending) this.showEmptyState();
    }
  },

  beginStream(userMessage) {
    this.isSending = true;
    this.setStatus("Working…", "thinking");
    this.addMessage("user", userMessage);
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    chatThread.querySelector(".chat-empty-state")?.remove();

    const wrap = document.createElement("div");
    wrap.className = "workspace-stream-block";

    const persona = document.createElement("div");
    persona.className = "msg-persona";
    wrap.appendChild(persona);

    const bubble = document.createElement("div");
    bubble.className = "message assistant streaming workspace-ask-message";
    const textNode = document.createElement("div");
    textNode.className = "message-text";
    bubble.appendChild(textNode);
    bubble._persona = persona;
    wrap.appendChild(bubble);

    chatThread.appendChild(wrap);
    this._streamEl = bubble;
    this._streamWrap = wrap;
    beginAgentActivity(bubble);
    chatThread.scrollTop = chatThread.scrollHeight;
  },

  onStreamStart(event) {
    if (!this._streamEl) return;
    if (event?.provider) {
      setBubbleProvider(this._streamEl, event.provider);
      setAgentActivityModel(this._streamEl, event.provider);
      this.setStatus(providerLabel(event.provider) || "Working…", "thinking");
    }
    beginAgentActivity(this._streamEl);
  },

  onStreamDelta(text) {
    if (!this._streamEl) return;
    const node = getMessageTextElement(this._streamEl);
    if (!node) return;
    node.textContent += text || "";
    const chatThread = document.getElementById("workspace-chat-thread");
    if (chatThread) chatThread.scrollTop = chatThread.scrollHeight;
  },

  onStreamProgress(event) {
    if (!this._streamEl || !event) return;
    updateAgentActivity(this._streamEl, event);
    if (event.provider) {
      this.setStatus(providerLabel(event.provider) || event.status || "Working…", "thinking");
    } else if (event.status) {
      this.setStatus(String(event.status).slice(0, 48), "thinking");
    }
    const chatThread = document.getElementById("workspace-chat-thread");
    if (chatThread) chatThread.scrollTop = chatThread.scrollHeight;
  },

  onStreamDone(event) {
    this.isSending = false;
    this.setStatus("Ready");
    if (this._streamEl) {
      this._streamEl.classList.remove("streaming");
      if (event?.provider) setBubbleProvider(this._streamEl, event.provider);
      if (isProviderError(event?.provider)) {
        renderProviderErrorBubble(
          this._streamEl,
          event.response || getMessageTextElement(this._streamEl)?.textContent || "",
          event.provider,
        );
      } else {
        const finalText = event?.response || getMessageTextElement(this._streamEl)?.textContent || "";
        const withStderr = event?.raw?.stderr ? `${finalText}\n\nstderr: ${event.raw.stderr}` : finalText;
        renderAssistantRichContent(this._streamEl, withStderr, event?.structured || null);
        finalizeAgentActivity(this._streamEl, event?.tool_trace);
        if (event?.usage) setBubbleUsage(this._streamEl, event.usage);
      }
    }
    // Keep the live bubble with activity; only replace if we need full history sync.
    if (event?.chat?.messages?.length) {
      this.renderFromChat(event.chat);
    }
    this._streamEl = null;
    this._streamWrap = null;
  },

  onStreamError(error) {
    this.isSending = false;
    if (this._streamEl) {
      this._streamEl.classList.remove("streaming");
      stopAgentActivityTimer(this._streamEl.querySelector(".agent-activity"));
      renderProviderErrorBubble(this._streamEl, error || "stream error", { status: "error" });
    }
    this.setStatus("Error", "error");
    window.setTimeout(() => this.setStatus("Ready"), 3000);
  },

  openInAsk(mode) {
    this.syncControlsToAsk();
    const input = document.getElementById("workspace-chat-input");
    const askInput = document.getElementById("chat-message");
    if (input && askInput && input.value.trim()) {
      askInput.value = input.value;
      autoGrowChatInput();
    }
    if (mode) syncAskMode(mode);
    switchView("chat");
  },

  async sendMessage() {
    const input = document.getElementById("workspace-chat-input");
    if (!input) return;
    const message = input.value.trim();
    if (!message || this.isSending) return;

    const previousSurface = this.surface;
    if (this.surface === "editor") this.applySurface("split");

    this.syncControlsToAsk();
    const askInput = document.getElementById("chat-message");
    if (askInput) {
      askInput.value = message;
      autoGrowChatInput();
    }
    input.value = "";
    this.autoResizeInput(input);

    try {
      await sendChatMessage(new Event("submit"), { workspaceMirror: true });
      if (previousSurface === "ask") this.applySurface("ask", { persist: true });
      else if (this.surface === "editor") this.applySurface("split", { persist: true });
    } catch (error) {
      this.onStreamError(error.message || String(error));
      throw error;
    }
  },

  buildMessageElement(role, content, isError = false, provider = null) {
    const messageEl = document.createElement("div");
    messageEl.className = `chat-message-compact ${role}`;
    const avatar = role === "user" ? "👤" : ((providerLabel(provider) || "AI").trim().charAt(0) || "A").toUpperCase();
    const time = new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
    messageEl.innerHTML = `
      <div class="chat-avatar-compact">${this.escapeHtml(avatar)}</div>
      <div class="chat-message-content-compact">
        <div class="chat-bubble-compact ${isError ? "error" : ""}">${this.formatMessage(content)}</div>
        <div class="chat-message-time">${time}</div>
      </div>`;
    return messageEl;
  },

  addMessage(role, content, isError = false, provider = null, store = true) {
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    chatThread.querySelector(".chat-empty-state")?.remove();
    chatThread.appendChild(this.buildMessageElement(role, content, isError, provider));
    if (isError && role === "assistant") this.addProviderErrorActions(content);
    chatThread.scrollTop = chatThread.scrollHeight;
  },

  addProviderErrorActions(text) {
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    const cta = providerErrorAction(text);
    const wrap = document.createElement("div");
    wrap.className = "provider-error-actions provider-error-actions-compact";
    if (usedExplicitProvider()) {
      const retryButton = document.createElement("button");
      retryButton.type = "button";
      retryButton.className = "btn btn-primary btn-sm";
      retryButton.textContent = t("provider.retryAuto");
      retryButton.addEventListener("click", () => handleProviderErrorAction("retry-auto"));
      wrap.appendChild(retryButton);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-secondary btn-sm";
    button.textContent = cta.label;
    button.addEventListener("click", () => {
      if (cta.action === "cli") {
        const checkbox = document.getElementById("workspace-chat-approve-cli");
        if (checkbox) checkbox.checked = true;
        this.syncControlsToAsk();
        showSnackbar("CLI runs enabled. Send your message again.", "info");
        return;
      }
      if (cta.action === "retry-auto") {
        handleProviderErrorAction("retry-auto");
        return;
      }
      switchView("providers");
    });
    wrap.appendChild(button);
    chatThread.appendChild(wrap);
  },

  formatMessage(content) {
    return this.escapeHtml(content || "").replace(/\n/g, "<br>");
  },

  escapeHtml(text) {
    // Delegate to the shared helper instead of allocating a DOM node per call.
    return escapeHtml(text);
  },

  setThinking(thinking) {
    this.isThinking = thinking;
    const chatThread = document.getElementById("workspace-chat-thread");
    if (!chatThread) return;
    chatThread.querySelector(".chat-thinking")?.closest(".chat-message-compact")?.remove();
    if (!thinking) {
      this.setStatus("Ready");
      return;
    }
    const thinkingEl = document.createElement("div");
    thinkingEl.className = "chat-message-compact assistant";
    thinkingEl.innerHTML = `
      <div class="chat-avatar-compact">AI</div>
      <div class="chat-message-content-compact">
        <div class="chat-thinking">
          <div class="chat-thinking-dot"></div>
          <div class="chat-thinking-dot"></div>
          <div class="chat-thinking-dot"></div>
        </div>
      </div>`;
    chatThread.appendChild(thinkingEl);
    chatThread.scrollTop = chatThread.scrollHeight;
  },

  openFilePicker() {
    document.getElementById("workspace-chat-file-input")?.click();
  },

  async handleFileSelect(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;
    try {
      await handleFileSelect(files);
      showSnackbar(`${files.length} file(s) attached in Ask composer.`, "info");
      this.applySurface(this.surface === "editor" ? "split" : this.surface);
      this.openInAsk();
    } catch (error) {
      showError(error);
    }
  },

  clearChat() {
    if (!confirm("Start a new Ask dialog? Current thread stays in Dialogs history.")) return;
    startNewAskThread();
  },
};

export { workspaceChat };
