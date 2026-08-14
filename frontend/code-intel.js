// Code intelligence UI (languages / LSP / symbols / hover / diagnostics / references). Classic script loaded before app.js; shares global scope.
function renderCodeLanguages(payload) {
  const languages = payload.languages || [];
  const servers = state.codeServersCache?.servers || [];
  renderCodeStatus(languages, servers);
  renderCodeLanguageSupport(languages, servers);
}
function codeLanguageTone(item) {
  if (item.ready) return "ok";
  if (item.fallback) return "fallback";
  if (item.status === "planned") return "planned";
  return "warn";
}
function codeLanguageStatusLabel(item) {
  if (item.ready) return t("code.lang.ready");
  if (item.fallback) return t("code.lang.fallback");
  if (item.status === "planned") return t("code.lang.planned");
  return t("code.lang.needsInstall");
}
function renderCodeStatus(languages, servers) {
  const message = document.querySelector("#code-status-message");
  const badge = document.querySelector("#code-status-badge");
  const pills = document.querySelector("#code-status-pills");
  const connectBtn = document.querySelector("#code-connect-folder");
  const analyzeBtn = document.querySelector("#code-analyze-project");
  const noProject = needsProjectOnboarding() || state.projectFilesStatus === "no_root" || state.projectFilesStatus === "missing_root";

  if (connectBtn) connectBtn.hidden = !noProject;
  if (analyzeBtn) analyzeBtn.hidden = noProject;

  if (!languages.length) {
    if (message) message.textContent = noProject ? t("code.status.noProject") : t("code.status.noLanguages");
    if (badge) {
      badge.textContent = t("code.readiness.idle");
      badge.dataset.tone = "idle";
    }
    if (pills) pills.innerHTML = "";
    return;
  }

  const readyCount = languages.filter(item => item.ready).length;
  const fallbackCount = languages.filter(item => !item.ready && item.fallback).length;
  const effectiveReady = readyCount + fallbackCount;

  if (message) {
    if (readyCount === languages.length) message.textContent = t("code.status.ready");
    else if (effectiveReady === languages.length && fallbackCount > 0) message.textContent = t("code.status.fallbackOnly");
    else message.textContent = t("code.status.partial").replace("{ready}", String(readyCount)).replace("{total}", String(languages.length));
  }
  if (badge) {
    if (readyCount === languages.length) {
      badge.textContent = t("code.readiness.ready");
      badge.dataset.tone = "ok";
    } else if (effectiveReady === languages.length) {
      badge.textContent = t("code.readiness.fallback");
      badge.dataset.tone = "fallback";
    } else {
      badge.textContent = t("code.readiness.partial");
      badge.dataset.tone = "warn";
    }
  }
  if (pills) {
    pills.innerHTML = languages.slice(0, 6).map(item => {
      const tone = codeLanguageTone(item);
      return `<span class="code-status-pill" data-tone="${tone}">${escapeHtml(item.server_label || item.language)} · ${escapeHtml(t("code.lang.files").replace("{count}", String(item.count)))} · ${escapeHtml(codeLanguageStatusLabel(item))}</span>`;
    }).join("");
  }

  const setupBadge = document.querySelector("#code-setup-badge");
  if (setupBadge) setupBadge.textContent = t("code.setup.summary").replace("{ready}", String(effectiveReady)).replace("{total}", String(Math.max(languages.length, servers.length || languages.length)));
}
function renderCodeLanguageSupport(languages, servers) {
  const container = document.querySelector("#code-language-support");
  if (!container) return;
  const serverById = Object.fromEntries((servers || []).map(server => [server.id, server]));
  if (!languages.length) {
    container.innerHTML = `<article class="code-support-card empty"><p>${escapeHtml(t("code.lang.noData"))}</p></article>`;
    return;
  }
  const grouped = new Map();
  for (const item of languages) {
    const key = item.server_id || item.extension;
    const existing = grouped.get(key);
    if (!existing) {
      grouped.set(key, { ...item, extensions: [item.extension], count: item.count || 0 });
      continue;
    }
    existing.count += item.count || 0;
    if (!existing.extensions.includes(item.extension)) existing.extensions.push(item.extension);
    existing.ready = existing.ready || item.ready;
  }
  container.innerHTML = [...grouped.values()].map(item => {
    const server = serverById[item.server_id] || {};
    const tone = codeLanguageTone({ ...item, status: server.status || item.status });
    const install = item.install_command || server.install_command || "";
    const runnable = item.install_runnable ?? server.install_runnable ?? Boolean(install);
    const extLabel = (item.extensions || [item.extension]).filter(Boolean).join(", ");
    const installActions = install ? `<div class="code-support-actions">
        ${runnable && !item.ready ? `<button type="button" class="btn-secondary" data-code-install="${escapeHtml(item.server_id || "")}">${escapeHtml(t("code.lang.install"))}</button>` : ""}
        <button type="button" class="btn-text" data-install-toggle>${escapeHtml(t("code.lang.showCommand"))}</button>
      </div>
      <div class="code-install-command" hidden>
        <code>${escapeHtml(install)}</code>
        <div class="provider-actions install-actions">
          ${runnable ? `<button data-code-install="${escapeHtml(item.server_id || "")}" type="button">${escapeHtml(t("code.lang.run"))}</button>` : ""}
          <button data-install-copy type="button" data-command="${escapeHtml(install)}">${escapeHtml(t("code.lang.copy"))}</button>
          <button data-agent-install-command="${escapeHtml(install)}" type="button">Ask Agent</button>
        </div>
        <p class="code-install-result" data-install-result hidden></p>
      </div>` : "";
    return `<article class="code-support-card" data-tone="${tone}" data-server-id="${escapeHtml(item.server_id || "")}">
      <div class="code-support-head">
        <div>
          <strong>${escapeHtml(item.server_label || item.language)}</strong>
          <p>${escapeHtml(extLabel)} · ${escapeHtml(t("code.lang.files").replace("{count}", String(item.count)))}</p>
        </div>
        <span class="code-support-status">${escapeHtml(codeLanguageStatusLabel({ ...item, status: server.status }))}</span>
      </div>
      ${server.notes ? `<p class="code-support-note">${escapeHtml(server.notes)}</p>` : ""}
      ${installActions}
    </article>`;
  }).join("");
  container.querySelectorAll("[data-install-toggle]").forEach(button => {
    button.addEventListener("click", () => {
      const command = button.closest(".code-support-card")?.querySelector(".code-install-command");
      if (!command) return;
      command.hidden = !command.hidden;
    });
  });
}
async function installCodeLanguageServer(serverId, trigger) {
  if (!serverId) return;
  const card = trigger?.closest?.(".code-support-card") || document.querySelector(`.code-support-card[data-server-id="${CSS.escape(serverId)}"]`);
  const resultEl = card?.querySelector("[data-install-result]");
  const buttons = card ? [...card.querySelectorAll("[data-code-install]")] : [];
  buttons.forEach(btn => { btn.disabled = true; btn.textContent = t("code.lang.installing"); });
  if (resultEl) {
    resultEl.hidden = false;
    resultEl.className = "code-install-result";
    resultEl.textContent = t("code.lang.installing");
  }
  const commandPanel = card?.querySelector(".code-install-command");
  if (commandPanel) commandPanel.hidden = false;
  try {
    const payload = await api(`/api/code/servers/${encodeURIComponent(serverId)}/install`, {
      method: "POST",
      body: JSON.stringify({ project_id: state.projectId, timeout_seconds: 600 }),
    });
    if (resultEl) {
      const detail = [payload.message, payload.stderr, payload.stdout].filter(Boolean).join("\n").trim();
      resultEl.className = `code-install-result ${payload.ready || payload.status === "ok" ? "ok" : "error"}`;
      resultEl.textContent = detail.slice(0, 1200) || (payload.ready ? t("code.lang.installOk") : t("code.lang.installFailed"));
    }
    await loadCodeServers();
  } catch (error) {
    if (resultEl) {
      resultEl.className = "code-install-result error";
      resultEl.textContent = error.message || t("code.lang.installFailed");
    }
    buttons.forEach(btn => { btn.disabled = false; btn.textContent = t("code.lang.install"); });
    throw error;
  }
}
function renderCodeServerList(servers) {
  const list = document.querySelector("#code-server-list");
  if (!list) return;
  list.innerHTML = "";
  for (const server of servers) {
    const command = Array.isArray(server.command) ? server.command.join(" ") : (server.command || "");
    const el = document.createElement("article");
    el.className = `provider code-server-card ${server.status === "available" ? "ready" : server.status === "missing_executable" ? "error" : "planned"}`;
    el.innerHTML = `<div class="provider-head"><div><strong>${escapeHtml(server.label)}</strong><p>${escapeHtml((server.extensions || []).join(", "))}</p></div><span class="provider-status">${escapeHtml(codeLanguageStatusLabel({ ready: server.status === "available", fallback: false, status: server.status }))}</span></div><p class="provider-hint">${escapeHtml(server.notes || "")}</p>${installCommandHtml(server.install_command, server.id)}<label class="code-command-field">Command<input data-code-command="${escapeHtml(server.id)}" value="${escapeHtml(command)}"></label><div class="provider-actions code-test-actions"><button data-code-test="${escapeHtml(server.id)}" type="button">Test</button><span class="provider-test" data-code-result></span></div>`;
    list.appendChild(el);
  }
  list.querySelectorAll("[data-code-command]").forEach(input => input.addEventListener("change", async () => { await api("/api/code/servers", { method: "PATCH", body: JSON.stringify({ id: input.dataset.codeCommand, command: input.value.split(" ").filter(Boolean) }) }); }));
  list.querySelectorAll("[data-code-test]").forEach(button => button.addEventListener("click", async () => {
    const card = button.closest(".provider");
    const result = card.querySelector("[data-code-result]");
    result.className = "provider-test"; result.textContent = "checking...";
    try {
      const payload = await api(`/api/code/servers/${button.dataset.codeTest}/test`, { method: "POST", body: "{}" });
      result.className = `provider-test ${payload.ready ? "ok" : "error"}`;
      result.textContent = payload.message || "";
      await loadCodeServers();
    } catch (error) { result.className = "provider-test error"; result.textContent = error.message; }
  }));
}
function syncCodeInsightTab() {
  const tab = state.codeInsightTab || "symbols";
  document.querySelectorAll("[data-code-tab]").forEach(button => {
    const active = button.dataset.codeTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  const position = document.querySelector("#code-position-controls");
  if (position) position.hidden = tab === "symbols" || tab === "diagnostics";
  const symbols = document.querySelector("#code-symbols");
  const insight = document.querySelector("#code-insight");
  if (symbols) symbols.hidden = tab !== "symbols";
  if (insight) insight.hidden = tab === "symbols";
}
function syncCodeExploreEmpty(hasResults) {
  const empty = document.querySelector("#code-explore-empty");
  if (empty) empty.hidden = Boolean(hasResults);
}
function setCodeInsightTab(tab) {
  state.codeInsightTab = tab;
  syncCodeInsightTab();
}
async function runCodeInsight() {
  const tab = state.codeInsightTab || "symbols";
  if (tab === "symbols") await loadCodeSymbols();
  else if (tab === "diagnostics") await loadCodeDiagnostics();
  else if (tab === "hover") await loadCodeHover();
  else if (tab === "references") await loadCodeReferences();
}
async function analyzeCodeProject() {
  const status = document.querySelector("#code-status-message");
  if (status) status.textContent = t("action.scanProject") + "...";
  try {
    await api("/api/project/scan", { method: "POST", body: JSON.stringify({ project_id: state.projectId }) });
    await loadCodeServers();
    showSnackbar(t("action.scanProject"), "ok");
  } catch (error) {
    showError(error);
    await loadCodeServers();
  }
}
function installCommandHtml(command, serverId = "") {
  if (!command) return "";
  const value = escapeHtml(command);
  const id = escapeHtml(serverId || "");
  return `<div class="install-command"><strong>Missing/install:</strong><div class="command-row"><code title="${value}">${value}</code><div class="provider-actions install-actions">${serverId ? `<button data-code-install="${id}" type="button">${escapeHtml(t("code.lang.run"))}</button>` : `<button data-terminal-install-run="${value}" type="button">${escapeHtml(t("code.lang.run"))}</button>`}<button data-install-copy type="button" data-command="${value}">${escapeHtml(t("code.lang.copy"))}</button><button data-agent-install-command="${value}" type="button">Ask Agent</button></div></div></div>`;
}
function fillCodeFileFromSelection(overwrite = true) {
  const input = document.querySelector("#code-file-path");
  if (!input || !state.selectedFile) return false;
  if (overwrite || !input.value.trim()) input.value = state.selectedFile;
  syncCodeFileHint();
  return true;
}
function syncCodeFileHint() {
  const hint = document.querySelector("#code-selected-file-hint");
  if (!hint) return;
  hint.textContent = state.selectedFile
    ? t("code.usingFile").replace("{path}", state.selectedFile)
    : t("code.noFileSelected");
}
function toggleCodeAdvanced() {
  const panel = document.querySelector(".code-lsp-advanced");
  if (!panel) return;
  panel.open = !panel.open;
}
function codeRequestPayload() {
  const input = document.querySelector("#code-file-path");
  const path = (input.value || state.selectedFile || "").trim();
  if (input && path) input.value = path;
  return {
    project_id: state.projectId,
    path,
    line: Number(document.querySelector("#code-line")?.value || 0),
    character: Number(document.querySelector("#code-character")?.value || 0),
    query: document.querySelector("#code-reference-query")?.value || "",
  };
}
async function loadCodeServers() {
  const [payload, languages] = await Promise.all([api("/api/code/servers"), api(`/api/code/languages?project_id=${projectParam()}`)]);
  state.codeServersCache = payload;
  const languageItems = languages.languages || [];
  renderCodeStatus(languageItems, payload.servers || []);
  renderCodeLanguageSupport(languageItems, payload.servers || []);
  renderCodeServerList(payload.servers || []);
}
async function loadCodeSymbols() {
  const container = document.querySelector("#code-symbols");
  const request = codeRequestPayload();
  if (!request.path) { container.innerHTML = `<article class="result"><strong>${escapeHtml(t("code.noFileSelected"))}</strong></article>`; syncCodeExploreEmpty(false); return; }
  container.innerHTML = '<article class="result"><strong>Analyzing</strong><p>...</p></article>';
  syncCodeExploreEmpty(true);
  try {
    const payload = await api("/api/code/symbols", { method: "POST", body: JSON.stringify(request) });
    const symbols = payload.symbols || [];
    container.innerHTML = `<article class="result code-insight-summary"><strong>${escapeHtml(payload.language)} · ${symbols.length} symbol(s)</strong><p>${escapeHtml(payload.path)} · ${escapeHtml(payload.source || "lsp")}${payload.message ? ` · ${escapeHtml(payload.message)}` : ""}</p></article>` + (symbols.length ? symbols.map(symbol => `<article class="result code-symbol-row" style="margin-left:${Math.min(symbol.depth, 5) * 14}px"><div class="row"><strong>${escapeHtml(symbol.name)}</strong><span class="badge">${escapeHtml(symbol.kind)}</span></div><span class="badge">line ${escapeHtml(String(symbol.line))}</span>${symbol.detail ? `<span class="muted"> ${escapeHtml(symbol.detail)}</span>` : ""}</article>`).join("") : `<article class="result"><strong>No symbols returned</strong></article>`);
    syncCodeExploreEmpty(true);
  } catch (error) {
    container.innerHTML = `<article class="result"><strong>Symbols failed</strong><p>${escapeHtml(error.message)}</p></article>`;
    syncCodeExploreEmpty(true);
  }
}
async function loadCodeHover() {
  const container = document.querySelector("#code-insight");
  const request = codeRequestPayload();
  if (!request.path) { container.innerHTML = `<article class="result"><strong>${escapeHtml(t("code.noFileSelected"))}</strong></article>`; syncCodeExploreEmpty(false); return; }
  syncCodeExploreEmpty(true);
  const payload = await api("/api/code/hover", { method: "POST", body: JSON.stringify(request) });
  container.innerHTML = `<article class="result code-insight-summary"><strong>Hover (${escapeHtml(payload.source || "lsp")})</strong><p>${escapeHtml(payload.path)}:${escapeHtml(String(Number(payload.line || 0) + 1))}:${escapeHtml(String(payload.character || 0))}</p><pre class="inline-pre">${escapeHtml(payload.hover || "No hover text.")}</pre></article>`;
}
async function loadCodeDiagnostics() {
  const container = document.querySelector("#code-insight");
  const request = codeRequestPayload();
  if (!request.path) { container.innerHTML = `<article class="result"><strong>${escapeHtml(t("code.noFileSelected"))}</strong></article>`; syncCodeExploreEmpty(false); return; }
  syncCodeExploreEmpty(true);
  const payload = await api("/api/code/diagnostics", { method: "POST", body: JSON.stringify(request) });
  const items = payload.diagnostics || [];
  container.innerHTML = `<article class="result code-insight-summary"><strong>Diagnostics (${escapeHtml(payload.source || "basic")})</strong><p>${escapeHtml(payload.path)} · ${items.length} issue(s)</p></article>` + (items.length ? items.map(item => `<article class="result"><div class="row"><strong>${escapeHtml(item.severity || "info")}</strong><span class="badge">line ${escapeHtml(String(item.line || 1))}</span></div><p>${escapeHtml(item.message || "")}</p></article>`).join("") : '<article class="result"><strong>No diagnostics</strong></article>');
}
async function loadCodeReferences() {
  const container = document.querySelector("#code-insight");
  const request = codeRequestPayload();
  if (!request.path) { container.innerHTML = `<article class="result"><strong>${escapeHtml(t("code.noFileSelected"))}</strong></article>`; syncCodeExploreEmpty(false); return; }
  syncCodeExploreEmpty(true);
  const payload = await api("/api/code/references", { method: "POST", body: JSON.stringify(request) });
  const refs = payload.references || [];
  container.innerHTML = `<article class="result code-insight-summary"><strong>References (${escapeHtml(payload.source || "lsp")})</strong><p>${escapeHtml(payload.query || "")} · ${refs.length} result(s)</p></article>` + (refs.length ? refs.map(ref => `<article class="result"><div class="row"><strong>${escapeHtml(ref.path || payload.path)}</strong><span class="badge">line ${escapeHtml(String(ref.line || 1))}</span></div><p>${escapeHtml(ref.preview || "")}</p></article>`).join("") : '<article class="result"><strong>No references</strong></article>');
}
