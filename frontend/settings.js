/* Settings, embeddings, router — extracted from app.js */
import { api } from "./api-client.js";
import { showAppConfirm, showAppPrompt } from "./app-dialog.js";
import { escapeHtml, showSnackbar } from "./dom-utils.js";
import { currentProject, displayProjectName, state, t } from "./state.js";
import { applyDensity, applyLanguage, applyTheme } from "./ui.js";

let lastVectorRuntime = null;

async function loadSettings() {
  const settings = await api("/api/settings");
  const ui = settings.ui || {};
  const lifecycle = settings.memory_lifecycle || {};
  const workspace = settings.workspace || {};
  if (workspace.current_project_id) state.projectId = workspace.current_project_id;

  // Default to system theme if not set or on first load
  const theme = ui.theme === "dark" ? "system" : (ui.theme || "system");

  const themeSelect = document.querySelector("#theme-select");
  const densitySelect = document.querySelector("#density-select");
  const languageSelect = document.querySelector("#settings-language-select");
  const memoryEnabled = document.querySelector("#memory-enabled");
  const askMemoryAdvice = document.querySelector("#ask-memory-advice");
  const lifecycleEnabled = document.querySelector("#lifecycle-enabled");
  const refreshOnAccess = document.querySelector("#refresh-on-access");
  const autoRescanOnStartup = document.querySelector("#auto-rescan-on-startup");
  const sourceSchedulerEnabled = document.querySelector("#source-scheduler-enabled");
  const sourceSchedulerTick = document.querySelector("#source-scheduler-tick");
  const sourceDefaultInterval = document.querySelector("#source-default-interval");
  const chatMemoryMode = document.querySelector("#chat-memory-mode");
  const chatCandidateTtl = document.querySelector("#chat-candidate-ttl");
  const chatSessionIdle = document.querySelector("#chat-session-idle");
  const chatStoreFactsOnly = document.querySelector("#chat-store-facts-only");
  const shortTermTtl = document.querySelector("#short-term-ttl");
  const archiveAfter = document.querySelector("#archive-after");
  const deleteAfter = document.querySelector("#delete-after");
  const promoteAfterHits = document.querySelector("#promote-after-hits");

  if (themeSelect) themeSelect.value = theme;
  if (densitySelect) densitySelect.value = ui.density || "comfortable";
  if (languageSelect) languageSelect.value = ui.language || state.language || "en";
  if (memoryEnabled) memoryEnabled.checked = ui.memory_enabled !== false;
  if (askMemoryAdvice) askMemoryAdvice.checked = ui.ask_memory_advice !== false;
  state.askMemoryAdvice = ui.ask_memory_advice !== false;
  applyTheme(theme);
  applyDensity(ui.density || "comfortable");
  applyLanguage(ui.language || state.language || "en");
  if (typeof window.syncAskMemoryAdviceBadge === "function") window.syncAskMemoryAdviceBadge();
  if (lifecycleEnabled) lifecycleEnabled.checked = lifecycle.enabled !== false;
  if (refreshOnAccess) refreshOnAccess.checked = lifecycle.refresh_on_access !== false;
  if (autoRescanOnStartup) autoRescanOnStartup.checked = lifecycle.auto_rescan_on_startup !== false;
  if (sourceSchedulerEnabled) sourceSchedulerEnabled.checked = lifecycle.source_scheduler_enabled !== false;
  if (sourceSchedulerTick) sourceSchedulerTick.value = lifecycle.source_scheduler_tick_minutes ?? 5;
  if (sourceDefaultInterval) sourceDefaultInterval.value = lifecycle.source_default_interval_minutes ?? 60;
  if (chatMemoryMode) chatMemoryMode.value = lifecycle.chat_memory_mode || "strict";
  if (chatCandidateTtl) chatCandidateTtl.value = lifecycle.chat_candidate_ttl_days ?? 7;
  if (chatSessionIdle) chatSessionIdle.value = lifecycle.chat_session_idle_minutes ?? 30;
  if (chatStoreFactsOnly) chatStoreFactsOnly.checked = lifecycle.chat_store_facts_only !== false;
  if (shortTermTtl) shortTermTtl.value = lifecycle.short_term_ttl_days || 14;
  if (archiveAfter) archiveAfter.value = lifecycle.archive_after_days || 30;
  if (deleteAfter) deleteAfter.value = lifecycle.delete_after_days || 0;
  if (promoteAfterHits) promoteAfterHits.value = lifecycle.promote_after_hits || 5;
  state.onboardingComplete = ui.onboarding_complete === true;
  fillEmbeddingsSettings(settings);
  fillIntegrationCredentials(settings.integrations || {});
}

