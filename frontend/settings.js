/* Settings, embeddings, router — extracted from app.js */
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
  const lifecycleEnabled = document.querySelector("#lifecycle-enabled");
  const refreshOnAccess = document.querySelector("#refresh-on-access");
  const autoRescanOnStartup = document.querySelector("#auto-rescan-on-startup");
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
  applyTheme(theme);
  applyDensity(ui.density || "comfortable");
  applyLanguage(ui.language || state.language || "en");
  if (lifecycleEnabled) lifecycleEnabled.checked = lifecycle.enabled !== false;
  if (refreshOnAccess) refreshOnAccess.checked = lifecycle.refresh_on_access !== false;
  if (autoRescanOnStartup) autoRescanOnStartup.checked = lifecycle.auto_rescan_on_startup !== false;
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
    statusEl.textContent = `${status.provider || "—"} / ${status.model || "—"} · ${status.dimensions || 0}d · ${status.enabled === false ? "off" : "on"} · ${covBit} · ${envBits}`;
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
    vector_pool: Number(document.querySelector("#vector-pool")?.value || 64),
    vector_min_score: Number(document.querySelector("#vector-min-score")?.value || 0.22),
    search_relevance_floor: Number(document.querySelector("#search-relevance-floor")?.value ?? 0.28),
    vector_query_timeout_ms: Number(document.querySelector("#vector-query-timeout")?.value || 2500),
    reindex_on_startup: document.querySelector("#reindex-on-startup")?.checked ?? false,
  };
}

async function saveEmbeddingsSettings() {
  const payload = readEmbeddingsForm();
  const settings = await api("/api/settings", {
    method: "PATCH",
    body: JSON.stringify({ memory_retrieval: payload }),
  });
  fillEmbeddingsSettings(settings);
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
