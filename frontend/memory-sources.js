/* Dynamic memory source registry UI */
import { api } from "./api-client.js";
import { showAppConfirm } from "./app-dialog.js";
import { sourceBrandIcon } from "./brand-icons.js";
import { escapeHtml, showSnackbar } from "./dom-utils.js";
import { state, t } from "./state.js";

const DRIVER_ICONS = {
  local_files: "📚",
  local_git: "🔀",
  chat: "💬",
  mcp: "🔌",
  http: "🌐",
  ftp: "📁",
};

const LOCAL_KIND_ORDER = ["docs", "adr", "code", "issues", "pull_requests", "meetings", "inbox"];
const LOCAL_KIND_LABELS = {
  docs: "docs",
  adr: "ADRs",
  code: "code",
  issues: "issues",
  pull_requests: "PRs",
  meetings: "meetings",
  inbox: "inbox",
};

/** Human blurbs for built-in bindings (keyed like backend DEFAULT_SOURCE_TITLES). */
const PRESET_SOURCE_BLURBS = {
  "chat:chat": "Captures durable facts from Ask conversations into the review queue.",
  "local_git:git_history": "Indexes commits from this project's local git history on disk.",
  "azure-boards:issues": "Work items from Azure DevOps (Boards MCP).",
  "azure-git:pull_requests": "Pull requests from Azure Repos (Git MCP).",
  "azure-wiki:wiki": "Wiki pages from Azure DevOps (Wiki MCP).",
  "github:issues": "Issues from linked GitHub repositories (GitHub MCP).",
  "github:pull_requests": "Pull requests from linked GitHub repositories (GitHub MCP).",
  "gitlab:issues": "Issues from linked GitLab projects (GitLab MCP).",
  "gitlab:pull_requests": "Merge requests from linked GitLab projects (GitLab MCP).",
  "gitlab:wiki": "Wiki pages from linked GitLab projects (GitLab MCP).",
  "granola:meetings": "Meeting notes synced from Granola via MCP.",
};

function sourceBlurb(source) {
  const cfg = source.config || {};
  const driver = String(cfg.driver || "").trim();
  const adapter = String(cfg.adapter || "").trim();
  const server = String(cfg.mcp_server_id || "").trim();
  const kind = String(source.kind || "").trim();
  for (const key of [`${adapter}:${kind}`, `${server}:${kind}`, `${driver}:${kind}`]) {
    if (key && PRESET_SOURCE_BLURBS[key]) return PRESET_SOURCE_BLURBS[key];
  }
  if (driver === "chat") return PRESET_SOURCE_BLURBS["chat:chat"];
  if (driver === "local_git") return PRESET_SOURCE_BLURBS["local_git:git_history"];
  if (driver === "http" && cfg.http?.url) return `HTTP feed · ${cfg.http.url}`;
  if (driver === "ftp" && cfg.ftp?.url) {
    const via = cfg.ftp.private_key ? "key auth" : "password";
    return `FTP/SFTP · ${cfg.ftp.url} · ${via}`;
  }
  if (driver === "mcp") {
    const via = adapter || server || "MCP";
    return `Custom MCP binding · ${kind || "items"} via ${via}`;
  }
  const bits = [kind, driver].filter(Boolean);
  if (adapter) bits.push(adapter);
  return bits.join(" · ") || "Custom source";
}

let cachedSources = [];
let editingSource = null;

const DRIVER_LABELS = {
  http: "HTTP / HTTPS",
  ftp: "FTP / SFTP",
  mcp: "MCP (discover tools)",
  chat: "Ask",
  local_git: "Local Git",
  local_files: "Local files",
};

const CREATE_DRIVER_OPTIONS = [
  ["http", "HTTP / HTTPS"],
  ["ftp", "FTP / SFTP"],
  ["mcp", "MCP (discover tools)"],
];

const CREATE_KIND_OPTIONS = ["docs", "wiki", "meetings", "issues", "pull_requests", "inbox"];

const PRESET_MCP_ADAPTERS = new Set([
  "azure-boards", "azure-git", "azure-wiki", "github", "gitlab", "granola",
]);

const AZURE_ADAPTERS = new Set(["azure-boards", "azure-git", "azure-wiki"]);

function isAzureSource(source) {
  return AZURE_ADAPTERS.has(String((source?.config || {}).adapter || ""));
}

function isRemovableSource(source) {
  const cfg = source?.config || {};
  const driver = String(cfg.driver || "");
  if (driver === "http" || driver === "ftp") return true;
  if (driver === "mcp" && !PRESET_MCP_ADAPTERS.has(String(cfg.adapter || ""))) return true;
  return false;
}

export async function loadProjectSources() {
  const projectId = state.projectId || "architectos";
  const payload = await api(`/api/sources?project_id=${encodeURIComponent(projectId)}`);
  cachedSources = Array.isArray(payload.sources) ? payload.sources : [];
  renderSourceGrid(cachedSources);
  renderMemorySourcesSettings(cachedSources);
  return cachedSources;
}

function sourceSubtitle(source) {
  const cfg = source.config || {};
  const adapter = cfg.adapter || "";
  if (adapter) return adapter;
  if (cfg.http?.url) return cfg.http.url;
  if (cfg.ftp?.url) return cfg.ftp.url;
  return String(cfg.driver || "local").replace("_", " ");
}