function fillEmbeddingsSettings(settings) {
  const retrieval = settings.memory_retrieval || {};
  const status = settings.memory_embeddings || {};
  state.embeddingCatalog = Array.isArray(status.catalog) ? status.catalog : (state.embeddingCatalog || []);
  const enabled = document.querySelector("#embeddings-enabled");
  const provider = document.querySelector("#embedding-provider");
  const dims = document.querySelector("#embedding-dimensions");
  const pool = document.querySelector("#vector-pool");
  const minScore = document.querySelector("#vector-min-score");
  const relevanceFloor = document.querySelector("#search-relevance-floor");
  const timeout = document.querySelector("#vector-query-timeout");
  const reindex = document.querySelector("#reindex-on-startup");
  const statusEl = document.querySelector("#embeddings-status");
  if (enabled) enabled.checked = retrieval.embeddings_enabled !== false;
  if (provider) provider.value = retrieval.embedding_provider || status.provider || "auto";
  if (dims) dims.value = retrieval.embedding_dimensions || status.dimensions || 1024;
  if (pool) pool.value = retrieval.vector_pool || 64;
  if (minScore) minScore.value = retrieval.vector_min_score ?? 0.22;
  if (relevanceFloor) relevanceFloor.value = retrieval.search_relevance_floor ?? 0.28;
  if (timeout) timeout.value = retrieval.vector_query_timeout_ms || 2500;
  if (reindex) reindex.checked = retrieval.reindex_on_startup === true;
  refreshEmbeddingModelOptions(retrieval.embedding_model || status.model || "");
  fillEmbeddingConnection(retrieval, status);
  fillVectorRuntime(settings.vector_runtime || {
    ok: status.sqlite_vec && status.sqlite_vec.loaded,
    current: status.sqlite_vec || {},
    repair: {},
    backend: status.vector_backend || "python",
  });
  if (statusEl) {
    const env = status.env || {};
    const envBits = [
      env.cloudflare_token && env.cloudflare_account ? "CF✓" : "CF✗",
      env.gemini_key ? "Gemini✓" : "Gemini✗",
      env.ollama_host ? "Ollama✓" : "Ollama✗",
      env.azure_key ? "Azure✓" : "Azure✗",
    ].join(" · ");
    const cov = status.coverage || {};
    const indexed = Number(cov.indexed || 0);
    const active = Number(cov.active_nodes || 0);
    const pct = cov.coverage_pct != null ? Number(cov.coverage_pct) : (active ? Math.round((indexed / active) * 1000) / 10 : 0);
    const covBit = active ? `${indexed}/${active} (${pct}%)` : `${indexed} indexed`;
    const last = status.last_check || {};
    const requested = status.requested_provider || retrieval.embedding_provider || "auto";
    const actual = status.provider || "—";
    const mismatch = requested !== "auto" && actual && requested !== actual;
    const lastBit = last.checked_at ? ` · last test: ${last.status || "?"} ${String(last.message || "").slice(0, 140)}` : "";
    statusEl.textContent = mismatch
      ? `${t("settings.embeddingFallbackWarn")} ${requested} → ${actual} / ${status.model || "—"} · ${status.dimensions || 0}d · ${covBit} · ${envBits}${lastBit}`
      : `${actual} / ${status.model || "—"} · ${status.dimensions || 0}d · ${status.enabled === false ? "off" : "on"} · ${covBit} · ${envBits}${lastBit}`;
  }
}

function fillEmbeddingConnection(retrieval, status) {
  const account = document.querySelector("#embedding-account-id");
  const baseUrl = document.querySelector("#embedding-base-url");
  const apiKey = document.querySelector("#embedding-api-key");
  const hint = document.querySelector("#embedding-api-key-hint");
  const conn = status.connection || {};
  const env = status.env || {};
  if (account) account.value = retrieval.embedding_account_id || conn.account_id || "";
  if (baseUrl) baseUrl.value = retrieval.embedding_base_url || conn.base_url || "";
  if (apiKey) apiKey.value = "";
  syncEmbeddingConnectionFields();
  const providerId = document.querySelector("#embedding-provider")?.value || "auto";
  const keyReady = embeddingKeyIsSet(providerId, env);
  if (hint) hint.textContent = keyReady ? t("settings.embeddingKeySet") : t("settings.embeddingKeyMissing");
}

