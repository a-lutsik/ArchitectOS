/* Memory ingest/rescan progress + source selection — extracted from app.js */
import { api } from "./api-client.js";
import { escapeHtml } from "./dom-utils.js";
import { loadMemoryCandidates, loadMemoryLifecycle } from "./memory-panel.js";
import { loadProjectSources, selectedSourceIds } from "./memory-sources.js";
import { loadAnalytics } from "./providers.js";
import { scheduleGraphLoad } from "./projects.js";
import { state } from "./state.js";

function ingestTimeoutPayload(sources = []) {
  const itemTimeout = Number(document.querySelector("#ingest-item-timeout")?.value || 25);
  const sourceTimeout = Number(document.querySelector("#ingest-source-timeout")?.value || 90);
  const timeouts = {};
  for (const source of sources) {
    timeouts[source] = { item: itemTimeout, source: sourceTimeout };
  }
  return {
    item_timeout: itemTimeout,
    source_timeout: sourceTimeout,
    timeouts,
  };
}

async function ingestMemorySources() {
  const summary = document.querySelector("#candidate-summary");
  const sources = selectedSourceIds();
  const ingestMode = document.querySelector("#ingest-direct-memory")?.checked ? "memory" : "candidates";
  const mineOnlyEl = document.querySelector("#ingest-boards-mine-only");
  const allItems = mineOnlyEl ? !mineOnlyEl.checked : true;
  const boardsTypes = selectedBoardsTypes();
  if (!sources.length) {
    if (summary) { summary.className = "provider-test error"; summary.textContent = "Select at least one source."; }
    return;
  }
  if (summary) { summary.className = "provider-test"; summary.textContent = "ingesting..."; }
  showIngestProgress(true);
  renderIngestProgress({
    running: true,
    current: "queued",
    sources,
    project_id: state.projectId,
    logs: [{ level: "info", message: `Starting AutoScan for ${sources.length} source(s) · ${ingestMode === "memory" ? "direct memory" : "review candidates"}` }],
  });
  try {
    const scheduled = await api("/api/memory/ingest", {
      method: "POST",
      body: JSON.stringify({
        project_id: state.projectId,
        sources,
        limit: 0,
        async: true,
        all_items: allItems,
        work_item_types: boardsTypes,
        ingest_mode: ingestMode,
        ...ingestTimeoutPayload(sources),
      })
    });
    if (!scheduled.scheduled && scheduled.reason === "already_running") {
      if (summary) { summary.className = "provider-test"; summary.textContent = "Ingest already running..."; }
    }
    const status = await waitForMemoryIngest(summary);
    if (status?.error) return;
    await refreshMemorySurfaces();
    scheduleGraphLoad();
  } catch (error) {
    if (summary) { summary.className = "provider-test error"; summary.textContent = error.message; }
    renderIngestProgress({ running: false, current: "error", error: error.message, logs: [{ level: "error", message: error.message }] });
  }
}

function showIngestProgress(visible) {
  const panel = document.querySelector("#ingest-progress");
  if (panel) panel.hidden = !visible;
}

function ingestStepTitle(key) {
  if (String(key).startsWith("source_")) return "Source scan";
  if (String(key).startsWith("project:")) return "Project scan";
  if (key === "queued") return "Queued";
  if (key === "starting") return "Starting";
  if (key === "prepare") return "Prepare review queue";
  if (key === "done") return "Finished";
  if (key === "error") return "Failed";
  return String(key || "Step").replace(/-/g, " ");
}

function ingestStepMark(_key, stepState) {
  if (stepState === "done") return "✓";
  if (stepState === "error") return "!";
  if (stepState === "warn") return "!";
  if (stepState === "running") return "●";
  return "·";
}

function friendlyIngestMessage(entry) {
  const message = String(entry?.message || "").trim();
  if (!message) return "Waiting…";
  return message
    .replace(/^Starting ingest · /, "Starting · ")
    .replace(/^Preparing review queue from /, "Building review queue from ")
    .replace(/^Ingest finished · /, "Done · ");
}

function buildIngestSteps(status) {
  const sources = Array.isArray(status.sources) ? status.sources.slice() : [];
  const steps = sources.length ? sources.slice() : ["starting"];
  steps.push("prepare");
  return steps;
}

function inferIngestStepStates(status) {
  const steps = buildIngestSteps(status);
  const logs = status.logs || [];
  const current = String(status.current || "");
  const states = {};
  const summaries = {};
  for (const step of steps) {
    states[step] = "pending";
  }
  if (status.running && current) {
    const idx = steps.indexOf(current);
    if (idx >= 0) {
      for (let i = 0; i < idx; i += 1) states[steps[i]] = "done";
      states[current] = "running";
    }
  }
  if (!status.running && !status.error) {
    for (const step of steps) states[step] = "done";
  }
  if (status.error) {
    const idx = steps.indexOf(current);
    for (let i = 0; i < steps.length; i += 1) {
      if (i < idx) states[steps[i]] = "done";
      else if (i === idx) states[steps[i]] = "error";
    }
  }
  for (const entry of logs) {
    const source = String(entry.source || "").trim();
    if (source && steps.includes(source)) {
      summaries[source] = friendlyIngestMessage(entry);
      if (/done ·/i.test(entry.message || "")) states[source] = "done";
      else if (entry.level === "error") states[source] = "error";
      else if (entry.level === "warn") states[source] = "warn";
      else if (/scanning/i.test(entry.message || "")) states[source] = "running";
    }
  }
  return { steps, states, summaries };
}

