// Terminal run/open UI: overlay shell, not a Setup page.
import { api } from "./api-client.js";
import { escapeHtml, on, setElementValue, showSnackbar, trapFocus } from "./dom-utils.js";
import { state, t } from "./state.js";
import { showError } from "./ui.js";

const HISTORY_LIMIT = 20;
const ASK_PROMPT_STDOUT_CAP = 6000;
const ASK_PROMPT_STDERR_CAP = 8000;

let releaseTerminalFocus = null;
let historyCursor = -1;
let historyDraft = "";
let lastRun = null;
let runInFlight = false;
let bound = false;

function terminalModal() {
  return document.querySelector("#terminal-modal");
}

function confirmModalOpen() {
  const modal = document.querySelector("#terminal-confirm-modal");
  return Boolean(modal && !modal.hasAttribute("hidden"));
}

function isTerminalOpen() {
  const modal = terminalModal();
  return Boolean(modal && !modal.hasAttribute("hidden"));
}

function commandInput() {
  return document.querySelector("#terminal-command");
}

function clipText(value, cap) {
  const text = String(value || "");
  if (text.length <= cap) return text;
  return `…(truncated)\n${text.slice(-cap)}`;
}

function runLooksFailed(run) {
  if (!run) return false;
  const status = String(run.status || "");
  if (status === "ok" || status === "opened") return false;
  return Boolean(status);
}

function syncTerminalTrigger() {
  const btn = document.querySelector("#open-terminal-modal");
  if (!btn) return;
  btn.classList.toggle("is-running", runInFlight);
  btn.classList.toggle("is-open", isTerminalOpen());
  btn.setAttribute("aria-pressed", isTerminalOpen() ? "true" : "false");
  const title = runInFlight ? t("terminal.running") : t("terminal.openTitle");
  btn.title = title;
  btn.setAttribute("aria-label", title);
}

function syncAskAgentButton() {
  const btn = document.querySelector("#terminal-ask-agent");
  if (!btn) return;
  const ready = Boolean(lastRun && String(lastRun.command || "").trim()) && !runInFlight;
  btn.disabled = !ready;
  btn.classList.toggle("is-failed", ready && runLooksFailed(lastRun));
}

function resetHistoryCursor() {
  historyCursor = -1;
  historyDraft = "";
}

function fillCommand(command) {
  resetHistoryCursor();
  setElementValue("#terminal-command", command);
  const input = commandInput();
  if (input) {
    input.focus();
    const end = input.value.length;
    input.setSelectionRange(end, end);
  }
}

function walkHistory(delta) {
  const items = state.terminalHistory;
  const input = commandInput();
  if (!input || !items.length) return;
  if (historyCursor < 0) historyDraft = input.value;
  const next = historyCursor + delta;
  if (next < 0) {
    historyCursor = -1;
    input.value = historyDraft;
    return;
  }
  historyCursor = Math.min(next, items.length - 1);
  input.value = items[historyCursor].command || "";
  const end = input.value.length;
  input.setSelectionRange(end, end);
}

function pushHistory(entry) {
  const command = String(entry.command || "").trim();
  if (!command) return;
  const prev = state.terminalHistory[0];
  if (prev && prev.command === command) {
    state.terminalHistory[0] = { ...prev, ...entry, command };
  } else {
    state.terminalHistory.unshift({ ...entry, command });
    state.terminalHistory = state.terminalHistory.slice(0, HISTORY_LIMIT);
  }
  resetHistoryCursor();
}

function renderTerminalHistory() {
  const list = document.querySelector("#terminal-history");
  if (!list) return;
  if (!state.terminalHistory.length) {
    list.innerHTML = `<p class="terminal-history-empty">${escapeHtml(t("terminal.historyEmpty"))}</p>`;
    return;
  }
  list.innerHTML = state.terminalHistory.map((item, index) => {
    const failed = runLooksFailed(item);
    const tone = item.status === "ok" || item.status === "opened" ? "ok" : failed ? "error" : "";
    return `<button type="button" class="terminal-history-item${tone ? ` is-${tone}` : ""}" data-terminal-history="${index}" title="${escapeHtml(item.command)}"><span class="terminal-history-cmd">${escapeHtml(item.command)}</span><span class="terminal-history-dot" aria-hidden="true"></span></button>`;
  }).join("");
  list.querySelectorAll("[data-terminal-history]").forEach(button => {
    button.addEventListener("click", () => {
      const item = state.terminalHistory[Number(button.dataset.terminalHistory)];
      if (item) fillCommand(item.command);
    });
  });
}

