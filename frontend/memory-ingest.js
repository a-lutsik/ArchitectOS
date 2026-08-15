/* Memory ingest/rescan progress + source selection — extracted from app.js */
import { api } from "./api-client.js";
import { escapeHtml } from "./dom-utils.js";
import { loadMemoryCandidates, loadMemoryLifecycle } from "./memory-panel.js";
import { loadAnalytics } from "./providers.js";
import { scheduleGraphLoad } from "./projects.js";
import { state } from "./state.js";

function ingestTimeoutPayload(sources = []) {
  const itemTimeout = Number(document.querySelector("#ingest-item-timeout")?.value || 25);
  const sourceTimeout = Number(document.querySelector("#ingest-source-timeout")?.value || 600);
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
  const sources = [...document.querySelectorAll(".ingest-source")].filter(input => input.checked).map(input => input.value);
  const limit = Number(document.querySelector("#ingest-limit")?.value || 12);
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
    logs: [{ level: "info", message: `Starting AutoScan for ${sources.length} source group(s) · ${ingestMode === "memory" ? "direct memory" : "review candidates"}` }],
  });
  try {
    const scheduled = await api("/api/memory/ingest", {
      method: "POST",
      body: JSON.stringify({
        project_id: state.projectId,
        sources,
        limit,
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

const INGEST_STEP_META = {
  queued: { title: "Queued", mark: "…" },
  starting: { title: "Starting", mark: "1" },
  files: { title: "Project files", mark: "F" },
  chat: { title: "App chat", mark: "C" },
  git: { title: "Git history", mark: "G" },
  granola: { title: "Granola meetings", mark: "N" },
  "azure-boards": { title: "Azure Boards", mark: "B" },
  "azure-git": { title: "Azure Git", mark: "R" },
  "azure-wiki": { title: "Azure Wiki", mark: "W" },
  "teams-meetings": { title: "Teams meetings", mark: "T" },
  prepare: { title: "Prepare review queue", mark: "P" },
  done: { title: "Finished", mark: "✓" },
  error: { title: "Failed", mark: "!" },
};

function ingestStepTitle(key) {
  if (INGEST_STEP_META[key]) return INGEST_STEP_META[key].title;
  if (String(key).startsWith("project:")) return "Project scan";
  return String(key || "Step").replace(/-/g, " ");
}

function ingestStepMark(key, state) {
  if (state === "done") return "✓";
  if (state === "error") return "!";
  if (state === "warn") return "!";
  if (state === "running") return "●";
  return (INGEST_STEP_META[key] && INGEST_STEP_META[key].mark) || "·";
}

function friendlyIngestMessage(entry) {
  const message = String(entry?.message || "").trim();
  if (!message) return "Waiting…";
  return message
    .replace(/^Starting ingest · /, "Starting · ")
    .replace(/^Scanning project files for /, "Looking through ")
    .replace(/^File scan done · /, "Found ")
    .replace(/^Reading chat history…$/, "Reading recent chats…")
    .replace(/^Chat done · /, "Chat: ")
    .replace(/^Scanning git history…$/, "Reading git commits…")
    .replace(/^Git done · /, "Git: ")
    .replace(/^Calling Granola MCP…$/, "Asking Granola for meetings…")
    .replace(/^Granola done · /, "Granola: ")
    .replace(/^Importing Azure Boards work items…$/, "Fetching Azure Boards work items…")
    .replace(/^Azure Boards done · /, "Boards: ")
    .replace(/^Importing Azure Git repos and pull requests…$/, "Fetching Azure Git repos and PRs…")
    .replace(/^Azure Git done · /, "Azure Git: ")
    .replace(/^Importing Azure Wiki pages…$/, "Fetching Azure Wiki pages…")
    .replace(/^Azure Wiki done · /, "Wiki: ")
    .replace(/^Importing Teams meetings \(Graph transcripts \/ AI Insights\)…$/, "Fetching Teams transcripts & insights…")
    .replace(/^Teams done · /, "Teams: ")
    .replace(/^Preparing review queue from /, "Building review queue from ")
    .replace(/^Ingest finished · /, "Done · ");
}

function buildIngestSteps(status) {
  const sources = Array.isArray(status.sources) ? status.sources.slice() : [];
  const fileGroup = ["docs", "code", "adr", "issues", "prs", "meetings"];
  const steps = [];
  const hasFiles = sources.some(item => fileGroup.includes(item));
  if (hasFiles) steps.push("files");
  for (const source of ["chat", "git", "granola", "azure-boards", "azure-git", "azure-wiki", "teams-meetings"]) {
    if (sources.includes(source)) steps.push(source);
  }
  if (!steps.length) steps.push("starting");
  steps.push("prepare");
  return steps;
}

function inferIngestStepStates(status) {
  const steps = buildIngestSteps(status);
  const logs = status.logs || [];
  const current = String(status.current || "");
  const states = {};
  const summaries = {};
  for (const key of steps) {
    states[key] = "pending";
    summaries[key] = "Waiting…";
  }
  for (const entry of logs) {
    let key = entry.source || "";
    const msg = String(entry.message || "");
    if (!key) {
      if (/prepare|review queue/i.test(msg)) key = "prepare";
      else if (/starting ingest|queued ingest/i.test(msg)) key = steps[0];
      else if (/finished/i.test(msg)) key = "prepare";
    }
    if (key === "docs" || key === "code" || key === "adr" || key === "issues" || key === "prs" || key === "meetings") key = "files";
    if (!steps.includes(key)) continue;
    const level = entry.level || "info";
    if (level === "error") states[key] = "error";
    else if (level === "warn") states[key] = states[key] === "error" ? "error" : "warn";
    else if (/done|finished|found |candidate|written/i.test(msg)) states[key] = states[key] === "error" ? "error" : (states[key] === "warn" ? "warn" : "done");
    else if (/scanning|reading|calling|importing|fetching|preparing|starting|looking/i.test(msg) || /…$/.test(msg)) {
      if (states[key] === "pending") states[key] = "running";
    }
    summaries[key] = friendlyIngestMessage(entry);
  }
  if (status.running && current) {
    let active = current;
    if (active.startsWith("project:")) active = steps[0] || "files";
    if (active === "starting" || active === "queued" || active === "done") active = steps.find(key => states[key] === "pending" || states[key] === "running") || steps[steps.length - 1];
    if (steps.includes(active) && states[active] !== "done" && states[active] !== "error" && states[active] !== "warn") {
      states[active] = "running";
    }
    // Mark earlier steps done if we've moved past them.
    const idx = steps.indexOf(active);
    if (idx > 0) {
      for (let i = 0; i < idx; i += 1) {
        if (states[steps[i]] === "pending" || states[steps[i]] === "running") states[steps[i]] = "done";
      }
    }
  }
  if (!status.running && !status.error) {
    for (const key of steps) {
      if (states[key] === "pending" || states[key] === "running") states[key] = "done";
    }
  }
  if (status.error) {
    const active = steps.find(key => states[key] === "running" || states[key] === "pending") || steps[steps.length - 1];
    states[active] = "error";
    summaries[active] = status.error;
  }
  return { steps, states, summaries };
}

function renderIngestProgress(status) {
  const panel = document.querySelector("#ingest-progress");
  const statusEl = document.querySelector("#ingest-agent-status");
  const stepsEl = document.querySelector("#ingest-agent-steps");
  const barFill = document.querySelector("#ingest-agent-bar-fill");
  const countEl = document.querySelector("#ingest-agent-count");
  const latestEl = document.querySelector("#ingest-agent-latest");
  const feedEl = document.querySelector("#ingest-agent-feed");
  if (!panel || !statusEl || !stepsEl) return;
  panel.hidden = false;

  const { steps, states, summaries } = inferIngestStepStates(status);
  const doneCount = steps.filter(key => ["done", "warn"].includes(states[key])).length;
  const total = steps.length || 1;
  const pct = status.running ? Math.round((doneCount / total) * 100) : (status.error ? Math.round((doneCount / total) * 100) : 100);

  if (status.running) {
    statusEl.textContent = `Working on ${ingestStepTitle(status.current || steps.find(key => states[key] === "running") || "scan")}…`;
    statusEl.dataset.tone = "running";
  } else if (status.error) {
    statusEl.textContent = "Stopped with an error";
    statusEl.dataset.tone = "error";
  } else {
    statusEl.textContent = "Finished";
    statusEl.dataset.tone = "ok";
  }

  if (barFill) barFill.style.width = `${Math.max(status.running ? 8 : 0, pct)}%`;
  if (countEl) countEl.textContent = `${Math.min(doneCount + (status.running ? 1 : 0), total)} / ${total}`;

  stepsEl.innerHTML = steps.map(key => {
    const state = states[key] || "pending";
    const badge = state === "running" ? "now" : state === "done" ? "done" : state === "warn" ? "skipped" : state === "error" ? "error" : "queued";
    return `<div class="ingest-agent-step" data-state="${escapeHtml(state)}" role="listitem">
      <span class="ingest-agent-step-mark">${escapeHtml(ingestStepMark(key, state))}</span>
      <div class="ingest-agent-step-body">
        <strong>${escapeHtml(ingestStepTitle(key))}</strong>
        <p>${escapeHtml(summaries[key] || "Waiting…")}</p>
      </div>
      <span class="ingest-agent-step-badge">${escapeHtml(badge)}</span>
    </div>`;
  }).join("");

  const latest = (status.logs || []).slice().reverse().find(Boolean);
  if (latestEl) latestEl.textContent = latest ? friendlyIngestMessage(latest) : "";

  if (feedEl) {
    feedEl.textContent = (status.logs || []).map(entry => {
      const level = (entry.level || "info").toUpperCase();
      const source = entry.source ? `[${entry.source}] ` : "";
      return `${level} ${source}${entry.message || ""}`;
    }).join("\n");
    feedEl.scrollTop = feedEl.scrollHeight;
  }
}

async function waitForMemoryIngest(summary, attempts = 180) {
  for (let i = 0; i < attempts; i += 1) {
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
    const teams = result.teams_count ? `, ${result.teams_count} Teams→memory` : "";
    const direct = result.direct_count ? `, ${result.direct_count} direct→memory` : "";
    if (summary) {
      summary.className = "provider-test ok";
      summary.textContent = `${result.count || 0} candidate(s), ${result.duplicates || 0} duplicate hint(s), ${result.pending || 0} pending${direct}${boards}${azureGit}${wiki}${teams}${warnings}`;
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
  const limit = Number(document.querySelector("#ingest-limit")?.value || 24);
  const ingestMode = document.querySelector("#ingest-direct-memory")?.checked ? "memory" : "candidates";
  const mineOnlyEl = document.querySelector("#ingest-boards-mine-only");
  const allItems = mineOnlyEl ? !mineOnlyEl.checked : true;
  const boardsTypes = selectedBoardsTypes();
  setIngestSourcesSelected(true);
  if (summary) { summary.className = "provider-test"; summary.textContent = "rescanning all sources..."; }
  try {
    const scheduled = await api("/api/memory/rescan", {
      method: "POST",
      body: JSON.stringify({
        trigger: "manual",
        project_id: state.projectId,
        all_projects: false,
        sources: "all",
        limit,
        include_mcp: true,
        all_items: allItems,
        work_item_types: boardsTypes,
        ingest_mode: ingestMode,
        ...ingestTimeoutPayload([
          "docs", "code", "chat", "git", "adr", "issues", "prs", "meetings",
          "granola", "azure-boards", "azure-git", "azure-wiki", "teams-meetings",
        ]),
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
    const teams = result.teams_count ? `, ${result.teams_count} Teams→memory` : "";
    const direct = result.memory_written ? `, ${result.memory_written} memory write(s)` : "";
    if (summary) {
      summary.className = "provider-test ok";
      summary.textContent = `${result.count || 0} candidate(s), ${result.duplicates || 0} duplicate hint(s)${direct}${boards}${azureGit}${wiki}${teams}${warnings}`;
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
function setIngestSourcesSelected(selected) {
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
  const enabled = Boolean(document.querySelector('.ingest-source[value="azure-boards"]')?.checked);
  panel.hidden = !enabled;
  syncBoardsTypeSummary();
}

async function syncIngestSourcesWithMcp() {
  try {
    const payload = await api("/api/mcp/servers");
    const servers = payload.servers || [];
    const byId = Object.fromEntries(servers.map(server => [server.id, server]));
    const ado = byId["azure-devops"];
    const adoGit = byId["azure-devops-git"];
    const granola = byId.granola;
    const adoOn = Boolean(ado?.enabled);
    const adoGitOn = Boolean(adoGit?.enabled) || adoOn;
    const granolaOn = Boolean(granola?.enabled);
    const mark = (value, enabled) => {
      const input = document.querySelector(`.ingest-source[value="${value}"]`);
      if (!input) return;
      const label = input.closest(".source-checkbox");
      if (enabled) {
        input.checked = true;
        input.disabled = false;
        if (label) label.title = label.dataset.readyTitle || label.title;
      } else {
        input.disabled = false;
        if (label && !label.dataset.readyTitle) label.dataset.readyTitle = label.title;
      }
    };
    mark("azure-boards", adoOn);
    mark("azure-wiki", adoOn);
    mark("azure-git", adoGitOn);
    mark("granola", granolaOn);
    syncBoardsOptionsVisibility();
  } catch (_error) {
    // MCP list is best-effort for AutoScan defaults.
    syncBoardsOptionsVisibility();
  }
}

export {
  ingestMemorySources, refreshMemorySurfaces, rescanAllMemorySources,
  setIngestSourcesSelected, syncBoardsOptionsVisibility, syncBoardsTypeSummary,
  syncIngestSourcesWithMcp, syncStartupMemoryRescan,
};