function renderIngestProgress(status) {
  const panel = document.querySelector("#ingest-progress");
  if (!panel) return;
  const { steps, states, summaries } = inferIngestStepStates(status || {});
  const stepsEl = panel.querySelector(".ingest-steps");
  if (stepsEl) {
    stepsEl.innerHTML = steps.map(step => {
      const stateName = states[step] || "pending";
      return `<li class="ingest-step ingest-step-${stateName}" data-step="${escapeHtml(step)}">
        <span class="ingest-step-mark">${ingestStepMark(step, stateName)}</span>
        <span class="ingest-step-title">${escapeHtml(ingestStepTitle(step))}</span>
        <span class="ingest-step-summary">${escapeHtml(summaries[step] || "")}</span>
      </li>`;
    }).join("");
  }
  const feedEl = panel.querySelector(".ingest-log-feed");
  if (feedEl && Array.isArray(status?.logs)) {
    feedEl.textContent = status.logs.map(entry => {
      const level = (entry.level || "info").toUpperCase();
      const source = entry.source ? `[${entry.source}] ` : "";
      return `${level} ${source}${entry.message || ""}`;
    }).join("\n");
    feedEl.scrollTop = feedEl.scrollHeight;
  }
}

async function waitForMemoryIngest(summary) {
  const sourceTimeout = Number(document.querySelector("#ingest-source-timeout")?.value || 90);
  const sourceCount = Math.max(1, selectedSourceIds().length);
  const deadline = Date.now() + (Math.max(15, sourceTimeout) * sourceCount + 60) * 1000;
  while (Date.now() < deadline) {
    const status = await api("/api/memory/ingest");
    renderIngestProgress(status);
    if (status.running) {
      if (summary) {
        summary.className = "provider-test";
        summary.textContent = `AutoScan working (${ingestStepTitle(status.current || "scan")})…`;
      }
      await new Promise(resolve => setTimeout(resolve, 400));
      continue;
    }
    if (status.error) {
      if (summary) { summary.className = "provider-test error"; summary.textContent = status.error; }
      return status;
    }
    const result = status.result || {};
    const warnings = result.warnings && result.warnings.length ? `; ${result.warnings.join("; ")}` : "";
    const boards = result.boards_count ? `, ${result.boards_count} Boards→memory` : "";
    const azureGit = result.azure_git_count ? `, ${result.azure_git_count} Azure Git→memory` : "";
    const wiki = result.wiki_count ? `, ${result.wiki_count} Wiki→memory` : "";
    const direct = result.direct_count ? `, ${result.direct_count} direct→memory` : "";
    if (summary) {
      summary.className = "provider-test ok";
      summary.textContent = `${result.count || 0} candidate(s), ${result.duplicates || 0} duplicate hint(s), ${result.pending || 0} pending${direct}${boards}${azureGit}${wiki}${warnings}`;
    }
    return status;
  }
  if (summary) {
    summary.className = "provider-test";
    summary.textContent = "Ingest is still running in the background.";
  }
  return null;
}

async function rescanAllMemorySources() {
  const summary = document.querySelector("#candidate-summary");
  const ingestMode = document.querySelector("#ingest-direct-memory")?.checked ? "memory" : "candidates";
  const mineOnlyEl = document.querySelector("#ingest-boards-mine-only");
  const allItems = mineOnlyEl ? !mineOnlyEl.checked : true;
  const boardsTypes = selectedBoardsTypes();
  if (summary) { summary.className = "provider-test"; summary.textContent = "rescanning enabled sources..."; }
  try {
    const scheduled = await api("/api/memory/rescan", {
      method: "POST",
      body: JSON.stringify({
        trigger: "manual",
        project_id: state.projectId,
        all_projects: false,
        sources: "all",
        limit: 0,
        include_mcp: true,
        all_items: allItems,
        work_item_types: boardsTypes,
        ingest_mode: ingestMode,
        ...ingestTimeoutPayload(selectedSourceIds()),
      })
    });
    if (!scheduled.scheduled && scheduled.reason === "already_running") {
      if (summary) { summary.className = "provider-test"; summary.textContent = "Rescan already running..."; }
    }
    await waitForMemoryRescan(summary);
  } catch (error) {
    if (summary) { summary.className = "provider-test error"; summary.textContent = error.message; }
  }
}