function renderTerminalRunning(command) {
  const output = document.querySelector("#terminal-output");
  const status = document.querySelector("#terminal-status");
  if (status) {
    status.hidden = false;
    status.className = "terminal-status is-running";
    status.textContent = t("terminal.running");
  }
  if (output) {
    output.textContent = [`$ ${command}`, "", t("terminal.waiting")].join("\n");
  }
  syncAskAgentButton();
  syncTerminalTrigger();
}

function renderTerminalResult(payload) {
  const output = document.querySelector("#terminal-output");
  const status = document.querySelector("#terminal-status");
  if (!output || !status) return;
  const stdout = payload.stdout || "";
  const stderr = payload.stderr || "";
  const failed = runLooksFailed(payload);
  const parts = [
    `$ ${payload.command || ""}`,
    payload.root ? `cwd: ${payload.root}` : "",
    `status: ${payload.status || ""}${payload.returncode !== null && payload.returncode !== undefined ? ` / exit ${payload.returncode}` : ""}${payload.duration_ms ? ` / ${payload.duration_ms}ms` : ""}`,
    stdout ? `\n${stdout}` : "",
    stderr ? `\n${stderr}` : "",
  ].filter(Boolean);
  output.textContent = parts.join("\n");
  output.scrollTop = output.scrollHeight;
  status.hidden = false;
  status.className = `terminal-status${payload.status === "ok" || payload.status === "opened" ? " is-ok" : failed ? " is-error" : ""}`;
  status.textContent = payload.status === "blocked"
    ? t("terminal.blocked")
    : payload.status === "timeout"
      ? t("terminal.timeout")
      : payload.status || "";
  lastRun = {
    command: payload.command || "",
    root: payload.root || "",
    status: payload.status || "",
    returncode: payload.returncode,
    duration_ms: payload.duration_ms,
    stdout,
    stderr,
  };
  syncAskAgentButton();
  syncTerminalTrigger();
}

function buildAskPrompt(run) {
  const failed = runLooksFailed(run);
  const lead = failed
    ? "A command in this project failed. Diagnose the root cause from the repository, then propose a minimal fix."
    : "A command ran in this project. Review the output, explain what happened, and suggest a next step if something looks wrong.";
  const stdout = clipText(run.stdout, ASK_PROMPT_STDOUT_CAP);
  const stderr = clipText(run.stderr, ASK_PROMPT_STDERR_CAP);
  const exit = run.returncode !== null && run.returncode !== undefined ? ` / exit ${run.returncode}` : "";
  const dur = run.duration_ms ? ` / ${run.duration_ms}ms` : "";
  return [
    lead,
    "Do not ask for permission in chat — read the files you need. If you can patch it, say what you would change and wait before writing unless writes are already approved.",
    "",
    `Command: ${run.command}`,
    run.root ? `cwd: ${run.root}` : "",
    `status: ${run.status || "unknown"}${exit}${dur}`,
    stdout ? `\nstdout:\n${stdout}` : "",
    stderr ? `\nstderr:\n${stderr}` : "",
  ].filter(Boolean).join("\n");
}

function closeTerminalModal() {
  const modal = terminalModal();
  if (!modal || modal.hasAttribute("hidden")) return;
  modal.setAttribute("hidden", "");
  document.body.style.overflow = "";
  if (releaseTerminalFocus) {
    releaseTerminalFocus();
    releaseTerminalFocus = null;
  }
  syncTerminalTrigger();
}