function embeddingKeyIsSet(providerId, env) {
  if (providerId === "cloudflare") return Boolean(env.cloudflare_token && env.cloudflare_account);
  if (providerId === "gemini") return Boolean(env.gemini_key);
  if (providerId === "openai") return Boolean(env.openai_key);
  if (providerId === "azure-openai") return Boolean(env.azure_key);
  if (providerId === "ollama") return Boolean(env.ollama_host);
  return providerId === "local" || providerId === "hash" || providerId === "auto";
}

function syncEmbeddingConnectionFields() {
  const providerId = document.querySelector("#embedding-provider")?.value || "auto";
  const accountWrap = document.querySelector("#embedding-account-wrap");
  const baseWrap = document.querySelector("#embedding-base-url-wrap");
  const keyWrap = document.querySelector("#embedding-api-key-wrap");
  const note = document.querySelector("#embedding-connection-note");
  const showAccount = providerId === "cloudflare";
  const showBase = ["openai", "azure-openai", "gemini", "ollama"].includes(providerId);
  const showKey = ["cloudflare", "gemini", "openai", "azure-openai"].includes(providerId);
  if (accountWrap) accountWrap.hidden = !showAccount;
  if (baseWrap) baseWrap.hidden = !showBase;
  if (keyWrap) keyWrap.hidden = !showKey;
  if (note) {
    if (providerId === "auto") note.textContent = t("settings.embeddingAutoNote");
    else if (providerId === "local") note.textContent = t("settings.embeddingLocalNote");
    else if (providerId === "hash") note.textContent = t("settings.embeddingHashNote");
    else if (providerId === "ollama") note.textContent = t("settings.embeddingOllamaNote");
    else note.textContent = t("settings.embeddingSecretNote");
  }
}

function refreshEmbeddingModelOptions(selectedModel = "") {
  const providerSelect = document.querySelector("#embedding-provider");
  const modelSelect = document.querySelector("#embedding-model");
  if (!modelSelect) return;
  const providerId = providerSelect?.value || "auto";
  const catalog = state.embeddingCatalog || [];
  let models = [];
  if (providerId === "auto") {
    models = catalog.flatMap(item => (item.models || []).map(model => ({ ...model, provider: item.id })));
  } else {
    const match = catalog.find(item => item.id === providerId);
    models = (match?.models || []).map(model => ({ ...model, provider: providerId }));
  }
  const current = selectedModel || modelSelect.value || "";
  modelSelect.innerHTML = "";
  if (!models.length) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = current || "(default)";
    modelSelect.appendChild(opt);
    return;
  }
  for (const model of models) {
    const opt = document.createElement("option");
    opt.value = model.id;
    opt.textContent = providerId === "auto" ? `${model.provider}: ${model.label || model.id}` : (model.label || model.id);
    modelSelect.appendChild(opt);
  }
  if (current && [...modelSelect.options].some(opt => opt.value === current)) {
    modelSelect.value = current;
  } else {
    modelSelect.selectedIndex = 0;
  }
  const selected = models.find(item => item.id === modelSelect.value);
  const dims = document.querySelector("#embedding-dimensions");
  if (dims && selected?.default_dims && !selectedModel) {
    dims.value = selected.default_dims;
  }
}

function readEmbeddingsForm() {
  return {
    embeddings_enabled: document.querySelector("#embeddings-enabled")?.checked ?? true,
    embedding_provider: document.querySelector("#embedding-provider")?.value || "auto",
    embedding_model: document.querySelector("#embedding-model")?.value || "",
    embedding_dimensions: Number(document.querySelector("#embedding-dimensions")?.value || 1024),
    embedding_account_id: document.querySelector("#embedding-account-id")?.value || "",
    embedding_base_url: document.querySelector("#embedding-base-url")?.value || "",
    vector_pool: Number(document.querySelector("#vector-pool")?.value || 64),
    vector_min_score: Number(document.querySelector("#vector-min-score")?.value || 0.22),
    search_relevance_floor: Number(document.querySelector("#search-relevance-floor")?.value ?? 0.28),
    vector_query_timeout_ms: Number(document.querySelector("#vector-query-timeout")?.value || 2500),
    reindex_on_startup: document.querySelector("#reindex-on-startup")?.checked ?? false,
  };
}

