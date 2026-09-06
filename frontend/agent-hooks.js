// Agent hooks setup panel: register our capture hooks in Cursor / Claude Code / Codex. ES module.
import { api } from "./api-client.js";
import { showAppConfirm } from "./app-dialog.js";
import { escapeHtml, showSnackbar } from "./dom-utils.js";
import { t } from "./state.js";

const CLIENT_LABELS = { cursor: "Cursor", claude: "Claude Code", codex: "Codex CLI" };
let hooksStatus = null;

function selectedScope() {
  const toggle = document.querySelector("#hooks-scope-project");
  return toggle && toggle.checked ? "project" : "user";
}
function rowsForScope(status, scope) {
  return (status && Array.isArray(status.clients) ? status.clients : []).filter(row => row.scope === scope);
}
function setActionsDisabled(disabled) {
  document.querySelectorAll("#hooks-view button[data-hook-action], #hooks-install-all, #hooks-uninstall-all").forEach(button => {
    button.disabled = disabled;
  });
}
function renderHooksSummary(status, rows) {
  const message = document.querySelector("#hooks-status-message");
  const badge = document.querySelector("#hooks-status-badge");
  const summary = document.querySelector("#hooks-summary");
  const installed = rows.filter(row => row.installed).length;

  if (message) {
    if (!status.entry_script_exists) message.textContent = t("hooks.status.unavailable");
    else if (!installed) message.textContent = t("hooks.status.idle");
    else if (installed === rows.length) message.textContent = t("hooks.status.all");
    else message.textContent = t("hooks.status.some").replace("{installed}", String(installed)).replace("{total}", String(rows.length));
  }
  if (badge) {
    const tone = installed === 0 ? "idle" : (installed === rows.length ? "ok" : "warn");
    badge.dataset.tone = tone;
    badge.textContent = t(`hooks.readiness.${tone === "ok" ? "ready" : (tone === "warn" ? "partial" : "idle")}`);
  }
  if (summary) {
    const reachable = status.server && status.server.reachable;
    summary.className = "provider-test";
    summary.textContent = reachable
      ? t("hooks.server.reachable").replace("{url}", status.server.url || "")
      : t("hooks.server.offline");
  }
}
function buildHookCard(row) {
  const label = CLIENT_LABELS[row.client] || row.client;
  const statusClass = row.installed ? "ready" : "planned";
  const statusLabel = row.installed ? t("hooks.state.installed") : t("hooks.state.missing");
  const reach = row.injects ? t("hooks.reach.captureInject") : t("hooks.reach.captureOnly");
  const events = (row.events || []).join(", ");
  const trust = row.client === "codex" && row.installed ? `<p class="provider-hint">${escapeHtml(t("hooks.codexTrust"))}</p>` : "";
  const action = row.installed
    ? `<button type="button" class="btn-secondary" data-hook-action="uninstall" data-hook-client="${escapeHtml(row.client)}">${escapeHtml(t("hooks.action.remove"))}</button>`
    : `<button type="button" class="btn-primary" data-hook-action="install" data-hook-client="${escapeHtml(row.client)}">${escapeHtml(t("hooks.action.install"))}</button>`;
  return `<div class="provider-head"><div><strong>${escapeHtml(label)}</strong><p>${escapeHtml(reach)}</p></div><span class="provider-status ${statusClass}">${escapeHtml(statusLabel)}</span></div>`
    + `<p class="provider-hint">${escapeHtml(events)}</p>`
    + `<p class="provider-last-check">${escapeHtml(row.config)}</p>`
    + trust
    + `<div class="provider-card-actions">${action}</div>`;
}
function renderAgentHooks(status) {
  hooksStatus = status;
  const list = document.querySelector("#hooks-client-list");
  const rows = rowsForScope(status, selectedScope());
  renderHooksSummary(status, rows);
  if (!list) return;
  list.innerHTML = "";
  for (const row of rows) {
    const card = document.createElement("article");
    card.className = `provider provider-card-compact ${row.installed ? "ready" : "planned"}`;
    card.innerHTML = buildHookCard(row);
    list.appendChild(card);
  }
  list.querySelectorAll("[data-hook-action]").forEach(button => button.addEventListener("click", () => {
    configureAgentHooks([button.dataset.hookClient], button.dataset.hookAction === "uninstall").catch(error => {
      showSnackbar(error && error.message ? error.message : String(error), "error");
    });
  }));
  setActionsDisabled(!status.entry_script_exists);
}
async function loadAgentHooks() {
  renderAgentHooks(await api("/api/hooks"));
}
async function configureAgentHooks(clients, remove) {
  const scope = selectedScope();
  const targets = rowsForScope(hooksStatus, scope).filter(row => clients.includes(row.client));
  const files = targets.map(row => row.config).join("\n");
  if (files) {
    const ok = await showAppConfirm({
      title: t(remove ? "hooks.action.remove" : "hooks.action.install"),
      message: t(remove ? "hooks.confirmRemove" : "hooks.confirmInstall"),
      detail: files,
      confirmLabel: t(remove ? "hooks.action.remove" : "hooks.action.install"),
      danger: Boolean(remove),
    });
    if (!ok) return;
  }

  setActionsDisabled(true);
  try {
    const payload = await api(`/api/hooks/${remove ? "uninstall" : "install"}`, {
      method: "POST",
      body: JSON.stringify({ clients, scope }),
    });
    const failed = (payload.results || []).filter(result => result.error);
    renderAgentHooks(payload.status);
    if (failed.length) showSnackbar(failed[0].error, "error");
    else showSnackbar(t(remove ? "hooks.removed" : "hooks.installed"), "success");
  } finally {
    setActionsDisabled(Boolean(hooksStatus && !hooksStatus.entry_script_exists));
  }
}
function rerenderAgentHooksScope() {
  if (hooksStatus) renderAgentHooks(hooksStatus);
}

export { configureAgentHooks, loadAgentHooks, rerenderAgentHooksScope };