async function waitForMemoryRescan(summary, attempts = 120) {
  showIngestProgress(true);
  for (let i = 0; i < attempts; i += 1) {
    const [status, ingest] = await Promise.all([
      api("/api/memory/rescan"),
      api("/api/memory/ingest").catch(() => null),
    ]);
    if (ingest) renderIngestProgress(ingest);
    if (status.running) {
      if (summary) {
        summary.className = "provider-test";
        summary.textContent = `rescanning (${status.trigger || "manual"})...`;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
      continue;
    }
    if (status.error) {
      if (summary) { summary.className = "provider-test error"; summary.textContent = status.error; }
      return status;
    }
    const result = status.result || {};
    const warnings = result.warnings && result.warnings.length ? `; ${result.warnings.join("; ")}` : "";
    const boards = result.boards_count ? `, ${result.boards_count} Boards→memory` : "";
    const azureGit = result.azure_git_count ? `, ${result.azure_git_count} Azure Git→memory` : "";
    const wiki = result.wiki_count ? `, ${result.wiki_count} Wiki→memory` : "";
    const direct = result.memory_written ? `, ${result.memory_written} memory write(s)` : "";
    if (summary) {
      summary.className = "provider-test ok";
      summary.textContent = `${result.count || 0} candidate(s), ${result.duplicates || 0} duplicate hint(s)${direct}${boards}${azureGit}${wiki}${warnings}`;
    }
    await refreshMemorySurfaces();
    scheduleGraphLoad();
    return status;
  }
  if (summary) {
    summary.className = "provider-test";
    summary.textContent = "Rescan is still running in the background. Refresh candidates later.";
  }
  return null;
}

async function refreshMemorySurfaces() {
  await Promise.all([
    loadMemoryCandidates(),
    loadMemoryLifecycle(),
    loadAnalytics(),
  ]);
}

async function syncStartupMemoryRescan() {
  try {
    const status = await api("/api/memory/rescan");
    if (!status.running && !status.result && !status.error) return;
    const summary = document.querySelector("#candidate-summary");
    if (status.running) {
      await waitForMemoryRescan(summary);
      return;
    }
    if (status.result && summary) {
      summary.className = "provider-test ok";
      summary.textContent = `Startup rescan: ${status.result.count || 0} candidate(s)`;
      await refreshMemorySurfaces();
    }
  } catch (_error) {
    // Startup rescan is best-effort; UI stays usable if it fails.
  }
}

async function setIngestSourcesSelected(selected) {
  document.querySelectorAll(".ingest-source").forEach(input => {
    input.checked = Boolean(selected);
  });
  syncBoardsOptionsVisibility();
}

function selectedBoardsTypes() {
  return [...document.querySelectorAll(".boards-type-option")]
    .filter(input => input.checked)
    .map(input => input.value);
}

function syncBoardsTypeSummary() {
  const summary = document.querySelector("#boards-type-summary");
  if (!summary) return;
  const all = [...document.querySelectorAll(".boards-type-option")];
  const selected = all.filter(input => input.checked);
  if (!selected.length) {
    summary.textContent = "No types";
  } else if (selected.length === all.length) {
    summary.textContent = "All types";
  } else {
    summary.textContent = selected.map(input => input.value).join(", ");
  }
}

function syncBoardsOptionsVisibility() {
  const panel = document.querySelector("#ingest-boards-options");
  if (!panel) return;
  const boardsChecked = [...document.querySelectorAll(".ingest-source:checked")].some(input => {
    const adapter = input.dataset.adapter || "";
    const legacy = input.value || "";
    return adapter === "azure-boards" || legacy === "azure-boards";
  });
  panel.hidden = !boardsChecked;
  panel.setAttribute("aria-hidden", boardsChecked ? "false" : "true");
  syncBoardsTypeSummary();
}

async function syncIngestSourcesWithMcp() {
  try {
    const [mcpPayload] = await Promise.all([
      api("/api/mcp/servers"),
      loadProjectSources(),
    ]);
    const servers = mcpPayload.servers || [];
    const byId = Object.fromEntries(servers.map(server => [server.id, server]));
    document.querySelectorAll(".ingest-source").forEach(input => {
      const adapter = input.dataset.adapter || "";
      const serverId = input.dataset.mcpServer || "";
      const server = byId[serverId];
      const label = input.closest(".source-checkbox");
      const enabled = !serverId || Boolean(server?.enabled);
      input.disabled = false;
      if (adapter.startsWith("azure") && !enabled) {
        if (label && !label.dataset.readyTitle) label.dataset.readyTitle = label.title;
        if (label) label.title = `Enable MCP server ${serverId || adapter} in MCP settings first`;
      } else if (label?.dataset.readyTitle) {
        label.title = label.dataset.readyTitle;
      }
    });
    syncBoardsOptionsVisibility();
  } catch (_error) {
    syncBoardsOptionsVisibility();
  }
}

document.querySelector("#source-grid")?.addEventListener("aos:sources-rendered", () => {
  syncBoardsOptionsVisibility();
});

document.addEventListener("change", event => {
  if (event.target?.classList?.contains("ingest-source")) {
    syncBoardsOptionsVisibility();
  }
});

export {
  ingestMemorySources, refreshMemorySurfaces, rescanAllMemorySources,
  setIngestSourcesSelected, syncBoardsOptionsVisibility, syncBoardsTypeSummary,
  syncIngestSourcesWithMcp, syncStartupMemoryRescan,
};