function formatWhen(raw) {
  const text = String(raw || "").trim();
  if (!text) return "—";
  try {
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch (_error) {
    return text;
  }
}

function sourceDriver(source) {
  return String((source.config || {}).driver || "");
}

function isLocalFilesSource(source) {
  return sourceDriver(source) === "local_files";
}

function sourceIsEnabled(source) {
  return Boolean((source.config || {}).enabled);
}

function localFilesSources(sources = cachedSources) {
  const byKind = new Map();
  for (const source of sources.filter(isLocalFilesSource)) {
    const kind = String(source.kind || "");
    if (!kind) continue;
    if (!byKind.has(kind)) byKind.set(kind, source);
  }
  return LOCAL_KIND_ORDER.map(kind => byKind.get(kind)).filter(Boolean);
}

function otherSources(sources = cachedSources) {
  return sources.filter(source => !isLocalFilesSource(source));
}

function renderSourceGrid(sources) {
  const grid = document.querySelector("#source-grid");
  if (!grid) return;
  const locals = localFilesSources(sources).filter(sourceIsEnabled);
  const others = otherSources(sources).filter(sourceIsEnabled);
  if (!locals.length && !others.length) {
    const configured = sources.length > 0;
    grid.innerHTML = configured
      ? `<p class="autoscan-help-text">No enabled sources. Turn them on in Settings → Sources.</p>`
      : `<p class="autoscan-help-text">No sources configured yet.</p>`;
    grid.dispatchEvent(new CustomEvent("aos:sources-rendered"));
    return;
  }
  const parts = [];
  if (locals.length) {
    const meta = locals
      .map(item => LOCAL_KIND_LABELS[item.kind] || item.kind)
      .join(" · ");
    parts.push(`
      <label class="source-checkbox" title="Local project files — one root, kinds below are toggled in Settings">
        <input class="ingest-source" type="checkbox" value="__local_files__" data-group="local_files" checked>
        <span class="source-icon">📚</span>
        <span class="source-label">Local files</span>
        <span class="source-meta">${escapeHtml(meta || "docs")}</span>
      </label>`);
  }
  for (const source of others) {
    const cfg = source.config || {};
    const icon = sourceBrandIcon(source, DRIVER_ICONS[cfg.driver] || "📌");
    const tested = cfg.last_test_ok === true ? " · tested" : cfg.last_test_ok === false ? " · test failed" : "";
    parts.push(`
      <label class="source-checkbox" title="${escapeHtml(sourceBlurb(source))}" data-adapter="${escapeHtml(cfg.adapter || "")}" data-mcp-server="${escapeHtml(cfg.mcp_server_id || "")}">
        <input class="ingest-source" type="checkbox" value="${escapeHtml(source.id)}" data-adapter="${escapeHtml(cfg.adapter || "")}" data-mcp-server="${escapeHtml(cfg.mcp_server_id || "")}" checked>
        <span class="source-icon">${icon}</span>
        <span class="source-label">${escapeHtml(source.name || source.kind)}</span>
        <span class="source-meta">${escapeHtml(String(source.kind || "").replaceAll("_", " "))}${escapeHtml(tested)}</span>
      </label>`);
  }
  grid.innerHTML = parts.join("");
  grid.dispatchEvent(new CustomEvent("aos:sources-rendered"));
}

export function selectedSourceIds() {
  const selected = [];
  for (const input of document.querySelectorAll(".ingest-source:checked")) {
    const value = input.value;
    if (value === "__local_files__") {
      for (const source of localFilesSources()) {
        if (sourceIsEnabled(source)) selected.push(source.id);
      }
      continue;
    }
    if (value) selected.push(value);
  }
  return [...new Set(selected)];
}

export async function testSource(sourceId) {
  const id = String(sourceId || "").trim();
  if (!id) throw new Error("Source id is missing");
  return api(`/api/sources/${encodeURIComponent(id)}/test`, { method: "POST", body: "{}" });
}

export async function discoverMcpSources(serverId) {
  return api("/api/sources/discover", {
    method: "POST",
    body: JSON.stringify({ project_id: state.projectId, server_id: serverId }),
  });
}

export async function saveSourcePatch(sourceId, patch) {
  const id = String(sourceId || "").trim();
  if (!id || id === "undefined" || id === "null") {
    throw new Error("Source id is missing — reload Settings and try again");
  }
  return api(`/api/sources/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export async function createProjectSource(payload) {
  return api("/api/sources", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function toggleAllSources(checked) {
  document.querySelectorAll(".ingest-source").forEach(input => {
    input.checked = Boolean(checked);
  });
}

function renderLocalFilesGroup(locals) {
  const anyEnabled = locals.some(item => (item.config || {}).enabled);
  const interval = Number((locals.find(item => (item.config || {}).enabled) || locals[0])?.config?.schedule?.interval_minutes || 60);
  const lastRun = locals
    .map(item => item.config?.last_run_at || item.config?.last_test_at || "")
    .filter(Boolean)
    .sort()
    .at(-1);
  const testProbe = locals.find(item => item.kind === "docs") || locals[0];
  const kinds = locals.map(source => {
    const enabled = Boolean((source.config || {}).enabled);
    const label = LOCAL_KIND_LABELS[source.kind] || source.kind;
    return `<label class="local-kind-chip">
      <input type="checkbox" class="local-kind-input" data-source-id="${escapeHtml(source.id)}" ${enabled ? "checked" : ""}>
      <span>${escapeHtml(label)}</span>
    </label>`;
  }).join("");

  return `
    <article class="memory-source-row memory-source-group" data-local-group="1">
      <div class="memory-source-main">
        <span class="memory-source-icon" aria-hidden="true">📚</span>
        <div class="memory-source-copy">
          <strong class="memory-source-title">Local project files</strong>
          <p class="memory-source-meta">One project root · pick which file kinds to scan</p>
          <p class="memory-source-when">Last run ${escapeHtml(formatWhen(lastRun))}</p>
          <div class="local-kind-chips">${kinds}</div>
        </div>
      </div>
      <div class="memory-source-controls">
        <label class="memory-source-enable setting-toggle-mini">
          <input type="checkbox" class="local-group-enabled" ${anyEnabled ? "checked" : ""}>
          <span>On</span>
        </label>
        <label class="memory-source-interval">
          <span>Every</span>
          <input type="number" min="5" class="local-group-interval" value="${interval}">
          <span>min</span>
        </label>
        <button type="button" class="btn-secondary btn-sm source-test-btn" data-source-id="${escapeHtml(testProbe?.id || "")}" title="Test connection">Test</button>
        <button type="button" class="btn-danger btn-sm local-group-clear" title="${escapeHtml(t("settings.source.clearLocalTitle"))}">${escapeHtml(t("settings.source.clear"))}</button>
      </div>
    </article>`;
}

function renderSingleSourceRow(source) {
  const cfg = source.config || {};
  const schedule = cfg.schedule || {};
  const driver = cfg.driver || "";
  const icon = sourceBrandIcon(source, DRIVER_ICONS[driver] || "📌");
  const enabled = Boolean(cfg.enabled);
  const testOk = cfg.last_test_ok;
  const statusClass = testOk === true ? "is-ok" : testOk === false ? "is-fail" : "";
  const statusLabel = testOk === true ? "OK" : testOk === false ? "Failed" : "";
  const statusHtml = statusLabel
    ? `<span class="source-test-status ${statusClass}" aria-live="polite">${statusLabel}</span>`
    : `<span class="source-test-status" aria-live="polite" hidden></span>`;
  const actionLabel = testOk === false ? "Retest" : "Test";
  return `
    <article class="memory-source-row" data-source-id="${escapeHtml(source.id)}">
      <div class="memory-source-main">
        <span class="memory-source-icon" aria-hidden="true">${icon}</span>
        <div class="memory-source-copy">
          <button type="button" class="memory-source-name source-open-edit" data-source-id="${escapeHtml(source.id)}" title="Edit source">${escapeHtml(source.name || "Untitled")}</button>
          <p class="memory-source-meta">${escapeHtml(sourceBlurb(source))}</p>
          <p class="memory-source-when">Last run ${escapeHtml(formatWhen(cfg.last_run_at || cfg.last_test_at))}</p>
        </div>
      </div>
      <div class="memory-source-controls">
        <label class="memory-source-enable setting-toggle-mini">
          <input type="checkbox" class="source-enabled-input" data-source-id="${escapeHtml(source.id)}" ${enabled ? "checked" : ""}>
          <span>On</span>
        </label>
        <label class="memory-source-interval">
          <span>Every</span>
          <input type="number" min="5" class="source-interval-input" data-source-id="${escapeHtml(source.id)}" value="${Number(schedule.interval_minutes || 60)}">
          <span>min</span>
        </label>
        ${statusHtml}
        <button type="button" class="btn-secondary btn-sm source-open-edit" data-source-id="${escapeHtml(source.id)}">Edit</button>
        <button type="button" class="btn-secondary btn-sm source-test-btn" data-source-id="${escapeHtml(source.id)}" title="Test connection">${actionLabel}</button>
        <button type="button" class="btn-danger btn-sm source-clear-btn" data-source-id="${escapeHtml(source.id)}" title="${escapeHtml(t("settings.source.clearTitle"))}">${escapeHtml(t("settings.source.clear"))}</button>
      </div>
    </article>`;
}

export function renderMemorySourcesSettings(sources) {
  const host = document.querySelector("#memory-sources-table");
  if (!host) return;
  const locals = localFilesSources(sources);
  const others = otherSources(sources);
  if (!locals.length && !others.length) {
    host.innerHTML = `<p class="settings-section-desc">No project sources yet. Click Add source to connect HTTP, FTP/SFTP, or discover an MCP server.</p>`;
    return;
  }

  const html = [];
  if (locals.length) html.push(renderLocalFilesGroup(locals));
  for (const source of others) html.push(renderSingleSourceRow(source));
  host.innerHTML = html.join("");

  const group = host.querySelector("[data-local-group]");
  if (group) {
    const master = group.querySelector(".local-group-enabled");
    const chips = [...group.querySelectorAll(".local-kind-input")];
    master?.addEventListener("change", () => {
      const on = Boolean(master.checked);
      chips.forEach(chip => {
        // Master On restores docs+adr if nothing selected; Off clears all.
        if (!on) chip.checked = false;
        else if (!chips.some(item => item.checked)) {
          const source = cachedSources.find(item => item.id === chip.dataset.sourceId);
          chip.checked = ["docs", "adr"].includes(String(source?.kind || ""));
        }
      });
      if (on && !chips.some(item => item.checked)) chips.forEach(chip => { chip.checked = true; });
    });
    chips.forEach(chip => {
      chip.addEventListener("change", () => {
        if (master) master.checked = chips.some(item => item.checked);
      });
    });
  }

  host.querySelectorAll(".source-open-edit").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = String(btn.dataset.sourceId || "").trim();
      if (id) openEditSourceModal(id);
    });
  });
  host.querySelector(".local-group-clear")?.addEventListener("click", () => {
    clearLocalFilesGroupMemory().catch(err => showSnackbar(err.message || String(err), "error"));
  });
  host.querySelectorAll(".source-clear-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = String(btn.dataset.sourceId || "").trim();
      if (id) clearSourceMemoryById(id).catch(err => showSnackbar(err.message || String(err), "error"));
    });
  });

  host.querySelectorAll(".source-test-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = String(btn.dataset.sourceId || "").trim();
      if (!id) {
        showSnackbar("Nothing to test — enable a local kind first", "error");
        return;
      }
      const row = btn.closest(".memory-source-row");
      let status = row?.querySelector(".source-test-status");
      if (row && !status) {
        status = document.createElement("span");
        status.className = "source-test-status";
        status.setAttribute("aria-live", "polite");
        btn.before(status);
      }
      btn.disabled = true;
      const prevLabel = btn.textContent;
      btn.textContent = "Testing…";
      try {
        const result = await testSource(id);
        if (status) {
          status.hidden = false;
          status.textContent = result.ok ? "OK" : "Failed";
          status.classList.toggle("is-ok", Boolean(result.ok));
          status.classList.toggle("is-fail", !result.ok);
        }
        btn.textContent = result.ok ? "Test" : "Retest";
        if (!result.ok) showSnackbar(result.error || "Test failed", "error");
      } catch (error) {
        if (status) {
          status.hidden = false;
          status.textContent = "Failed";
          status.classList.remove("is-ok");
          status.classList.add("is-fail");
        }
        btn.textContent = "Retest";
        showSnackbar(error.message || "Test failed", "error");
      } finally {
        btn.disabled = false;
        if (btn.textContent === "Testing…") btn.textContent = prevLabel || "Test";
      }
    });
  });

  // Enter inside bindings must not submit the parent Settings form.
  host.querySelectorAll("input").forEach(input => {
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
      }
    });
  });
}

export async function persistSourceSettingsFromTable() {
  const projectId = state.projectId || "architectos";
  const host = document.querySelector("#memory-sources-table");
  if (!host) return;

  const errors = [];
  const patchOne = async (id, patch, label) => {
    try {
      await saveSourcePatch(id, patch);
    } catch (error) {
      errors.push(`${label || id}: ${error?.message || error}`);
    }
  };

  const group = host.querySelector("[data-local-group]");
  if (group) {
    const interval = Number(group.querySelector(".local-group-interval")?.value || 60);
    const masterOn = Boolean(group.querySelector(".local-group-enabled")?.checked);
    for (const chip of group.querySelectorAll(".local-kind-input")) {
      const id = String(chip.dataset.sourceId || "").trim();
      const original = cachedSources.find(item => item.id === id);
      if (!id || !original?.kind) continue;
      const enabled = masterOn && Boolean(chip.checked);
      const cfg = { ...(original.config || {}) };
      cfg.enabled = enabled;
      cfg.schedule = { ...(cfg.schedule || {}), interval_minutes: interval, enabled };
      await patchOne(id, {
        project_id: original.project_id || projectId,
        kind: original.kind,
        config: cfg,
      }, original.name || original.kind);
    }
  }

  for (const row of host.querySelectorAll(".memory-source-row[data-source-id]")) {
    const id = String(row.dataset.sourceId || "").trim();
    if (!id) continue;
    const original = cachedSources.find(item => item.id === id);
    if (!original?.kind) {
      errors.push(`${id}: source not loaded — reload Settings`);
      continue;
    }
    const cfg = { ...(original.config || {}) };
    cfg.enabled = row.querySelector(".source-enabled-input")?.checked ?? cfg.enabled;
    cfg.schedule = {
      ...(cfg.schedule || {}),
      interval_minutes: Number(row.querySelector(".source-interval-input")?.value || 60),
      enabled: cfg.enabled,
    };
    await patchOne(id, {
      project_id: original.project_id || projectId,
      kind: original.kind,
      config: cfg,
    }, original.name || original.kind);
  }

  await loadProjectSources();
  if (errors.length) {
    const message = errors.length === 1 ? errors[0] : errors.slice(0, 3).join(" · ");
    showSnackbar(message, "error");
    return { ok: false, errors };
  }
  showSnackbar("Source settings saved", "success");
  return { ok: true };
}

function setAddSourceDriver(driver) {
  const http = document.querySelector("#add-source-http-fields");
  const ftp = document.querySelector("#add-source-ftp-fields");
  const mcp = document.querySelector("#add-source-mcp-fields");
  const ado = document.querySelector("#add-source-ado-fields");
  const azure = isAzureSource(editingSource);
  if (http) http.hidden = driver !== "http";
  if (ftp) ftp.hidden = driver !== "ftp";
  if (mcp) mcp.hidden = azure || driver !== "mcp";
  if (ado) ado.hidden = !azure;
  if (driver === "http") syncHttpFormVisibility();
}

function syncHttpFormVisibility() {
  const method = document.querySelector("#add-source-http-method")?.value || "GET";
  const bodyWrap = document.querySelector("#add-source-http-body-wrap");
  if (bodyWrap) bodyWrap.hidden = method === "GET" || method === "HEAD";
  const auth = document.querySelector("#add-source-http-auth")?.value || "none";
  const bearer = document.querySelector("#add-source-http-bearer-wrap");
  const basic = document.querySelector("#add-source-http-basic-wrap");
  if (bearer) bearer.hidden = auth !== "bearer";
  if (basic) basic.hidden = auth !== "basic";
}

function parseHeaderLines(raw) {
  const headers = {};
  for (const line of String(raw || "").split(/\r?\n/)) {
    const text = line.trim();
    if (!text || text.startsWith("#")) continue;
    const idx = text.indexOf(":");
    if (idx <= 0) continue;
    const key = text.slice(0, idx).trim();
    const value = text.slice(idx + 1).trim();
    if (key) headers[key] = value;
  }
  return headers;
}

function formatHeaderLines(headers) {
  return Object.entries(headers || {})
    .map(([key, value]) => `${key}: ${value}`)
    .join("\n");
}

/** Minimal curl tokenizer for common -X/-H/-d/-u/--data-raw forms. */
export function parseCurlCommand(input) {
  const raw = String(input || "").trim().replace(/\\\r?\n/g, " ");
  if (!raw) throw new Error("Paste a curl command first");
  if (!/\bcurl\b/i.test(raw)) throw new Error("Not a curl command");

  const tokens = [];
  const re = /'([^']*)'|"((?:\\.|[^"\\])*)"|(\S+)/g;
  let match;
  while ((match = re.exec(raw))) {
    if (match[1] != null) tokens.push(match[1]);
    else if (match[2] != null) tokens.push(match[2].replace(/\\"/g, '"').replace(/\\\\/g, "\\"));
    else tokens.push(match[3]);
  }

  let method = "";
  let url = "";
  const headers = {};
  let body = "";
  let username = "";
  let password = "";
  let bearer = "";
  let auth = "none";

  for (let i = 0; i < tokens.length; i += 1) {
    const tok = tokens[i];
    if (tok === "curl" || tok.startsWith("curl")) continue;
    const next = () => {
      i += 1;
      return tokens[i];
    };
    if (tok === "-X" || tok === "--request") {
      method = String(next() || "").toUpperCase();
      continue;
    }
    if (tok === "-H" || tok === "--header") {
      const header = String(next() || "");
      const colon = header.indexOf(":");
      if (colon > 0) {
        const key = header.slice(0, colon).trim();
        const value = header.slice(colon + 1).trim();
        headers[key] = value;
        if (key.toLowerCase() === "authorization") {
          const m = value.match(/^Bearer\s+(.+)$/i);
          if (m) {
            auth = "bearer";
            bearer = m[1].trim();
            delete headers[key];
          }
        }
      }
      continue;
    }
    if (tok === "-d" || tok === "--data" || tok === "--data-raw" || tok === "--data-binary") {
      body = String(next() || "");
      if (!method) method = "POST";
      continue;
    }
    if (tok === "-u" || tok === "--user") {
      const cred = String(next() || "");
      const split = cred.indexOf(":");
      username = split >= 0 ? cred.slice(0, split) : cred;
      password = split >= 0 ? cred.slice(split + 1) : "";
      auth = "basic";
      continue;
    }
    if (tok.startsWith("-")) continue;
    if (/^https?:\/\//i.test(tok) || tok.startsWith("/") || tok.includes("://")) {
      url = tok;
    }
  }

  if (!url) throw new Error("Could not find URL in curl command");
  if (!method) method = body ? "POST" : "GET";
  return { method, url, headers, body, auth, bearer_token: bearer, username, password };
}

function applyCurlToHttpForm() {
  const raw = document.querySelector("#add-source-curl")?.value || "";
  const parsed = parseCurlCommand(raw);
  const method = document.querySelector("#add-source-http-method");
  const url = document.querySelector("#add-source-http-url");
  const headers = document.querySelector("#add-source-http-headers");
  const body = document.querySelector("#add-source-http-body");
  const auth = document.querySelector("#add-source-http-auth");
  const bearer = document.querySelector("#add-source-http-bearer");
  const user = document.querySelector("#add-source-http-user");
  const pass = document.querySelector("#add-source-http-pass");
  if (method) method.value = parsed.method || "GET";
  if (url) url.value = parsed.url || "";
  if (headers) headers.value = formatHeaderLines(parsed.headers);
  if (body) body.value = parsed.body || "";
  if (auth) auth.value = parsed.auth || "none";
  if (bearer) bearer.value = parsed.bearer_token || "";
  if (user) user.value = parsed.username || "";
  if (pass) pass.value = parsed.password || "";
  syncHttpFormVisibility();
  showSnackbar("cURL applied to form fields", "success");
}

function resetHttpFormFields() {
  const ids = [
    ["#add-source-curl", ""],
    ["#add-source-http-method", "GET"],
    ["#add-source-http-url", ""],
    ["#add-source-http-headers", ""],
    ["#add-source-http-body", ""],
    ["#add-source-http-auth", "none"],
    ["#add-source-http-bearer", ""],
    ["#add-source-http-user", ""],
    ["#add-source-http-pass", ""],
    ["#add-source-http-extract-mode", "auto"],
    ["#add-source-http-items-path", ""],
    ["#add-source-http-label-field", ""],
    ["#add-source-http-text-fields", ""],
  ];
  for (const [sel, value] of ids) {
    const el = document.querySelector(sel);
    if (el) el.value = value;
  }
  const preview = document.querySelector("#add-source-http-preview");
  if (preview) {
    preview.hidden = true;
    preview.innerHTML = "";
  }
  syncHttpFormVisibility();
}

function buildHttpConfigFromForm() {
  const url = (document.querySelector("#add-source-http-url")?.value || "").trim();
  if (!url) throw new Error("URL is required");
  const method = (document.querySelector("#add-source-http-method")?.value || "GET").toUpperCase();
  const headers = parseHeaderLines(document.querySelector("#add-source-http-headers")?.value || "");
  const body = document.querySelector("#add-source-http-body")?.value || "";
  const auth = document.querySelector("#add-source-http-auth")?.value || "none";
  const extractMode = document.querySelector("#add-source-http-extract-mode")?.value || "auto";
  const itemsPath = (document.querySelector("#add-source-http-items-path")?.value || "").trim();
  const labelField = (document.querySelector("#add-source-http-label-field")?.value || "").trim();
  const textFields = (document.querySelector("#add-source-http-text-fields")?.value || "")
    .split(",")
    .map(part => part.trim())
    .filter(Boolean);
  const http = {
    url,
    method,
    headers,
    auth,
    extract: {
      mode: extractMode,
      items_path: itemsPath,
      label_field: labelField,
      text_fields: textFields,
    },
  };
  if (method !== "GET" && method !== "HEAD" && body.trim()) http.body = body;
  if (auth === "bearer") {
    http.bearer_token = document.querySelector("#add-source-http-bearer")?.value || "";
  }
  if (auth === "basic") {
    http.username = (document.querySelector("#add-source-http-user")?.value || "").trim();
    http.password = document.querySelector("#add-source-http-pass")?.value || "";
  }
  return http;
}

function renderHttpProbePreview(result) {
  const host = document.querySelector("#add-source-http-preview");
  if (!host) return;
  const paths = Array.isArray(result.detected_paths) ? result.detected_paths.slice(0, 16) : [];
  const candidates = Array.isArray(result.preview_candidates) ? result.preview_candidates : [];
  const jsonPreview = String(result.json_preview || "").trim();
  if (!paths.length && !candidates.length && !jsonPreview) {
    host.hidden = true;
    host.innerHTML = "";
    return;
  }
  const pathChips = paths.length
    ? `<div class="add-source-path-chips">${paths.map(path => `<button type="button" class="local-kind-chip add-source-path-chip" data-path="${escapeHtml(path)}">${escapeHtml(path)}</button>`).join("")}</div>`
    : "";
  const candHtml = candidates.length
    ? `<div class="add-source-preview-cands">${candidates.map(item => `
        <article class="add-source-preview-cand">
          <strong>${escapeHtml(item.label || "item")}</strong>
          <pre>${escapeHtml(String(item.text || "").slice(0, 280))}</pre>
        </article>`).join("")}</div>`
    : "";
  const jsonHtml = jsonPreview
    ? `<details class="add-source-json-preview"><summary>JSON sample</summary><pre>${escapeHtml(jsonPreview.slice(0, 2500))}</pre></details>`
    : "";
  host.hidden = false;
  host.innerHTML = `
    <p class="field-hint">Detected paths — click to fill Items path or Label/Text fields.</p>
    ${pathChips}
    ${candHtml}
    ${jsonHtml}`;
  host.querySelectorAll(".add-source-path-chip").forEach(btn => {
    btn.addEventListener("click", () => {
      const path = btn.getAttribute("data-path") || "";
      const items = document.querySelector("#add-source-http-items-path");
      const label = document.querySelector("#add-source-http-label-field");
      const texts = document.querySelector("#add-source-http-text-fields");
      const mode = document.querySelector("#add-source-http-extract-mode");
      if (mode) mode.value = "json";
      if (!items?.value) {
        // Prefer parent array path when chip looks like parent.field
        const parts = path.split(".");
        if (parts.length >= 2 && items) items.value = parts.slice(0, -1).join(".");
        else if (items) items.value = path.replace(/\[0\]$/, "");
        if (label && parts.length) label.value = parts[parts.length - 1];
      } else if (label && !label.value) {
        label.value = path.split(".").pop() || path;
      } else if (texts) {
        const current = texts.value.split(",").map(s => s.trim()).filter(Boolean);
        const leaf = path.split(".").pop() || path;
        if (!current.includes(leaf)) texts.value = [...current, leaf].join(", ");
      }
    });
  });
}

function resetFtpKeyFields() {
  const file = document.querySelector("#add-source-ftp-key-file");
  const area = document.querySelector("#add-source-ftp-key");
  const name = document.querySelector("#add-source-ftp-key-name");
  const pass = document.querySelector("#add-source-ftp-key-pass");
  if (file) file.value = "";
  if (area) {
    area.value = "";
  }
  if (name) {
    name.hidden = true;
    name.textContent = "";
  }
  if (pass) pass.value = "";
}

async function readFtpPrivateKey() {
  const area = document.querySelector("#add-source-ftp-key");
  const pasted = String(area?.value || "").trim();
  if (pasted) return { pem: pasted, filename: "pasted-key" };
  const fileInput = document.querySelector("#add-source-ftp-key-file");
  const file = fileInput?.files?.[0];
  if (!file) return { pem: "", filename: "" };
  const pem = String(await file.text()).trim();
  if (!pem) throw new Error("Private key file is empty");
  if (!/BEGIN[\w\s]*PRIVATE KEY/i.test(pem) && !pem.startsWith("openssh-key-v1")) {
    if (pem.length < 32) throw new Error("Private key file does not look like a PEM/OpenSSH key");
  }
  return { pem, filename: file.name || "private-key" };
}

function bindFtpKeyFilePicker() {
  const file = document.querySelector("#add-source-ftp-key-file");
  if (!file || file.dataset.bound === "1") return;
  file.dataset.bound = "1";
  file.addEventListener("change", async () => {
    const name = document.querySelector("#add-source-ftp-key-name");
    const area = document.querySelector("#add-source-ftp-key");
    const picked = file.files?.[0];
    if (!picked) {
      if (name) {
        name.hidden = true;
        name.textContent = "";
      }
      return;
    }
    try {
      const pem = String(await picked.text()).trim();
      if (area) {
        area.value = pem;
      }
      if (name) {
        name.hidden = false;
        name.textContent = `Loaded: ${picked.name} (${Math.max(1, Math.round(picked.size / 1024))} KB)`;
      }
    } catch (error) {
      showSnackbar(error.message || "Could not read key file", "error");
      file.value = "";
    }
  });
}

async function fillMcpServerOptions(selectedId = "") {
  const select = document.querySelector("#add-source-mcp-server");
  if (!select) return;
  try {
    const payload = await api("/api/mcp/servers");
    const servers = Array.isArray(payload.servers) ? payload.servers : [];
    select.innerHTML = servers.length
      ? servers.map(server => `<option value="${escapeHtml(server.id)}">${escapeHtml(server.label || server.id)}</option>`).join("")
      : `<option value="">No MCP servers</option>`;
    if (selectedId && [...select.options].some(option => option.value === selectedId)) {
      select.value = selectedId;
    }
  } catch (_error) {
    select.innerHTML = `<option value="">Failed to load MCP servers</option>`;
  }
}

function resetDriverSelect(includeDriver = "") {
  const select = document.querySelector("#add-source-driver");
  if (!select) return;
  const options = CREATE_DRIVER_OPTIONS.slice();
  if (includeDriver && !options.some(([value]) => value === includeDriver)) {
    options.push([includeDriver, DRIVER_LABELS[includeDriver] || includeDriver]);
  }
  select.innerHTML = options.map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`).join("");
  select.disabled = false;
}

function syncKindOptions(currentKind = "") {
  const select = document.querySelector("#add-source-kind");
  if (!select) return;
  const kinds = CREATE_KIND_OPTIONS.slice();
  if (currentKind && !kinds.includes(currentKind)) kinds.push(currentKind);
  select.innerHTML = kinds.map(kind => `<option value="${escapeHtml(kind)}">${escapeHtml(kind)}</option>`).join("");
  if (currentKind) select.value = currentKind;
}

function setSecretPlaceholder(selector, editing) {
  const el = document.querySelector(selector);
  if (el) el.placeholder = editing ? "Leave blank to keep current" : "";
}

function setBuiltinHint(source) {
  const hint = document.querySelector("#add-source-builtin-fields");
  if (!hint) return;
  if (isAzureSource(source)) {
    hint.hidden = true;
    hint.innerHTML = "";
    return;
  }
  const driver = String((source?.config || {}).driver || "");
  const notes = {
    chat: "Ask captures facts from conversations in this project. There is no remote URL to configure.",
    local_git: "Indexes the connected project folder’s git history. Change the folder from Workspace.",
    local_files: "Scans the connected project folder. File kinds are toggled on the Sources list.",
  };
  const text = notes[driver] || "";
  hint.hidden = !text;
  hint.innerHTML = text ? `<p class="field-hint">${escapeHtml(text)}</p>` : "";
}

function resetAdoSourceFields() {
  const wrap = document.querySelector("#add-source-ado-fields");
  const org = document.querySelector("#add-source-ado-org");
  const token = document.querySelector("#add-source-ado-token");
  const hint = document.querySelector("#add-source-ado-token-hint");
  const status = document.querySelector("#add-source-ado-status");
  if (wrap) wrap.hidden = true;
  if (org) org.value = "";
  if (token) token.value = "";
  if (hint) hint.textContent = "";
  if (status) {
    status.hidden = true;
    status.textContent = "";
  }
}

async function fillAdoSourceFields(source) {
  const wrap = document.querySelector("#add-source-ado-fields");
  const mcp = document.querySelector("#add-source-mcp-fields");
  if (!wrap) return;
  const azure = isAzureSource(source);
  wrap.hidden = !azure;
  if (azure && mcp) mcp.hidden = true;
  if (!azure) return;
  const org = document.querySelector("#add-source-ado-org");
  const token = document.querySelector("#add-source-ado-token");
  const hint = document.querySelector("#add-source-ado-token-hint");
  const status = document.querySelector("#add-source-ado-status");
  if (token) token.value = "";
  try {
    const payload = await api("/api/integrations/credentials");
    const ado = payload.azure_devops || {};
    if (org) org.value = ado.org || "";
    if (hint) hint.textContent = ado.token_set
      ? "Secret is set. Leave blank to keep it."
      : "No secret yet. Paste a PAT here.";
    if (status) {
      status.hidden = false;
      status.textContent = ado.ready
        ? "Azure DevOps is connected. Saving here updates Boards, PRs, and Wiki together."
        : "Save organization and PAT to connect Azure Boards, PRs, and Wiki.";
    }
  } catch (_error) {
    if (hint) hint.textContent = "Could not load the current Azure DevOps secret.";
  }
}

async function saveAdoFromSourceModal() {
  if (!isAzureSource(editingSource)) return null;
  const org = (document.querySelector("#add-source-ado-org")?.value || "").trim();
  const token = (document.querySelector("#add-source-ado-token")?.value || "").trim();
  if (!org && !token) return null;
  const payload = { kind: "azure_devops" };
  if (org) payload.org = org;
  if (token) payload.token = token;
  return api("/api/integrations/credentials", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

function syncMcpHint(editing) {
  const hint = document.querySelector("#add-source-mcp-fields .mcp-hint");
  if (!hint) return;
  hint.textContent = editing
    ? "Rebinds this source to another configured MCP server. Tokens stay on the server (Setup → MCP)."
    : "Runs Discover on that server and creates draft bindings (krisp → name krisp).";
}

function setSourceModalMode(editing) {
  const title = document.querySelector("#add-source-title");
  const create = document.querySelector("#add-source-create");
  const del = document.querySelector("#add-source-delete");
  const driver = document.querySelector("#add-source-driver");
  const kind = document.querySelector("#add-source-kind");
  if (title) title.textContent = editing ? "Edit source" : "Add source";
  if (create) create.textContent = editing ? "Save" : "Add";
  if (del) {
    del.hidden = !editing || !isRemovableSource(editingSource);
    del.textContent = t("settings.source.deleteWithData");
  }
  if (driver) {
    driver.disabled = Boolean(editing);
    const mcpOpt = [...driver.options].find(option => option.value === "mcp");
    if (mcpOpt) mcpOpt.textContent = editing ? "MCP" : "MCP (discover tools)";
  }
  const cfg = editingSource?.config || {};
  const lockKind = Boolean(editing) && (
    PRESET_MCP_ADAPTERS.has(String(cfg.adapter || ""))
    || ["chat", "local_git", "local_files"].includes(String(cfg.driver || ""))
  );
  if (kind) kind.disabled = lockKind;
  syncMcpHint(Boolean(editing));
}

function resetSourceModalForm() {
  const error = document.querySelector("#add-source-error");
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }
  const name = document.querySelector("#add-source-name");
  if (name) name.value = "";
  clearAddSourceTestStatus();
  resetFtpKeyFields();
  resetHttpFormFields();
  resetAdoSourceFields();
  setSecretPlaceholder("#add-source-http-bearer", false);
  setSecretPlaceholder("#add-source-http-pass", false);
  setSecretPlaceholder("#add-source-ftp-pass", false);
  setSecretPlaceholder("#add-source-ftp-key-pass", false);
  setBuiltinHint(null);
}

function fillSourceForm(source) {
  const cfg = source.config || {};
  const driver = String(cfg.driver || "http");
  resetDriverSelect(driver);
  const driverEl = document.querySelector("#add-source-driver");
  if (driverEl) driverEl.value = driver;
  syncKindOptions(source.kind || "");
  const name = document.querySelector("#add-source-name");
  if (name) name.value = source.name || "";
  setAddSourceDriver(driver);
  setBuiltinHint(source);
  fillAdoSourceFields(source).catch(() => {});
  setSecretPlaceholder("#add-source-http-bearer", true);
  setSecretPlaceholder("#add-source-http-pass", true);
  setSecretPlaceholder("#add-source-ftp-pass", true);
  setSecretPlaceholder("#add-source-ftp-key-pass", true);

  if (driver === "http") {
    const http = cfg.http || {};
    const extract = http.extract || {};
    const setVal = (sel, value) => {
      const el = document.querySelector(sel);
      if (el) el.value = value ?? "";
    };
    setVal("#add-source-curl", "");
    setVal("#add-source-http-method", http.method || "GET");
    setVal("#add-source-http-url", http.url || "");
    setVal("#add-source-http-headers", formatHeaderLines(http.headers || {}));
    setVal("#add-source-http-body", http.body || "");
    setVal("#add-source-http-auth", http.auth || "none");
    setVal("#add-source-http-bearer", "");
    setVal("#add-source-http-user", http.username || "");
    setVal("#add-source-http-pass", "");
    setVal("#add-source-http-extract-mode", extract.mode || "auto");
    setVal("#add-source-http-items-path", extract.items_path || "");
    setVal("#add-source-http-label-field", extract.label_field || "");
    setVal("#add-source-http-text-fields", Array.isArray(extract.text_fields) ? extract.text_fields.join(", ") : "");
    syncHttpFormVisibility();
  } else if (driver === "ftp") {
    const ftp = cfg.ftp || {};
    const setVal = (sel, value) => {
      const el = document.querySelector(sel);
      if (el) el.value = value ?? "";
    };
    setVal("#add-source-ftp-url", ftp.url || "");
    setVal("#add-source-ftp-user", ftp.username || "");
    setVal("#add-source-ftp-pass", "");
    setVal("#add-source-ftp-path", ftp.remote_path || "/");
    const keyHint = document.querySelector("#add-source-ftp-key-name");
    if (keyHint && ftp.private_key) {
      keyHint.hidden = false;
      keyHint.textContent = `Key on file${ftp.private_key_filename ? `: ${ftp.private_key_filename}` : ""} — paste a new key to replace.`;
    }
  }
}

export function openAddSourceModal() {
  const modal = document.querySelector("#add-source-modal");
  if (!modal) return;
  editingSource = null;
  resetDriverSelect();
  syncKindOptions("docs");
  resetSourceModalForm();
  const driver = document.querySelector("#add-source-driver");
  if (driver) driver.value = "http";
  setAddSourceDriver("http");
  bindFtpKeyFilePicker();
  setSourceModalMode(false);
  fillMcpServerOptions().catch(() => {});
  modal.hidden = false;
}

export function openEditSourceModal(sourceId) {
  const source = cachedSources.find(item => item.id === sourceId);
  const modal = document.querySelector("#add-source-modal");
  if (!source || !modal) {
    showSnackbar("Source not loaded — reload Settings", "error");
    return;
  }
  editingSource = source;
  resetSourceModalForm();
  fillSourceForm(source);
  bindFtpKeyFilePicker();
  setSourceModalMode(true);
  const serverId = String((source.config || {}).mcp_server_id || "");
  fillMcpServerOptions(serverId).catch(() => {});
  modal.hidden = false;
}

export function closeAddSourceModal() {
  const modal = document.querySelector("#add-source-modal");
  if (modal) modal.hidden = true;
  editingSource = null;
  const driver = document.querySelector("#add-source-driver");
  const kind = document.querySelector("#add-source-kind");
  if (driver) driver.disabled = false;
  if (kind) kind.disabled = false;
  resetFtpKeyFields();
  clearAddSourceTestStatus();
}

function formatWipeCounts(result) {
  const nodes = Number(result?.nodes || 0);
  const candidates = Number(result?.candidates || 0);
  const edges = Number(result?.edges || 0);
  return t("settings.wipe.counts")
    .replace("{nodes}", String(nodes))
    .replace("{candidates}", String(candidates))
    .replace("{edges}", String(edges));
}

async function refreshAfterWipe() {
  try {
    const { refreshMemorySurfaces } = await import("./memory-ingest.js");
    await refreshMemorySurfaces();
  } catch (_err) {
    /* Memory view may not be mounted. */
  }
}

export async function clearSourceMemoryById(sourceId, sourceName) {
  const id = String(sourceId || "").trim();
  if (!id) throw new Error("source_id is required");
  const source = cachedSources.find(item => item.id === id);
  const name = sourceName || source?.name || "this source";
  const preview = await api(`/api/sources/${encodeURIComponent(id)}/clear`, {
    method: "POST",
    body: JSON.stringify({ confirm: false }),
  });
  const ok = await showAppConfirm({
    title: t("settings.source.clearTitle"),
    message: t("settings.source.clearConfirm").replace("{name}", name),
    detail: formatWipeCounts(preview),
    confirmLabel: t("settings.source.clear"),
    danger: true,
  });
  if (!ok) return;
  const result = await api(`/api/sources/${encodeURIComponent(id)}/clear`, {
    method: "POST",
    body: JSON.stringify({ confirm: true }),
  });
  showSnackbar(`${t("settings.source.clearDone")} ${formatWipeCounts(result)}`, "success");
  refreshAfterWipe();
}

export async function clearEditingSourceMemory() {
  if (!editingSource?.id) {
    throw new Error("Source is not open for edit");
  }
  await clearSourceMemoryById(editingSource.id, editingSource.name);
}

export async function clearLocalFilesGroupMemory() {
  const locals = localFilesSources(cachedSources);
  if (!locals.length) {
    showSnackbar(t("settings.source.clearEmpty"), "info");
    return;
  }
  const previews = [];
  for (const source of locals) {
    const preview = await api(`/api/sources/${encodeURIComponent(source.id)}/clear`, {
      method: "POST",
      body: JSON.stringify({ confirm: false }),
    });
    previews.push(preview);
  }
  const totals = previews.reduce(
    (acc, item) => ({
      nodes: acc.nodes + Number(item.nodes || 0),
      candidates: acc.candidates + Number(item.candidates || 0),
      edges: acc.edges + Number(item.edges || 0),
    }),
    { nodes: 0, candidates: 0, edges: 0 },
  );
  const ok = await showAppConfirm({
    title: t("settings.source.clearLocalTitle"),
    message: t("settings.source.clearLocalConfirm"),
    detail: formatWipeCounts(totals),
    confirmLabel: t("settings.source.clear"),
    danger: true,
  });
  if (!ok) return;
  let wiped = { nodes: 0, candidates: 0, edges: 0 };
  for (const source of locals) {
    const result = await api(`/api/sources/${encodeURIComponent(source.id)}/clear`, {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    });
    wiped = {
      nodes: wiped.nodes + Number(result.nodes || 0),
      candidates: wiped.candidates + Number(result.candidates || 0),
      edges: wiped.edges + Number(result.edges || 0),
    };
  }
  showSnackbar(`${t("settings.source.clearDone")} ${formatWipeCounts(wiped)}`, "success");
  refreshAfterWipe();
}

export async function deleteEditingSource() {
  if (!editingSource || !isRemovableSource(editingSource)) {
    throw new Error("This built-in source cannot be deleted");
  }
  const name = editingSource.name || "this source";
  const sourceId = editingSource.id;
  const preview = await api(`/api/sources/${encodeURIComponent(sourceId)}/clear`, {
    method: "POST",
    body: JSON.stringify({ confirm: false }),
  });
  const ok = await showAppConfirm({
    title: t("settings.source.deleteTitle"),
    message: t("settings.source.deleteConfirm").replace("{name}", name),
    detail: formatWipeCounts(preview),
    confirmLabel: t("settings.source.deleteWithData"),
    danger: true,
  });
  if (!ok) return;
  await api(`/api/sources/${encodeURIComponent(sourceId)}?purge_memory=1`, { method: "DELETE" });
  closeAddSourceModal();
  await loadProjectSources();
  showSnackbar(t("settings.source.deleteDone"), "success");
  refreshAfterWipe();
}

function clearAddSourceTestStatus() {
  const status = document.querySelector("#add-source-test-status");
  if (!status) return;
  status.hidden = true;
  status.textContent = "";
  status.classList.remove("is-ok", "is-fail");
  const preview = document.querySelector("#add-source-http-preview");
  if (preview) {
    preview.hidden = true;
    preview.innerHTML = "";
  }
}

function setAddSourceTestStatus(ok, message) {
  const status = document.querySelector("#add-source-test-status");
  if (!status) return;
  status.hidden = false;
  status.textContent = message;
  status.classList.toggle("is-ok", Boolean(ok));
  status.classList.toggle("is-fail", !ok);
}

function mergeKeptSecrets(next, previous, keys) {
  const out = { ...next };
  for (const key of keys) {
    if (!String(out[key] || "") && previous?.[key]) out[key] = previous[key];
  }
  return out;
}

async function buildEditSourceDraft() {
  const source = editingSource;
  if (!source) throw new Error("Source is not open for edit");
  const cfg = { ...(source.config || {}) };
  const driver = String(cfg.driver || "http");
  const kind = document.querySelector("#add-source-kind")?.value || source.kind || "docs";
  const name = (document.querySelector("#add-source-name")?.value || "").trim();
  const projectId = source.project_id || state.projectId || "architectos";

  if (driver === "http") {
    const http = buildHttpConfigFromForm();
    cfg.http = mergeKeptSecrets(http, cfg.http || {}, ["bearer_token", "password"]);
    if (http.username) cfg.http.username = http.username;
  } else if (driver === "ftp") {
    const url = (document.querySelector("#add-source-ftp-url")?.value || "").trim();
    if (!url) throw new Error("FTP/SFTP URL is required");
    const { pem, filename } = await readFtpPrivateKey();
    const passphrase = document.querySelector("#add-source-ftp-key-pass")?.value || "";
    const password = document.querySelector("#add-source-ftp-pass")?.value || "";
    if (pem && !/^sftp:/i.test(url)) {
      throw new Error("Private key auth requires an sftp:// URL");
    }
    const ftp = {
      ...(cfg.ftp || {}),
      url,
      username: (document.querySelector("#add-source-ftp-user")?.value || "").trim(),
      remote_path: (document.querySelector("#add-source-ftp-path")?.value || "/").trim() || "/",
    };
    if (password) ftp.password = password;
    if (pem) {
      ftp.private_key = pem;
      ftp.private_key_filename = filename || "private-key";
      if (passphrase) ftp.private_key_passphrase = passphrase;
    }
    if (!ftp.password && !ftp.private_key) {
      throw new Error("Provide a password or a private key for SFTP/FTP");
    }
    cfg.ftp = ftp;
  } else if (driver === "mcp") {
    const serverId = document.querySelector("#add-source-mcp-server")?.value || cfg.mcp_server_id || "";
    if (!serverId) throw new Error("Choose an MCP server");
    cfg.mcp_server_id = serverId;
  }

  return {
    mode: "edit",
    source_id: source.id,
    project_id: projectId,
    driver,
    kind,
    name,
    config: cfg,
  };
}

async function buildAddSourceDraft() {
  if (editingSource) return buildEditSourceDraft();
  const driver = document.querySelector("#add-source-driver")?.value || "http";
  const kind = document.querySelector("#add-source-kind")?.value || "docs";
  const name = (document.querySelector("#add-source-name")?.value || "").trim();
  const projectId = state.projectId || "architectos";

  if (driver === "mcp") {
    const serverId = document.querySelector("#add-source-mcp-server")?.value || "";
    if (!serverId) throw new Error("Choose an MCP server");
    return {
      mode: "mcp",
      project_id: projectId,
      driver: "mcp",
      kind,
      name,
      config: {
        driver: "mcp",
        mcp_server_id: serverId,
        enabled: false,
      },
    };
  }

  const config = {
    driver,
    enabled: false,
    schedule: { enabled: false, interval_minutes: 60, on_startup: false },
    timeouts: { item: 25, total: 180 },
    max_items: 0,
    ingest_mode: "candidates",
  };
  if (driver === "http") {
    config.http = buildHttpConfigFromForm();
  } else if (driver === "ftp") {
    const url = (document.querySelector("#add-source-ftp-url")?.value || "").trim();
    if (!url) throw new Error("FTP/SFTP URL is required");
    const { pem, filename } = await readFtpPrivateKey();
    const passphrase = document.querySelector("#add-source-ftp-key-pass")?.value || "";
    const password = document.querySelector("#add-source-ftp-pass")?.value || "";
    if (pem && !/^sftp:/i.test(url)) {
      throw new Error("Private key auth requires an sftp:// URL");
    }
    if (!password && !pem) {
      throw new Error("Provide a password or a private key for SFTP/FTP");
    }
    config.ftp = {
      url,
      username: (document.querySelector("#add-source-ftp-user")?.value || "").trim(),
      password,
      remote_path: (document.querySelector("#add-source-ftp-path")?.value || "/").trim() || "/",
      recursive: true,
      include_glob: ["**/*.md", "**/*.txt", "**/*.html"],
    };
    if (pem) {
      config.ftp.private_key = pem;
      config.ftp.private_key_filename = filename || "private-key";
      if (passphrase) config.ftp.private_key_passphrase = passphrase;
    }
  } else {
    throw new Error(`Unsupported type: ${driver}`);
  }

  return {
    mode: "create",
    project_id: projectId,
    driver,
    kind,
    name,
    config,
  };
}

export async function testAddSourceConnection() {
  const error = document.querySelector("#add-source-error");
  const btn = document.querySelector("#add-source-test");
  const showError = (message) => {
    if (!error) return;
    error.hidden = false;
    error.textContent = message;
  };
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }
  clearAddSourceTestStatus();
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Testing…";
  }
  try {
    const draft = await buildAddSourceDraft();
    await saveAdoFromSourceModal();
    const result = await api("/api/sources/probe", {
      method: "POST",
      body: JSON.stringify({
        project_id: draft.project_id,
        driver: draft.driver,
        kind: draft.kind,
        config: draft.config,
      }),
    });
    if (result.ok) {
      const sample = String(result.sample || result.title || "").trim();
      const ms = result.latency_ms != null ? ` · ${result.latency_ms} ms` : "";
      const candCount = Array.isArray(result.preview_candidates) ? result.preview_candidates.length : 0;
      const mapping = candCount ? ` · ${candCount} preview item(s)` : "";
      const detail = sample ? ` — ${sample.slice(0, 160)}` : "";
      setAddSourceTestStatus(true, `Connection OK${ms}${mapping}${detail}`);
      if (draft.driver === "http") renderHttpProbePreview(result);
      showSnackbar("Connection OK", "success");
    } else {
      const message = result.error || "Connection failed";
      setAddSourceTestStatus(false, message);
      showError(message);
    }
  } catch (err) {
    const message = err.message || String(err);
    setAddSourceTestStatus(false, message);
    showError(message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Test connection";
    }
  }
}