function openTerminalModal(options = {}) {
  const modal = terminalModal();
  if (!modal) return;
  const command = String(options.command || "").trim();
  const wasOpen = isTerminalOpen();
  modal.removeAttribute("hidden");
  document.body.style.overflow = "hidden";
  renderTerminalHistory();
  syncAskAgentButton();
  if (!wasOpen) {
    if (releaseTerminalFocus) releaseTerminalFocus();
    releaseTerminalFocus = trapFocus(modal);
  }
  if (command) fillCommand(command);
  else commandInput()?.focus();
  syncTerminalTrigger();
  if (options.autoRun && command) {
    executeTerminalCommand(command).catch(showError);
  }
}

function toggleTerminalModal() {
  if (isTerminalOpen()) closeTerminalModal();
  else openTerminalModal();
}

// The backend blocks risky commands unless the request carries a confirmation
// phrase. That phrase is deliberately not a constant here: it arrives on the
// block response and is re-sent only after the user types it back, so nothing
// in the UI can arm a destructive run on the user's behalf.
function terminalPayload(commandOverride = "", confirmPhrase = "") {
  return {
    project_id: state.projectId,
    command: commandOverride || commandInput()?.value || "",
    shell: document.querySelector("#terminal-shell")?.value || "auto",
    timeout_seconds: Number(document.querySelector("#terminal-timeout")?.value || 20),
    allow_destructive: Boolean(confirmPhrase),
    destructive_confirm: confirmPhrase,
  };
}

let releaseConfirmFocus = null;
function closeTerminalConfirm() {
  const modal = document.querySelector("#terminal-confirm-modal");
  if (modal) modal.setAttribute("hidden", "");
  if (releaseConfirmFocus) { releaseConfirmFocus(); releaseConfirmFocus = null; }
}

function requestDestructiveConfirmation(command, blocked) {
  const modal = document.querySelector("#terminal-confirm-modal");
  const phrase = String(blocked.requires_confirm || "");
  const input = modal?.querySelector("#terminal-confirm-input");
  const runButton = modal?.querySelector("#terminal-confirm-run");
  if (!modal || !input || !runButton || !phrase) return Promise.resolve("");
  modal.querySelector("#terminal-confirm-command").textContent = command;
  modal.querySelector("#terminal-confirm-reason").textContent =
    blocked.risk ? `Blocked by the terminal safety policy: ${blocked.risk}` : (blocked.stderr || "Blocked by the terminal safety policy.");
  modal.querySelector("#terminal-confirm-phrase").textContent = phrase;
  input.value = "";
  runButton.disabled = true;
  modal.removeAttribute("hidden");
  releaseConfirmFocus = trapFocus(modal);
  input.focus();
  return new Promise(resolve => {
    const cancelTargets = Array.from(modal.querySelectorAll("[data-terminal-confirm-cancel]"));
    const settle = value => {
      input.removeEventListener("input", onInput);
      input.removeEventListener("keydown", onEnter);
      runButton.removeEventListener("click", onRun);
      document.removeEventListener("keydown", onEscape);
      cancelTargets.forEach(target => target.removeEventListener("click", onCancel));
      closeTerminalConfirm();
      resolve(value);
    };
    const onInput = () => { runButton.disabled = input.value !== phrase; };
    const onRun = () => { if (input.value === phrase) settle(phrase); };
    const onEnter = event => { if (event.key === "Enter") { event.preventDefault(); onRun(); } };
    const onCancel = () => settle("");
    const onEscape = event => { if (event.key === "Escape") settle(""); };
    input.addEventListener("input", onInput);
    input.addEventListener("keydown", onEnter);
    runButton.addEventListener("click", onRun);
    document.addEventListener("keydown", onEscape);
    cancelTargets.forEach(target => target.addEventListener("click", onCancel));
  });
}

