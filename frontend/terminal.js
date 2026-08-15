// Terminal run/open UI helpers. ES module.
import { api } from "./api-client.js";
import { switchView } from "./ask-ui.js";
import { escapeHtml, setElementValue } from "./dom-utils.js";
import { state } from "./state.js";

function terminalPayload(commandOverride = "") {
  const allowDestructive = Boolean(document.querySelector("#terminal-allow-destructive")?.checked);
  return {
    project_id: state.projectId,
    command: commandOverride || document.querySelector("#terminal-command")?.value || "",
    shell: document.querySelector("#terminal-shell")?.value || "auto",
    timeout_seconds: Number(document.querySelector("#terminal-timeout")?.value || 20),
    allow_destructive: allowDestructive,
    // Backend requires this exact confirm string in addition to the checkbox.
    destructive_confirm: allowDestructive ? "I_UNDERSTAND_DESTRUCTIVE" : "",
  };
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
  const result = await api("/api/terminal/run", { method: "POST", body: JSON.stringify(payload) });
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