export async function submitAddSourceModal() {
  const error = document.querySelector("#add-source-error");
  const showError = (message) => {
    if (!error) return;
    error.hidden = false;
    error.textContent = message;
  };
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }

  try {
    const draft = await buildAddSourceDraft();
    if (draft.mode === "edit") {
      await saveAdoFromSourceModal();
      await saveSourcePatch(draft.source_id, {
        project_id: draft.project_id,
        kind: draft.kind,
        name: draft.name,
        config: { ...draft.config, name_customized: Boolean(draft.name) },
      });
      closeAddSourceModal();
      await loadProjectSources();
      showSnackbar("Source updated", "success");
      return;
    }
    if (draft.mode === "mcp") {
      const result = await discoverMcpSources(draft.config.mcp_server_id);
      const count = Array.isArray(result.sources) ? result.sources.length : 0;
      closeAddSourceModal();
      await loadProjectSources();
      showSnackbar(count ? `Discovered ${count} source binding(s)` : "No bindings discovered", count ? "success" : "info");
      return;
    }

    const payload = {
      project_id: draft.project_id,
      kind: draft.kind,
      config: draft.config,
    };
    if (draft.name) payload.name = draft.name;
    await createProjectSource(payload);
    closeAddSourceModal();
    await loadProjectSources();
    showSnackbar("Source added", "success");
  } catch (err) {
    showError(err.message || String(err));
  }
}

export function bindMemorySourcesUi() {
  const driver = document.querySelector("#add-source-driver");
  if (driver && !driver.dataset.bound) {
    driver.dataset.bound = "1";
    driver.addEventListener("change", () => {
      setAddSourceDriver(driver.value);
      clearAddSourceTestStatus();
    });
  }
  const method = document.querySelector("#add-source-http-method");
  if (method && !method.dataset.bound) {
    method.dataset.bound = "1";
    method.addEventListener("change", syncHttpFormVisibility);
  }
  const auth = document.querySelector("#add-source-http-auth");
  if (auth && !auth.dataset.bound) {
    auth.dataset.bound = "1";
    auth.addEventListener("change", syncHttpFormVisibility);
  }
  const curlBtn = document.querySelector("#add-source-curl-apply");
  if (curlBtn && !curlBtn.dataset.bound) {
    curlBtn.dataset.bound = "1";
    curlBtn.addEventListener("click", () => {
      try {
        applyCurlToHttpForm();
      } catch (error) {
        showSnackbar(error.message || "Invalid cURL", "error");
      }
    });
  }
  syncHttpFormVisibility();
}
