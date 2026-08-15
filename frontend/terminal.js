// Terminal run/open UI helpers. ES module.
import { api } from "./api-client.js";
import { switchView } from "./ask-ui.js";
import { escapeHtml, setElementValue, trapFocus } from "./dom-utils.js";
import { state } from "./state.js";

// The backend blocks risky commands unless the request carries a confirmation
// phrase. That phrase is deliberately not a constant here: it arrives on the
// block response and is re-sent only after the user types it back, so nothing
// in the UI can arm a destructive run on the user's behalf.
function terminalPayload(commandOverride = "", confirmPhrase = "") {
  return {
    project_id: state.projectId,
    command: commandOverride || document.querySelector("#terminal-command")?.value || "",
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
  document.body.style.overflow = "";
  if (releaseConfirmFocus) { releaseConfirmFocus(); releaseConfirmFocus = null; }
}
// Resolves with the phrase the user typed, or "" if they backed out.
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
  document.body.style.overflow = "hidden";
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
function renderTerminalResult(payload) {
  const output = document.querySelector("#terminal-output");
  const status = document.querySelector("#terminal-status");
  if (!output || !status) return;
  const stdout = payload.stdout || "";
  const stderr = payload.stderr || "";
  const parts = [
    `$ ${payload.command || ""}`,
    `cwd: ${payload.root || ""}`,
    `status: ${payload.status || ""}${payload.returncode !== null && payload.returncode !== undefined ? ` / exit ${payload.returncode}` : ""}${payload.duration_ms ? ` / ${payload.duration_ms}ms` : ""}`,
    stdout ? `\nstdout:\n${stdout}` : "",
    stderr ? `\nstderr:\n${stderr}` : "",
  ].filter(Boolean);
  output.textContent = parts.join("\n");
  status.className = `provider-test ${payload.status === "ok" || payload.status === "opened" ? "ok" : payload.status === "blocked" || payload.status === "error" ? "error" : ""}`;
  status.textContent = payload.status === "blocked" ? "blocked by terminal safety policy" : payload.status || "";
}
function renderTerminalHistory() {
  const list = document.querySelector("#terminal-history");
  if (!list) return;
  list.innerHTML = state.terminalHistory.length ? state.terminalHistory.map((item, index) => `<article class="result"><div class="row"><strong>${escapeHtml(item.command)}</strong><button data-terminal-rerun="${index}" type="button">Use</button></div><span class="badge">${escapeHtml(item.status || "")}</span><span class="badge">${escapeHtml(item.shell || "auto")}</span></article>`).join("") : '<article class="result"><strong>No commands yet</strong></article>';
  list.querySelectorAll("[data-terminal-rerun]").forEach(button => button.addEventListener("click", () => {
    const item = state.terminalHistory[Number(button.dataset.terminalRerun)];
    if (item) setElementValue("#terminal-command", item.command);
  }));
}
async function executeTerminalCommand(commandOverride = "") {
  const status = document.querySelector("#terminal-status");
  const payload = terminalPayload(commandOverride);
  if (!payload.command.trim()) throw new Error("Terminal command is required.");
  const commandInput = document.querySelector("#terminal-command");
  if (commandInput) commandInput.value = payload.command;
  if (status) { status.className = "provider-test"; status.textContent = "running..."; }
  let result = await api("/api/terminal/run", { method: "POST", body: JSON.stringify(payload) });
  if (result.status === "blocked" && result.requires_confirm) {
    const phrase = await requestDestructiveConfirmation(payload.command, result);
    if (phrase) {
      if (status) { status.className = "provider-test"; status.textContent = "running..."; }
      const confirmed = terminalPayload(payload.command, phrase);
      result = await api("/api/terminal/run", { method: "POST", body: JSON.stringify(confirmed) });
    }
  }
  state.terminalHistory.unshift({ command: payload.command, shell: result.shell || payload.shell, status: result.status });
  state.terminalHistory = state.terminalHistory.slice(0, 20);
  renderTerminalResult(result);
  renderTerminalHistory();
}
async function runTerminalCommand(event) {
  if (event) event.preventDefault();
  await executeTerminalCommand();
}
async function runInstallCommandInTerminal(command) {
  if (!command) return;
  switchView("terminal");
  await executeTerminalCommand(command);
}
async function openExternalTerminal() {
  const status = document.querySelector("#terminal-status");
  if (status) { status.className = "provider-test"; status.textContent = "opening..."; }
  const payload = await api("/api/terminal/open", { method: "POST", body: JSON.stringify({ project_id: state.projectId }) });
  renderTerminalResult({ ...payload, command: (payload.command || []).join(" "), stdout: payload.message || "", stderr: "" });
}

export { openExternalTerminal, renderTerminalHistory, runInstallCommandInTerminal, runTerminalCommand };
