/* Providers status/cards/runs + analytics — extracted from app.js */
function providerStatusLabel(status, enabled) {
  const tone = providerStatusClass(status, enabled);
  const labels = {
    ready: t("providers.status.ready"),
    error: t("providers.status.error"),
    disabled: t("providers.status.disabled"),
    planned: t("providers.status.planned"),
  };
  return labels[tone] || tone;
}
function renderProvidersStatus(providers) {
  const message = document.querySelector("#providers-status-message");
  const badge = document.querySelector("#providers-status-badge");
  if (!providers.length) {
    if (message) message.textContent = t("providers.status.empty");
    if (badge) { badge.textContent = t("providers.readiness.idle"); badge.dataset.tone = "idle"; }
    return;
  }
  const ready = providers.filter(provider => providerStatusClass(provider.status, provider.enabled) === "ready").length;
  const enabled = providers.filter(provider => provider.enabled).length;
  if (message) {
    message.textContent = t("providers.status.summary")
      .replace("{ready}", String(ready))
      .replace("{enabled}", String(enabled))
      .replace("{total}", String(providers.length));
  }
  if (badge) {
    if (ready === providers.length) {
      badge.textContent = t("providers.readiness.ready");
      badge.dataset.tone = "ok";
    } else if (ready > 0) {
      badge.textContent = t("providers.readiness.partial");
      badge.dataset.tone = "warn";
    } else {
      badge.textContent = t("providers.readiness.idle");
      badge.dataset.tone = "idle";
    }
  }
}
async function connectEnvProviders() {
  const summary = document.querySelector("#provider-summary");
  if (summary) summary.textContent = "connecting environment providers...";
  const payload = await api("/api/providers/connect-env", { method: "POST", body: "{}" });
  const missing = (payload.missing || []).map(item => item.api_key_env).join(", ");
  if (summary) summary.textContent = `${payload.connected.length} connected${missing ? `; missing ${missing}` : ""}`;
  showSnackbar(payload.message || "Environment providers checked.", payload.connected.length ? "success" : "info");
  await loadProviders();
  await loadCouncil();
}
async function testAllProviders() {
  const summary = document.querySelector("#provider-summary");
  if (summary) summary.textContent = "checking providers...";
  const payload = await api("/api/providers/test-all", { method: "POST", body: "{}" });
  const ready = payload.checks.filter(check => check.provider.ready).length;
  if (summary) summary.textContent = `${ready}/${payload.checks.length} ready`;
  await loadProviders();
}
function buildProviderCard(provider) {
  const command = Array.isArray(provider.command) ? provider.command.join(" ") : (provider.command || "");
  const statusClass = providerStatusClass(provider.status, provider.enabled);
  const statusLabel = providerStatusLabel(provider.status, provider.enabled);
  const lastCheck = provider.last_check ? `<span class="provider-last-check">last check: ${escapeHtml(provider.last_check.status)} at ${escapeHtml(provider.last_check.checked_at || "")}</span>` : "";
  const modelOptions = (provider.available_models || []).map(model => `<option value="${escapeHtml(model.name)}">${escapeHtml(model.name)}</option>`).join("");
  const hasModelPicker = provider.id === "ollama" || (provider.id === "openrouter" && provider.available_models && provider.available_models.length);
  const modelControl = hasModelPicker
    ? `<label>Model<select data-model="${escapeHtml(provider.id)}"><option value="">default</option>${modelOptions}</select></label>`
    : `<label>Model<input data-model="${escapeHtml(provider.id)}" value="${escapeHtml(provider.model)}" placeholder="model"></label>`;
  const modelMeta = ["ollama", "openrouter"].includes(provider.id) && provider.models_checked_at ? `<span class="provider-last-check">models checked: ${escapeHtml(provider.models_checked_at)}</span>` : "";
  const modelActions = ["ollama", "openrouter"].includes(provider.id) ? `<button data-refresh-models="${escapeHtml(provider.id)}" type="button">Refresh Models</button><span class="provider-test" data-model-result></span>` : "";
  const loginLabel = providerLoginLabel(provider);
  const loginAction = loginLabel
    ? `<button data-login-provider="${escapeHtml(provider.id)}" type="button" class="btn-secondary">${escapeHtml(loginLabel)}</button>`
    : "";
  return `<div class="provider-head"><div><strong>${escapeHtml(provider.label)}</strong><p>${escapeHtml(provider.provider_type)} · ${escapeHtml(statusLabel)}</p></div><span class="provider-status ${statusClass}">${escapeHtml(statusLabel)}</span></div><p class="provider-hint">${escapeHtml(providerHint(provider))}</p>${lastCheck}${modelMeta}<div class="provider-card-actions"><label class="inline-check"><input type="checkbox" data-provider="${escapeHtml(provider.id)}" ${provider.enabled ? "checked" : ""}> enabled</label><label class="inline-check"><input type="checkbox" data-approval-required="${escapeHtml(provider.id)}" ${provider.approval_required ? "checked" : ""}> require approval</label>${loginAction}<button data-test-provider="${escapeHtml(provider.id)}" type="button" class="btn-secondary">Test</button>${modelActions}<span class="provider-test" data-test-result></span></div><details class="provider-advanced"><summary>Configuration</summary><div class="provider-advanced-body">${modelControl}<label>Command<input data-command="${escapeHtml(provider.id)}" value="${escapeHtml(command)}" placeholder="command"></label><label>Workdir policy<select data-workdir-policy="${escapeHtml(provider.id)}"><option value="project-root">project-root</option><option value="custom">custom under project</option></select></label><label>Workdir<input data-workdir="${escapeHtml(provider.id)}" value="${escapeHtml(provider.workdir || "")}" placeholder="optional project subdirectory"></label><label>Base URL<input data-base-url="${escapeHtml(provider.id)}" value="${escapeHtml(provider.base_url || "")}" placeholder="http://127.0.0.1:11434"></label><label>API key env<input data-api-key-env="${escapeHtml(provider.id)}" value="${escapeHtml(provider.api_key_env || "")}" placeholder="${provider.id === "gemini-cli" ? "GEMINI_API_KEY" : "OPENAI_API_KEY"}"></label><label>Timeout<input data-timeout="${escapeHtml(provider.id)}" type="number" min="1" value="${escapeHtml(provider.timeout_seconds || 120)}"></label></div></details><div class="provider-actions-list" data-action-list></div>`;
}
async function loadProviders() {
  const payload = await api("/api/providers");
  syncChatProviderSelect(payload.providers);
  renderProvidersStatus(payload.providers);
  const list = document.querySelector("#provider-list");
  list.innerHTML = "";
  for (const provider of payload.providers) {
    const el = document.createElement("article");
    el.className = `provider provider-card-compact ${providerStatusClass(provider.status, provider.enabled)}`;
    el.innerHTML = buildProviderCard(provider);
    list.appendChild(el);
    const modelSelect = el.querySelector("select[data-model]");
    if (modelSelect) modelSelect.value = provider.model || "";
    const policy = el.querySelector("[data-workdir-policy]");
    if (policy) policy.value = provider.workdir_policy || "project-root";
  }
  list.querySelectorAll("[data-provider]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.provider}`, { method: "PATCH", body: JSON.stringify({ enabled: input.checked }) }); await loadProviders(); }));
  list.querySelectorAll("[data-model]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.model}`, { method: "PATCH", body: JSON.stringify({ model: input.value }) }); }));
  list.querySelectorAll("[data-command]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.command}`, { method: "PATCH", body: JSON.stringify({ command: input.value }) }); }));
  list.querySelectorAll("[data-approval-required]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.approvalRequired}`, { method: "PATCH", body: JSON.stringify({ approval_required: input.checked }) }); }));
  list.querySelectorAll("[data-workdir-policy]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.workdirPolicy}`, { method: "PATCH", body: JSON.stringify({ workdir_policy: input.value }) }); }));
  list.querySelectorAll("[data-workdir]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.workdir}`, { method: "PATCH", body: JSON.stringify({ workdir: input.value }) }); }));
  list.querySelectorAll("[data-base-url]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.baseUrl}`, { method: "PATCH", body: JSON.stringify({ base_url: input.value }) }); }));
  list.querySelectorAll("[data-api-key-env]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.apiKeyEnv}`, { method: "PATCH", body: JSON.stringify({ api_key_env: input.value }) }); }));
  list.querySelectorAll("[data-timeout]").forEach(input => input.addEventListener("change", async () => { await api(`/api/providers/${input.dataset.timeout}`, { method: "PATCH", body: JSON.stringify({ timeout_seconds: input.value }) }); }));
  list.querySelectorAll("[data-refresh-models]").forEach(button => button.addEventListener("click", async () => {
    const card = button.closest(".provider");
    const result = card.querySelector("[data-model-result]");
    result.className = "provider-test";
    result.textContent = "refreshing models...";
    try {
      const payload = await api(`/api/providers/${button.dataset.refreshModels}/models`);
      result.className = `provider-test ${payload.provider.ready ? "ok" : "error"}`;
      result.textContent = payload.hint ? `${payload.message} ${payload.hint}` : payload.message;
      await loadProviders();
    } catch (error) {
      result.className = "provider-test error";
      result.textContent = error.message;
    }
  }));
  list.querySelectorAll("[data-test-provider]").forEach(button => button.addEventListener("click", async () => {
    const card = button.closest(".provider");
    const result = card.querySelector("[data-test-result]");
    const actions = card.querySelector("[data-action-list]");
    result.className = "provider-test";
    result.textContent = "checking...";
    actions.innerHTML = "";
    try {
      const payload = await api(`/api/providers/${button.dataset.testProvider}/test`, { method: "POST", body: "{}" });
      result.className = `provider-test ${payload.provider.ready ? "ok" : "error"}`;
      result.textContent = payload.hint ? `${payload.message} ${payload.hint}` : payload.message;
      actions.innerHTML = (payload.actions || []).map(action => `<span class="badge">${escapeHtml(action)}</span>`).join("");
      await loadProviders();
    } catch (error) {
      result.className = "provider-test error";
      result.textContent = error.message;
    }
  }));
  list.querySelectorAll("[data-login-provider]").forEach(button => button.addEventListener("click", async () => {
    const card = button.closest(".provider");
    const result = card.querySelector("[data-test-result]");
    const actions = card.querySelector("[data-action-list]");
    result.className = "provider-test";
    result.textContent = "opening sign-in…";
    if (actions) actions.innerHTML = "";
    try {
      const payload = await api(`/api/providers/${button.dataset.loginProvider}/login`, { method: "POST", body: "{}" });
      result.className = `provider-test ${payload.provider?.ok ? "ok" : "error"}`;
      result.textContent = payload.hint ? `${payload.message} ${payload.hint}` : (payload.message || "Sign-in started.");
      showSnackbar(payload.message || "Sign-in started.", payload.provider?.ok ? "success" : "info");
    } catch (error) {
      result.className = "provider-test error";
      result.textContent = error.message;
    }
  }));
}
function isProviderSelectable(provider) {
  if (!provider || !provider.enabled) return false;
  if (provider.last_check && provider.last_check.ready) return true;
  return providerStatusClass(provider.status, provider.enabled) === "ready";
}
function syncChatProviderSelect(providers) {
  const selects = [document.querySelector("#chat-provider"), document.querySelector("#workspace-chat-provider")].filter(Boolean);
  if (!selects.length) return;
  const current = selects[0].value || "auto";
  const html = '<option value="auto">Auto route</option><option value="local-memory">Local memory</option>' + providers.map(provider => {
    const disabled = isProviderSelectable(provider) ? "" : " disabled";
    const suffix = disabled ? " (unavailable)" : "";
    return `<option value="${escapeHtml(provider.id)}"${disabled}>${escapeHtml(provider.label)}${escapeHtml(suffix)}</option>`;
  }).join("");
  for (const select of selects) {
    const keep = select.value || current;
    select.innerHTML = html;
    select.value = keep;
    if (!select.value || select.selectedOptions[0]?.disabled) select.value = "auto";
  }
  syncAskMode();
}
async function loadProviderRuns() {
  const container = document.querySelector("#provider-run-list");
  if (!container) return;
  const payload = await api(`/api/provider-runs?project_id=${projectParam()}&limit=12`);
  container.innerHTML = payload.runs.length ? "" : '<div class="result"><strong>No provider runs</strong><p>Run chat or AI routing to populate the audit trail.</p></div>';
  for (const run of payload.runs) {
    const el = document.createElement("article");
    el.className = "result";
    const usageLabel = formatUsageLabel(run.usage || {});
    const model = run.model || (run.selected_provider && run.selected_provider.model) || "";
    el.innerHTML = `<strong>${escapeHtml(run.provider_label || run.provider_id)}${model ? ` · ${escapeHtml(model)}` : ""}</strong><p>${escapeHtml(run.message_preview || "")}</p><span class="badge">${escapeHtml(run.status)}</span><span class="badge">${escapeHtml(run.updated_at || "")}</span>${usageLabel ? `<span class="badge">${escapeHtml(usageLabel)}</span>` : ""}${run.stderr_preview ? `<p>stderr: ${escapeHtml(run.stderr_preview)}</p>` : ""}`;
    container.appendChild(el);
  }
}
async function loadAnalytics() {
  const payload = await api(`/api/analytics?project_id=${projectParam()}`);
  const grids = [
    document.querySelector("#analytics-grid"),
    document.querySelector("#analytics-view-grid"),
  ].filter(Boolean);
  if (!grids.length) return;
  const life = payload.memory_lifecycle || {};
  const tier = life.by_tier || {};
  const stateCounts = life.by_state || {};
  const cov = (payload.embeddings && payload.embeddings.coverage) || {};
  const indexed = Number(cov.indexed || 0);
  const active = Number(cov.active_nodes || 0);
  const embValue = active ? `${indexed}/${active}` : String(indexed);
  const usage = payload.provider_usage || {};
  const totals = usage.totals || {};
  const byModel = Array.isArray(usage.by_model) ? usage.by_model : [];
  const tokenTotal = Number(totals.total_tokens || 0);
  const costTotal = totals.cost_usd != null ? Number(totals.cost_usd) : null;
  const metrics = [
    ["Nodes", payload.nodes],
    ["Edges", payload.edges],
    [t("memory.embeddingsMetric"), embValue],
    [t("usage.tokensMetric"), formatTokenCount(tokenTotal)],
    [t("usage.costMetric"), costTotal != null && costTotal > 0 ? formatUsageCost(costTotal) : "—"],
    ["Tasks", payload.tasks],
    ["Dialogs", payload.chats],
    ["Favorites", payload.favorites],
    ["Long-term", tier.long_term || 0],
    ["Short-term", tier.short_term || 0],
    ["Fresh", stateCounts.fresh || 0],
    ["Stale", stateCounts.stale || 0],
    ["Archived", stateCounts.archived || 0],
  ];
  const modelRows = byModel.slice(0, 8).map(row => {
    const name = `${row.provider_id || ""}${row.model ? ` / ${row.model}` : ""}`.replace(/^ \/ /, "");
    const tokens = formatTokenCount(row.total_tokens || 0);
    const cost = row.cost_usd != null ? formatUsageCost(row.cost_usd) : "—";
    return `<div class="usage-model-row"><strong>${escapeHtml(name || "unknown")}</strong><span>${escapeHtml(String(row.runs || 0))} runs · ${escapeHtml(tokens)} tok · ${escapeHtml(cost)}</span></div>`;
  }).join("");
  const html = [
    ...metrics.map(([label, value]) => `<article class="metric"><strong>${value}</strong><span>${label}</span></article>`),
    `<article class="metric metric-wide usage-by-model"><strong>${t("usage.byModel")}</strong><div class="usage-model-list">${modelRows || `<div class="usage-model-empty">${t("usage.empty")}</div>`}</div></article>`,
  ].join("");
  for (const grid of grids) grid.innerHTML = html;
}