async function executeTerminalCommand(commandOverride = "") {
  const payload = terminalPayload(commandOverride);
  if (!payload.command.trim()) throw new Error(t("terminal.commandRequired"));
  const input = commandInput();
  if (input) input.value = payload.command;
  runInFlight = true;
  renderTerminalRunning(payload.command);
  let result;
  try {
    result = await api("/api/terminal/run", {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: (Number(payload.timeout_seconds) || 20) * 1000 + 8000,
    });
    if (result.status === "blocked" && result.requires_confirm) {
      const phrase = await requestDestructiveConfirmation(payload.command, result);
      if (phrase) {
        renderTerminalRunning(payload.command);
        const confirmed = terminalPayload(payload.command, phrase);
        result = await api("/api/terminal/run", {
          method: "POST",
          body: JSON.stringify(confirmed),
          timeoutMs: (Number(confirmed.timeout_seconds) || 20) * 1000 + 8000,
        });
      }
    }
  } catch (err) {
    result = { command: payload.command, status: "error", stdout: "", stderr: err instanceof Error ? err.message : String(err) };
  } finally {
    runInFlight = false;
  }
  pushHistory({ command: payload.command, shell: result.shell || payload.shell, status: result.status });
  renderTerminalResult(result);
  renderTerminalHistory();
}

async function runTerminalCommand(event) {
  if (event) event.preventDefault();
  await executeTerminalCommand();
}

async function runInstallCommandInTerminal(command) {
  if (!command) return;
  openTerminalModal({ command, autoRun: true });
}

async function openExternalTerminal() {
  const status = document.querySelector("#terminal-status");
  if (status) {
    status.hidden = false;
    status.className = "terminal-status is-running";
    status.textContent = t("terminal.opening");
  }
  const payload = await api("/api/terminal/open", { method: "POST", body: JSON.stringify({ project_id: state.projectId }) });
  renderTerminalResult({ ...payload, command: (payload.command || []).join(" "), stdout: payload.message || "", stderr: "" });
}

async function askAgentFromTerminal() {
  if (!lastRun || !String(lastRun.command || "").trim()) return;
  const prompt = buildAskPrompt(lastRun);
  closeTerminalModal();
  const { startNewAskThread } = await import("./chat.js");
  const { autoGrowChatInput, switchView, syncAskMode } = await import("./ask-ui.js");
  startNewAskThread({ notify: false });
  if (state.askMode === "memory") syncAskMode("quick");
  await switchView("chat", { newThread: true });
  const input = document.querySelector("#chat-message");
  if (input) {
    input.value = prompt;
    autoGrowChatInput();
    input.focus();
  }
  showSnackbar(t("terminal.askReady"), "info");
}

function onGlobalKeydown(event) {
  const meta = event.metaKey || event.ctrlKey;
  if (meta && (event.key === "`" || event.code === "Backquote")) {
    event.preventDefault();
    toggleTerminalModal();
    return;
  }
  if (event.key !== "Escape" || !isTerminalOpen() || confirmModalOpen()) return;
  event.preventDefault();
  closeTerminalModal();
}

function bindTerminalUi() {
  if (bound) return;
  bound = true;
  on("#open-terminal-modal", "click", () => toggleTerminalModal());
  on("#terminal-form", "submit", event => runTerminalCommand(event).catch(showError));
  on("#terminal-open", "click", () => openExternalTerminal().catch(showError));
  on("#terminal-clear", "click", () => {
    state.terminalHistory = [];
    renderTerminalHistory();
  });
  on("#terminal-ask-agent", "click", () => askAgentFromTerminal().catch(showError));
  document.querySelectorAll("[data-close-terminal]").forEach(el => {
    el.addEventListener("click", closeTerminalModal);
  });
  commandInput()?.addEventListener("keydown", event => {
    if (event.key === "ArrowUp") {
      event.preventDefault();
      walkHistory(1);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      walkHistory(-1);
    }
  });
  document.addEventListener("keydown", onGlobalKeydown);
  syncAskAgentButton();
  syncTerminalTrigger();
}

export {
  bindTerminalUi,
  closeTerminalModal,
  openExternalTerminal,
  openTerminalModal,
  renderTerminalHistory,
  runInstallCommandInTerminal,
  runTerminalCommand,
};