function embeddingSecretPayload() {
  const apiKey = document.querySelector("#embedding-api-key")?.value || "";
  return apiKey.trim() ? { api_key: apiKey.trim() } : {};
}

async function saveEmbeddingsSettings() {
  const payload = { ...readEmbeddingsForm(), ...embeddingSecretPayload() };
  const settings = await api("/api/settings", {
    method: "PATCH",
    body: JSON.stringify({ memory_retrieval: payload }),
  });
  fillEmbeddingsSettings(settings);
  const keyInput = document.querySelector("#embedding-api-key");
  if (keyInput) keyInput.value = "";
  showSnackbar(t("settings.embeddingsSaved"), "success");
}

async function testEmbeddingsConnection() {
  const resultEl = document.querySelector("#embeddings-test-result");
  const button = document.querySelector("#embeddings-test");
  if (resultEl) resultEl.textContent = t("settings.embeddingTesting");
  if (button) button.disabled = true;
  try {
    const payload = {
      ...readEmbeddingsForm(),
      ...embeddingSecretPayload(),
      save: true,
    };
    const result = await api("/api/memory/embeddings/test", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const keyInput = document.querySelector("#embedding-api-key");
    if (keyInput) keyInput.value = "";
    if (resultEl) {
      resultEl.textContent = result.message || (result.ready ? "ok" : "failed");
      resultEl.className = `provider-test ${result.ready ? "ok" : "error"}`;
    }
    showSnackbar(result.message || (result.ready ? t("settings.embeddingTestOk") : t("settings.embeddingTestFail")), result.ready ? "success" : "error");
    const settings = await api("/api/settings");
    fillEmbeddingsSettings(settings);
  } catch (error) {
    if (resultEl) {
      resultEl.textContent = error.message || t("settings.embeddingTestFail");
      resultEl.className = "provider-test error";
    }
    throw error;
  } finally {
    if (button) button.disabled = false;
  }
}

function vectorRuntimeBadgeLabel(report) {
  const current = report.current || {};
  if (report.ok || current.loaded) return t("settings.vectorRuntimeReady");
  if (current.reason === "not-installed" && current.load_extension) return t("settings.vectorRuntimeMissing");
  if (current.load_extension === false) return t("settings.vectorRuntimeDisabled");
  return t("settings.vectorRuntimeFallback");
}

function fillVectorRuntime(report) {
  lastVectorRuntime = report || lastVectorRuntime;
  const current = (report && report.current) || {};
  const repair = (report && report.repair) || {};
  const badge = document.querySelector("#vector-runtime-badge");
  const pythonEl = document.querySelector("#vector-runtime-python");
  const summary = document.querySelector("#vector-runtime-summary");
  const command = document.querySelector("#vector-runtime-command");
  const fixBtn = document.querySelector("#vector-runtime-fix");
  if (badge) {
    const ready = Boolean(report && (report.ok || current.loaded));
    badge.className = `provider-status ${ready ? "ready" : (repair.fixable || repair.needs_check ? "planned" : "error")}`;
    badge.textContent = vectorRuntimeBadgeLabel(report || {});
  }
  const hint = document.querySelector("#vector-runtime-hint");
  const frozenHint = document.querySelector("#vector-runtime-frozen");
  if (hint) hint.hidden = Boolean(current.frozen);
  if (frozenHint) frozenHint.hidden = !current.frozen;
  if (pythonEl) pythonEl.textContent = current.executable || current.python || "";
  if (summary) {
    if (repair.reason === "pip-missing") summary.textContent = t("settings.vectorRuntimeNoPip");
    else if (repair.reason === "bootstrap-pip") summary.textContent = t("settings.vectorRuntimeBootstrap");
    else summary.textContent = repair.summary || "";
  }
  if (command) {
    const preview = repair.command_preview || "";
    command.hidden = !preview;
    command.textContent = preview;
  }
  if (fixBtn) {
    const canFixNow = Boolean(repair.fixable);
    const canDiscover = Boolean(repair.needs_check);
    const hideFix = Boolean(current.frozen) || (!canFixNow && !canDiscover && !current.loaded);
    fixBtn.hidden = hideFix;
    fixBtn.disabled = hideFix || !(canFixNow || canDiscover);
    if (fixBtn.getAttribute("aria-busy") !== "true") {
      fixBtn.textContent = t("settings.vectorRuntimeFix");
    }
  }
}

async function checkVectorRuntime() {
  const checkBtn = document.querySelector("#vector-runtime-check");
  const summary = document.querySelector("#vector-runtime-summary");
  if (checkBtn) checkBtn.disabled = true;
  if (summary) summary.textContent = t("settings.vectorRuntimeChecking");
  try {
    const report = await api("/api/settings/vector-runtime");
    fillVectorRuntime(report);
    return report;
  } finally {
    if (checkBtn) checkBtn.disabled = false;
  }
}

function vectorRuntimeRepairError(result) {
  if (result?.error) return String(result.error);
  const failed = (result?.steps || []).find(step => !step.ok);
  const raw = String((failed && (failed.error || failed.stderr || failed.stdout)) || "");
  if (/No module named pip/i.test(raw)) return t("settings.vectorRuntimeNoPip");
  const line = raw.trim().split(/\r?\n/).filter(Boolean).pop() || t("settings.vectorRuntimeNotFixable");
  return line.slice(0, 280);
}

async function repairVectorRuntime() {
  let report = lastVectorRuntime;
  if (!report || !report.repair || report.repair.needs_check || !report.repair.action) {
    report = await checkVectorRuntime();
  }
  const repair = (report && report.repair) || {};
  if (!repair.fixable || !repair.action) {
    showSnackbar(repair.reason === "pip-missing" ? t("settings.vectorRuntimeNoPip") : t("settings.vectorRuntimeNotFixable"), "error");
    return;
  }
  const message = repair.bootstrap_pip
    ? `${t("settings.vectorRuntimeConfirm")} ${t("settings.vectorRuntimeBootstrap")}`
    : t("settings.vectorRuntimeConfirm");
  const ok = await showAppConfirm({
    title: t("settings.vectorRuntime"),
    message,
    detail: repair.command_preview || "",
    confirmLabel: t("settings.vectorRuntimeFix"),
  });
  if (!ok) return;
  const fixBtn = document.querySelector("#vector-runtime-fix");
  const checkBtn = document.querySelector("#vector-runtime-check");
  const summary = document.querySelector("#vector-runtime-summary");
  const fixLabel = t("settings.vectorRuntimeFix");
  if (fixBtn) {
    fixBtn.disabled = true;
    fixBtn.setAttribute("aria-busy", "true");
    fixBtn.textContent = t("settings.vectorRuntimeFixing");
  }
  if (checkBtn) checkBtn.disabled = true;
  if (summary) summary.textContent = t("settings.vectorRuntimeFixing");
  try {
    const result = await api("/api/settings/vector-runtime/repair", {
      method: "POST",
      body: JSON.stringify({ confirm: true, action: repair.action }),
    });
    fillVectorRuntime(result.status || result);
    if (!result.ok) {
      showSnackbar(vectorRuntimeRepairError(result), "error");
      return;
    }
    if (result.restart_required && result.restart_command) {
      showSnackbar(`${t("settings.vectorRuntimeRestart")} ${result.restart_command}`, "success");
    } else {
      showSnackbar(t("settings.vectorRuntimeFixed"), "success");
    }
  } finally {
    if (fixBtn) {
      fixBtn.removeAttribute("aria-busy");
      fixBtn.textContent = fixLabel;
    }
    if (checkBtn) checkBtn.disabled = false;
    const latest = lastVectorRuntime && lastVectorRuntime.repair;
    if (fixBtn) {
      const canFixNow = Boolean(latest && latest.fixable);
      const canDiscover = Boolean(latest && latest.needs_check);
      fixBtn.disabled = !(canFixNow || canDiscover);
    }
  }
}

async function rebuildEmbeddingsIndex() {
  await saveEmbeddingsSettings();
  const result = await api("/api/memory/embeddings/rebuild", {
    method: "POST",
    body: JSON.stringify({ clear: true, async: true }),
  });
  const statusEl = document.querySelector("#embeddings-status");
  if (statusEl) {
    statusEl.textContent = `Rebuild started · ${result.provider || "?"} / ${result.model || "?"} (async)`;
  }
}

async function runSecurityPreview() {
  const input = document.querySelector("#security-preview-input");
  const result = document.querySelector("#security-preview-result");
  if (!input || !result) return;
  const payload = await api("/api/security/preview", { method: "POST", body: JSON.stringify({ text: input.value }) });
  const findings = (payload.findings || []).map(item => `<span class="badge">${escapeHtml(item.kind)} x${escapeHtml(String(item.count))}</span>`).join("");
  result.innerHTML = `<article class="result"><strong>${payload.redacted ? "Redaction applied" : "No sensitive patterns"}</strong><p>${escapeHtml(payload.text || "")}</p>${findings}</article>`;
}
const ROUTER_PRESETS = {
  cheap: { quality: 0.15, cost: 0.55, speed: 0.2, availability: 0.1 },
  fast: { quality: 0.2, cost: 0.15, speed: 0.55, availability: 0.1 },
  quality: { quality: 0.55, cost: 0.15, speed: 0.2, availability: 0.1 },
  balanced: { quality: 0.4, cost: 0.3, speed: 0.2, availability: 0.1 },
};
function routerWeightInputs() {
  return {
    quality: document.querySelector("#router-w-quality"),
    cost: document.querySelector("#router-w-cost"),
    speed: document.querySelector("#router-w-speed"),
    availability: document.querySelector("#router-w-availability"),
  };
}
function setRouterWeights(weights, strategy) {
  const inputs = routerWeightInputs();
  for (const [key, input] of Object.entries(inputs)) {
    if (!input) continue;
    const value = Number(weights?.[key] ?? ROUTER_PRESETS.balanced[key]);
    input.value = String(Math.round(value * 100));
  }
  if (strategy) {
    const select = document.querySelector("#router-strategy");
    if (select) select.value = strategy;
  }
  syncRouterWeightLabels();
}
function readRouterWeightsRaw() {
  const inputs = routerWeightInputs();
  return {
    quality: Number(inputs.quality?.value || 0) / 100,
    cost: Number(inputs.cost?.value || 0) / 100,
    speed: Number(inputs.speed?.value || 0) / 100,
    availability: Number(inputs.availability?.value || 0) / 100,
  };
}
function normalizeRouterWeights(weights) {
  const total = Object.values(weights).reduce((sum, value) => sum + Number(value || 0), 0) || 1;
  return Object.fromEntries(Object.entries(weights).map(([key, value]) => [key, Number(value || 0) / total]));
}
function syncRouterWeightLabels() {
  const raw = readRouterWeightsRaw();
  const sum = Object.values(raw).reduce((a, b) => a + b, 0);
  for (const key of Object.keys(raw)) {
    const label = document.querySelector(`#router-w-${key}-val`);
    if (label) label.textContent = raw[key].toFixed(2);
  }
  const sumEl = document.querySelector("#router-weight-sum");
  if (sumEl) sumEl.textContent = sum.toFixed(2);
  document.querySelectorAll("[data-router-preset]").forEach(btn => {
    const preset = ROUTER_PRESETS[btn.dataset.routerPreset];
    const active = preset && Object.keys(preset).every(key => Math.abs(preset[key] - raw[key]) < 0.03);
    btn.classList.toggle("active", Boolean(active));
  });
}
async function loadRouterSettings() {
  const payload = await api("/api/router/settings");
  const router = payload.router || {};
  setRouterWeights(router.weights || ROUTER_PRESETS.balanced, router.strategy || "balanced");
}
async function saveRouterSettings() {
  const weights = normalizeRouterWeights(readRouterWeightsRaw());
  setRouterWeights(weights);
  const body = {
    strategy: document.querySelector("#router-strategy").value,
    weights,
  };
  await api("/api/router/settings", { method: "PATCH", body: JSON.stringify(body) });
  await previewRouting(document.querySelector("#router-preview-query").value || "");
  showSnackbar("Router settings saved.", "success");
}
async function previewRouting(query) {
  const container = document.querySelector("#router-preview");
  if (!container) return;
  const payload = await api("/api/router/preview", { method: "POST", body: JSON.stringify({ message: query, project_id: state.projectId }) });
  const decision = payload.decision || {};
  const ranked = (decision.ranked || []).map(item => `<article class="result${item.provider_id === decision.selected ? " selected" : ""}"><div class="row"><strong>${escapeHtml(item.label)}</strong><span class="badge">score ${escapeHtml(String(item.score))}</span></div><span class="badge">quality ${escapeHtml(String(item.quality))}</span><span class="badge">cost ${escapeHtml(String(item.cost))}</span><span class="badge">latency ${escapeHtml(String(item.latency))}</span><span class="badge">${item.availability > 0 ? "available" : "offline"}</span>${item.matches_role ? '<span class="badge">role match</span>' : ""}</article>`).join("");
  container.innerHTML = `<article class="result"><strong>Role: ${escapeHtml(payload.role || "auto")} · Strategy: ${escapeHtml(decision.strategy || "")}</strong><p>${escapeHtml(decision.reason || "")}</p></article>${ranked}`;
}

function fillIntegrationCredentials(integrations) {
  const ado = integrations.azure_devops || {};
  const org = document.querySelector("#settings-ado-org");
  const token = document.querySelector("#settings-ado-token");
  const adoHint = document.querySelector("#settings-ado-token-hint");
  const adoBadge = document.querySelector("#settings-ado-badge");
  if (org) org.value = ado.org || "";
  if (token) token.value = "";
  if (adoHint) adoHint.textContent = ado.token_set ? t("settings.secretSet") : t("settings.secretMissing");
  if (adoBadge) {
    adoBadge.textContent = ado.ready ? t("settings.integrationReady") : t("settings.integrationMissing");
    adoBadge.dataset.tone = ado.ready ? "ok" : "idle";
  }
}

async function saveIntegrationCredentials() {
  const token = document.querySelector("#settings-ado-token")?.value || "";
  const payload = {
    kind: "azure_devops",
    org: document.querySelector("#settings-ado-org")?.value || "",
  };
  if (token.trim()) payload.token = token.trim();
  const result = await api("/api/integrations/credentials", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  fillIntegrationCredentials(result);
  showSnackbar(result.message || t("settings.ado.saved"), result.azure_devops?.ready ? "success" : "info");
}

async function wipeProjectMemory() {
  const project = currentProject();
  const projectId = state.projectId || project?.id || "";
  if (!projectId) {
    showSnackbar(t("settings.wipe.noProject"), "error");
    return;
  }
  const projectName = displayProjectName(project) || project?.name || projectId;
  const preview = await api("/api/memory/wipe", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, confirm: false }),
  });
  const typed = await showAppPrompt({
    title: t("settings.wipe.title"),
    message: `${t("settings.wipe.prompt")} ${t("settings.wipe.counts")
      .replace("{nodes}", String(preview.nodes || 0))
      .replace("{candidates}", String(preview.candidates || 0))
      .replace("{edges}", String(preview.edges || 0))}`,
    label: t("settings.wipe.typeLabel").replace("{name}", projectName),
    placeholder: projectName,
    confirmLabel: t("settings.wipe.action"),
  });
  if (typed == null) return;
  if (String(typed).trim() !== String(projectName).trim()) {
    showSnackbar(t("settings.wipe.nameMismatch"), "error");
    return;
  }
  const result = await api("/api/memory/wipe", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, confirm: true }),
  });
  const counts = t("settings.wipe.counts")
    .replace("{nodes}", String(result.nodes || 0))
    .replace("{candidates}", String(result.candidates || 0))
    .replace("{edges}", String(result.edges || 0));
  showSnackbar(`${t("settings.wipe.done")} ${counts}`, "success");
  try {
    const { refreshMemorySurfaces } = await import("./memory-ingest.js");
    await refreshMemorySurfaces();
  } catch (_err) {
    /* ignore */
  }
}

export {
  ROUTER_PRESETS, checkVectorRuntime, fillEmbeddingsSettings, fillIntegrationCredentials, loadRouterSettings, loadSettings, previewRouting,
  rebuildEmbeddingsIndex, refreshEmbeddingModelOptions, repairVectorRuntime, runSecurityPreview,
  saveEmbeddingsSettings, saveIntegrationCredentials, saveRouterSettings, setRouterWeights, syncEmbeddingConnectionFields, syncRouterWeightLabels,
  testEmbeddingsConnection, wipeProjectMemory,
};
