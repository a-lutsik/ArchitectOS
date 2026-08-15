/* Memory panel: lifecycle, list, candidates, files — extracted from app.js */
import { api } from "./api-client.js";
import { formatBytes, readFileAsDataUrl } from "./ask-ui.js";
import { escapeHtml } from "./dom-utils.js";
import { refreshMemorySurfaces } from "./memory-ingest.js";
import { loadAnalytics } from "./providers.js";
import { refreshWorkspace, runSearch, scheduleGraphLoad } from "./projects.js";
import { projectParam, state, t } from "./state.js";
import { showError } from "./ui.js";
import { createVirtualList } from "./virtual-list.js";

async function loadMemoryLifecycle() {
  const container = document.querySelector("#memory-lifecycle-dashboard");
  if (!container) return;

  try {
    const payload = await api(`/api/analytics?project_id=${projectParam()}`);
    const life = payload.memory_lifecycle || {};
    const tier = life.by_tier || {};
    const stateCounts = life.by_state || {};
    const emb = payload.embeddings || {};
    const cov = emb.coverage || {};
    const indexed = Number(cov.indexed || 0);
    const active = Number(cov.active_nodes || 0);
    const missing = Number(cov.missing || Math.max(0, active - indexed));
    const pct = cov.coverage_pct != null ? Number(cov.coverage_pct) : (active ? Math.round((indexed / active) * 1000) / 10 : 0);
    const embLabel = emb.provider ? `${emb.provider}${emb.model ? ` / ${emb.model}` : ""}${emb.dimensions ? ` · ${emb.dimensions}d` : ""}` : "";

    const total = (tier.long_term || 0) + (tier.short_term || 0);
    const longTermPct = total > 0 ? Math.round((tier.long_term || 0) / total * 100) : 0;
    const shortTermPct = total > 0 ? Math.round((tier.short_term || 0) / total * 100) : 0;

    container.innerHTML = `
      <div style="margin-bottom: 16px;">
        <strong style="color: var(--ink); font-size: 14px;">Status Breakdown</strong>
        <div style="display: flex; gap: 10px; margin-top: 8px; flex-wrap: wrap;">
          <span class="badge">Fresh: ${stateCounts.fresh || 0}</span>
          <span class="badge">Stable: ${stateCounts.stable || 0}</span>
          <span class="badge warning">Stale: ${stateCounts.stale || 0}</span>
          <span class="badge">Archived: ${stateCounts.archived || 0}</span>
        </div>
      </div>
      <div style="margin-bottom: 16px;">
        <strong style="color: var(--ink); font-size: 14px;">Tier Distribution</strong>
        <div style="margin-top: 8px;">
          <div style="font-size: 13px; margin-bottom: 4px;">Long-term: ${tier.long_term || 0} (${longTermPct}%)</div>
          <div style="font-size: 13px;">Short-term: ${tier.short_term || 0} (${shortTermPct}%)</div>
        </div>
      </div>
      <div style="margin-bottom: 16px;">
        <strong style="color: var(--ink); font-size: 14px;">${t("memory.embeddingsCoverage")}</strong>
        <div style="margin-top: 4px; font-size: 24px; font-weight: 600; color: var(--accent);">${indexed}<span style="font-size: 14px; font-weight: 500; color: var(--muted);"> / ${active}</span></div>
        <div style="font-size: 13px; color: var(--muted); margin-top: 2px;">${pct}% · ${missing} missing${embLabel ? ` · ${escapeHtml(embLabel)}` : ""}</div>
      </div>
      <div>
        <strong style="color: var(--ink); font-size: 14px;">Total Memory Items</strong>
        <div style="margin-top: 4px; font-size: 24px; font-weight: 600; color: var(--accent);">${total}</div>
      </div>
    `;
    await loadMemoryLifecycleItems();
  } catch (error) {
    container.innerHTML = '<div style="color: var(--muted);">Failed to load lifecycle data</div>';
  }
}

async function loadMemoryLifecycleItems() {
  const container = document.querySelector("#memory-lifecycle-items");
  if (!container) return;
  const stateFilter = document.querySelector("#memory-lifecycle-state-filter")?.value || "all";
  const tierFilter = document.querySelector("#memory-lifecycle-tier-filter")?.value || "all";
  try {
    const payload = await api(`/api/memory/items?project_id=${projectParam()}&lifecycle_state=${encodeURIComponent(stateFilter)}&tier=${encodeURIComponent(tierFilter)}&limit=40`);
    const items = payload.items || [];
    const total = Number(payload.total || items.length);
    if (!items.length) {
      container.innerHTML = '<div class="memory-lifecycle-empty">No memory items match this lifecycle filter.</div>';
      return;
    }
    container.innerHTML = `
      <div class="memory-lifecycle-item-count">${items.length} shown · ${total} matched</div>
      ${items.map(renderMemoryLifecycleItem).join("")}
    `;
  } catch (error) {
    container.innerHTML = `<div class="memory-lifecycle-empty">${escapeHtml(error.message || "Failed to load lifecycle items")}</div>`;
  }
}

function renderMemoryLifecycleItem(item) {
  const meta = item.metadata || {};
  const stage = meta.lifecycle_state || "unknown";
  const tier = meta.memory_tier || "unknown";
  const score = meta.memory_score != null ? `<span class="candidate-chip subtle">score ${escapeHtml(String(meta.memory_score))}</span>` : "";
  const text = String(item.text || "").trim();
  const preview = text.length > 180 ? `${text.slice(0, 177)}…` : text;
  return `
    <article class="memory-lifecycle-item">
      <div class="memory-lifecycle-item-top">
        <strong title="${escapeHtml(item.label || "")}">${escapeHtml(item.label || "Memory item")}</strong>
        <span class="candidate-chip">${escapeHtml(stage)}</span>
      </div>
      <p>${escapeHtml(preview || "No preview.")}</p>
      <div class="candidate-card-chips">
        <span class="candidate-chip subtle">${escapeHtml(tier)}</span>
        <span class="candidate-chip subtle">${escapeHtml(item.status || "active")}</span>
        ${item.type ? `<span class="candidate-chip subtle">${escapeHtml(item.type)}</span>` : ""}
        ${score}
      </div>
    </article>`;
}

// --- Memory Graph|List tab ------------------------------------------------
const MEMORY_TYPE_ICONS = {
  Decision: "🎯", Lesson: "💡", Constraint: "⚠️", Feature: "✨", Doc: "📚",
  Artifact: "📦", Task: "✅", Concept: "💭", Project: "🏗️", Provider: "🔌",
  Requirement: "📋", Meeting: "🤝",
};
function memoryTypeIcon(type) {
  return MEMORY_TYPE_ICONS[type] || "📝";
}

function switchMemoryTab(tab) {
  const graphTab = document.querySelector("#memory-tab-graph");
  const listTab = document.querySelector("#memory-tab-list");
  const graphLayout = document.querySelector(".memory-graph-section .graph-layout");
  const listPanel = document.querySelector("#memory-list-panel");
  if (!graphTab || !listTab || !graphLayout || !listPanel) return;
  const showList = tab === "list";
  graphTab.classList.toggle("active", !showList);
  graphTab.setAttribute("aria-selected", String(!showList));
  listTab.classList.toggle("active", showList);
  listTab.setAttribute("aria-selected", String(showList));
  // Inline display: CSS display rules on these containers override the hidden attribute.
  graphLayout.style.display = showList ? "none" : "";
  listPanel.style.display = showList ? "" : "none";
  if (showList) loadMemoryList().catch(showError);
}

async function loadMemoryList() {
  const container = document.querySelector("#memory-list-items");
  if (!container) return;
  const tier = document.querySelector("#memory-list-tier")?.value || "all";
  const stage = document.querySelector("#memory-list-state")?.value || "all";
  const query = (document.querySelector("#memory-list-search")?.value || "").trim().toLowerCase();
  const countEl = document.querySelector("#memory-list-count");
  try {
    const payload = await api(`/api/memory/items?project_id=${projectParam()}&lifecycle_state=${encodeURIComponent(stage)}&tier=${encodeURIComponent(tier)}&limit=150`);
    let items = payload.items || [];
    const total = Number(payload.total || items.length);
    if (query) {
      items = items.filter(item => `${item.label || ""} ${item.text || ""}`.toLowerCase().includes(query));
    }
    if (countEl) countEl.textContent = `${items.length} shown · ${total} matched`;
    if (!items.length) {
      resetMemoryListView();
      container.innerHTML = '<div class="memory-lifecycle-empty">No memory items match these filters.</div>';
      return;
    }
    renderMemoryListItems(container, items);
  } catch (error) {
    resetMemoryListView();
    container.innerHTML = `<div class="memory-lifecycle-empty">${escapeHtml(error.message || "Failed to load memory items")}</div>`;
  }
}

// The list is windowed (see virtual-list.js): only viewport rows + overscan
// exist in the DOM instead of one innerHTML for up to 150 cards.
let memoryListView = null; // { container, vlist }

function memoryListItemHtml(item) {
  const meta = item.metadata || {};
  const text = String(item.text || "").trim();
  const preview = text.length > 200 ? `${text.slice(0, 197)}…` : text;
  const updated = String(item.updated_at || "").slice(0, 10);
  return `
    <article class="memory-list-item" data-node-id="${escapeHtml(item.id || "")}">
      <div class="memory-list-item-icon">${memoryTypeIcon(item.type)}</div>
      <div class="memory-list-item-body">
        <div class="memory-list-item-top">
          <strong title="${escapeHtml(item.label || "")}">${escapeHtml(item.label || "Memory item")}</strong>
          <span class="muted">${escapeHtml(updated)}</span>
        </div>
        <p>${escapeHtml(preview || "No preview.")}</p>
        <div class="candidate-card-chips">
          ${item.type ? `<span class="candidate-chip">${escapeHtml(item.type)}</span>` : ""}
          <span class="candidate-chip subtle">${escapeHtml(meta.memory_tier || "unknown")}</span>
          <span class="candidate-chip subtle">${escapeHtml(meta.lifecycle_state || "unknown")}</span>
          ${meta.favorite || meta.pinned ? '<span class="candidate-chip">★</span>' : ""}
        </div>
      </div>
    </article>`;
}

function renderMemoryListItems(container, items) {
  if (!memoryListView || memoryListView.container !== container) {
    resetMemoryListView();
    // The 8px row gap moves from the container (now spacing spacers too) to
    // the window holder, keeping row spacing identical to the old innerHTML.
    container.style.gap = "0px";
    memoryListView = {
      container,
      vlist: createVirtualList({
        container,
        renderRow: item => {
          const wrap = document.createElement("div");
          wrap.innerHTML = memoryListItemHtml(item);
          return wrap.firstElementChild || wrap.firstChild;
        },
        estimatedRowHeight: 120, // includes the 8px gap; measured after render
        overscan: 5,
        gap: 8, // .memory-list-items { gap: 8px }
        holderStyle: "display:flex;flex-direction:column;gap:8px;",
      }),
    };
  }
  memoryListView.vlist.setRows(items);
}

function resetMemoryListView() {
  if (!memoryListView) return;
  memoryListView.vlist.destroy();
  memoryListView.container.style.gap = "";
  memoryListView = null;
}

async function loadMemoryCandidates() {
  const container = document.querySelector("#candidate-list");
  if (!container) return;
  syncCandidateBatchActions();
  const filter = document.querySelector("#candidate-status-filter")?.value || "candidate";
  const listStatus = filter === "duplicate" ? "candidate" : filter;
  const payload = await api(`/api/memory/candidates?project_id=${projectParam()}&status=${encodeURIComponent(listStatus)}&limit=80`);
  let candidates = payload.candidates || [];
  if (filter === "duplicate") {
    candidates = candidates.filter(item => item.metadata && item.metadata.duplicate);
  }
  renderCandidateStatusStats(payload.counts || payload);
  const countEl = document.querySelector("#candidates-count");
  if (countEl) {
    const total = Number((payload.counts || {}).total ?? candidates.length) || candidates.length;
    const shown = candidates.length;
    if (filter === "all") {
      countEl.textContent = `${shown} shown · ${total} total`;
    } else if (shown < total && (filter === "candidate" || filter === "promoted" || filter === "rejected")) {
      const label = filter === "candidate" ? "pending" : filter === "promoted" ? "accepted" : "rejected";
      const scoped = Number((payload.counts || {})[filter === "candidate" ? "pending" : filter === "promoted" ? "accepted" : "rejected"] || shown);
      countEl.textContent = `${shown} shown · ${scoped} ${label}`;
    } else {
      const noun = shown === 1 ? "item" : "items";
      countEl.textContent = `${shown} ${noun}`;
    }
  }
  if (!candidates.length) {
    container.innerHTML = `
      <div class="candidate-empty">
        <strong>Nothing to review here</strong>
        <p>Run AutoScan, or switch the filter to see accepted / rejected items.</p>
      </div>`;
    return;
  }
  container.innerHTML = "";
  for (const candidate of candidates) {
    container.appendChild(renderCandidateCard(candidate));
  }
  container.querySelectorAll("[data-promote-candidate]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    await api(`/api/memory/candidates/${button.dataset.promoteCandidate}/promote`, { method: "POST", body: "{}" });
    await loadMemoryCandidates();
    await loadMemoryLifecycle();
    await loadAnalytics();
    await runSearch(document.querySelector("#search-query").value || "memory");
    await refreshWorkspace();
    scheduleGraphLoad();
  }));
  container.querySelectorAll("[data-reject-candidate]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    await api(`/api/memory/candidates/${button.dataset.rejectCandidate}/reject`, { method: "POST", body: JSON.stringify({ reason: "Rejected in Memory UI" }) });
    await refreshMemorySurfaces();
  }));
}

function renderCandidateStatusStats(counts) {
  const root = document.querySelector("#candidates-status-stats");
  const pending = Number(counts?.pending || 0);
  const accepted = Number(counts?.accepted || 0);
  const rejected = Number(counts?.rejected || 0);
  const duplicate = Number(counts?.duplicate || 0);
  const badge = document.querySelector("#nav-memory-review-badge");
  if (badge) {
    badge.textContent = pending > 99 ? "99+" : String(pending);
    badge.hidden = pending <= 0;
    badge.title = `${pending} memory candidate(s) waiting for review`;
  }
  if (!root) return;
  const set = (id, value) => {
    const el = document.querySelector(id);
    if (el) el.textContent = String(value);
  };
  set("#candidates-stat-pending", pending);
  set("#candidates-stat-accepted", accepted);
  set("#candidates-stat-rejected", rejected);
  set("#candidates-stat-duplicate", duplicate);
  root.querySelectorAll(".candidates-stat").forEach(el => {
    const key = el.dataset.stat;
    const value = key === "pending" ? pending : key === "accepted" ? accepted : key === "rejected" ? rejected : duplicate;
    el.classList.toggle("is-zero", value === 0);
    el.classList.toggle("is-active", value > 0);
  });
}

function candidateSourceLabel(sourceType) {
  const map = {
    docs: "Repo docs",
    code: "Code",
    chat: "App chat",
    git: "Git",
    adr: "ADR",
    issues: "Issue files",
    prs: "PR notes",
    meetings: "Meeting notes",
    granola: "Granola",
    "azure-boards": "Azure Boards",
    "azure-git": "Azure Git",
    "azure-wiki": "Azure Wiki",
    "teams-meetings": "Teams",
  };
  return map[String(sourceType || "").toLowerCase()] || String(sourceType || "Memory");
}

function candidateIconLetter(sourceType) {
  const label = candidateSourceLabel(sourceType);
  return (label.trim().charAt(0) || "M").toUpperCase();
}

function candidateTitle(candidate) {
  const label = String(candidate.label || "").trim();
  const cleaned = label.replace(/^(Code|Doc|Docs|ADR|Issue|PR|Meeting|Git|Chat|Wiki|Teams)\s*:\s*/i, "").trim();
  const path = String(candidate.source_ref || (candidate.metadata || {}).path || cleaned);
  const base = path.split(/[\\/]/).filter(Boolean).pop() || cleaned || "Memory item";
  return base;
}

function candidatePath(candidate) {
  const meta = candidate.metadata || {};
  const ref = String(candidate.source_ref || meta.path || meta.relative_path || "").trim();
  if (!ref) return "";
  const title = candidateTitle(candidate);
  if (ref === title) return "";
  return ref.length > 72 ? `…${ref.slice(-70)}` : ref;
}

function candidateWhy(candidate) {
  const meta = candidate.metadata || {};
  const source = String(candidate.source_type || meta.source_type || "").toLowerCase();
  if (meta.duplicate) {
    const similar = meta.duplicate_label ? ` Similar to “${meta.duplicate_label}”.` : "";
    return `Possible duplicate.${similar}`;
  }
  const reasons = {
    code: "Found in project source files.",
    docs: "Found in local repository docs.",
    git: "Extracted from recent git history.",
    chat: "Captured from ArchitectOS chat.",
    adr: "Architecture decision record.",
    issues: "Local issue/bug markdown file.",
    prs: "Local PR notes or template.",
    meetings: "Local meeting notes file.",
    granola: "Imported from Granola.",
    "azure-boards": "Azure Boards work item.",
    "azure-git": "Azure Repos repository or pull request.",
    "azure-wiki": "Azure Wiki page.",
    "teams-meetings": "Teams transcript or AI insights.",
  };
  return reasons[source] || "Suggested by AutoScan for durable memory.";
}

function candidatePreviewText(candidate) {
  let text = String(candidate.text || "").trim();
  text = text
    .replace(/^File\s+.+\s+imported into ArchitectOS memory\.?\s*/i, "")
    .replace(/^Code:\s*.+\n+/i, "")
    .replace(/\r/g, "");
  const lines = text.split("\n").map(line => line.trimEnd()).filter(line => line.trim());
  const useful = lines.filter(line => {
    const trimmed = line.trim();
    if (!trimmed) return false;
    if (/^package\s+/.test(trimmed)) return false;
    if (/^import\s+/.test(trimmed)) return false;
    if (/^\/\//.test(trimmed) && trimmed.length < 40) return false;
    if (/^\*\s*Copyright/i.test(trimmed)) return false;
    if (/^#\s*!/.test(trimmed)) return false;
    return true;
  });
  const preview = (useful.length ? useful : lines).slice(0, 4).join("\n").trim();
  if (!preview) return "No readable preview.";
  return preview.length > 220 ? `${preview.slice(0, 217)}…` : preview;
}

function renderCandidateCard(candidate) {
  const el = document.createElement("article");
  const meta = candidate.metadata || {};
  const sourceType = String(candidate.source_type || meta.source_type || "manual");
  const isDuplicate = Boolean(meta.duplicate);
  const canPromote = candidate.status !== "promoted";
  const canReject = candidate.status === "candidate";
  const status = String(candidate.status || "candidate");
  el.className = `candidate-card${isDuplicate ? " is-duplicate" : ""}`;
  const path = candidatePath(candidate);
  const fullText = String(candidate.text || "").trim();
  el.innerHTML = `
    <div class="candidate-card-layout">
      <div class="candidate-card-icon" data-source="${escapeHtml(sourceType)}" aria-hidden="true">${escapeHtml(candidateIconLetter(sourceType))}</div>
      <div class="candidate-card-body">
        <div class="candidate-card-top">
          <div class="candidate-card-heading">
            <h5 title="${escapeHtml(candidate.label || "")}">${escapeHtml(candidateTitle(candidate))}</h5>
            <div class="candidate-card-chips">
              <span class="candidate-chip">${escapeHtml(candidateSourceLabel(sourceType))}</span>
              ${candidate.type ? `<span class="candidate-chip subtle">${escapeHtml(candidate.type)}</span>` : ""}
              ${status !== "candidate" ? `<span class="candidate-chip subtle">${escapeHtml(status === "promoted" ? "accepted" : status)}</span>` : ""}
              ${isDuplicate ? `<span class="candidate-chip warning">duplicate</span>` : ""}
            </div>
          </div>
          <div class="candidate-card-actions">
            ${canPromote ? `<button data-promote-candidate="${escapeHtml(candidate.id)}" type="button" class="candidate-action candidate-action-accept">Accept</button>` : ""}
            ${canReject ? `<button data-reject-candidate="${escapeHtml(candidate.id)}" type="button" class="candidate-action candidate-action-reject">Reject</button>` : ""}
          </div>
        </div>
        ${path ? `<p class="candidate-card-path" title="${escapeHtml(candidate.source_ref || path)}">${escapeHtml(path)}</p>` : ""}
        <p class="candidate-card-why">${escapeHtml(candidateWhy(candidate))}</p>
        <pre class="candidate-card-preview">${escapeHtml(candidatePreviewText(candidate))}</pre>
        ${fullText.length > 220 ? `<details class="candidate-card-more"><summary>View full text</summary><pre>${escapeHtml(fullText)}</pre></details>` : ""}
      </div>
    </div>`;
  return el;
}

async function batchUpdateCandidates(body) {
  closeCandidatesMoreMenu();
  const summary = document.querySelector("#candidate-batch-summary") || document.querySelector("#candidate-summary");
  const acceptBtn = document.querySelector("#candidates-accept-filtered");
  const rejectBtn = document.querySelector("#candidates-reject-filtered");
  const pendingHint = Number(document.querySelector("#candidates-stat-pending")?.textContent || 0);
  if (summary) {
    summary.className = "provider-test candidates-batch-summary";
    summary.hidden = false;
    summary.textContent = pendingHint > 200
      ? `Updating full queue (${pendingHint.toLocaleString()} items)… this can take a while`
      : "Updating full queue…";
  }
  if (acceptBtn) acceptBtn.disabled = true;
  if (rejectBtn) rejectBtn.disabled = true;
  let payload;
  try {
    payload = await api("/api/memory/candidates/batch", {
      method: "POST",
      body: JSON.stringify({ project_id: state.projectId, all: true, ...body })
    });
  } finally {
    syncCandidateBatchActions();
  }
  if (summary) {
    const errCount = payload.error_count || (payload.errors && payload.errors.length) || 0;
    summary.className = errCount ? "provider-test error candidates-batch-summary" : "provider-test ok candidates-batch-summary";
    const parts = [
      payload.action === "promote" ? `Accepted ${payload.promoted || 0}` : `Rejected ${payload.rejected || 0}`,
      payload.matched != null ? `matched ${payload.matched}` : "",
      payload.skipped ? `skipped ${payload.skipped}` : "",
      payload.counts ? `pending now ${payload.counts.pending ?? payload.pending ?? 0}` : "",
      errCount ? `errors ${errCount}` : ""
    ].filter(Boolean);
    summary.textContent = parts.join(" · ");
  }
  if (payload.counts) renderCandidateStatusStats(payload.counts);
  await refreshMemorySurfaces();
  if (payload.promoted) {
    await runSearch(document.querySelector("#search-query").value || "memory");
    await refreshWorkspace();
    scheduleGraphLoad();
  }
  return payload;
}

function candidateBatchFilterFromUi() {
  const filter = document.querySelector("#candidate-status-filter")?.value || "candidate";
  if (filter === "duplicate") return { status: "duplicate", duplicate_only: true };
  if (filter === "all") return { status: "all" };
  return { status: filter };
}

function syncCandidateBatchActions() {
  const filter = document.querySelector("#candidate-status-filter")?.value || "candidate";
  const acceptBtn = document.querySelector("#candidates-accept-filtered");
  const rejectBtn = document.querySelector("#candidates-reject-filtered");
  const labels = {
    candidate: { accept: "Accept all", reject: "Reject all" },
    duplicate: { accept: "Accept duplicates", reject: "Reject duplicates" },
    rejected: { accept: "Accept rejected", reject: "Reject again" },
    promoted: { accept: "Accept promoted", reject: "Reject promoted" },
    all: { accept: "Accept all", reject: "Reject all" },
  };
  const pair = labels[filter] || labels.candidate;
  if (acceptBtn) {
    acceptBtn.textContent = pair.accept;
    acceptBtn.disabled = filter === "promoted";
    acceptBtn.title = filter === "promoted"
      ? "Already accepted items stay in memory"
      : `Accept the entire ${filter === "candidate" ? "pending" : filter} queue (not only the ${document.querySelector("#candidates-count")?.textContent || "shown"} list)`;
  }
  if (rejectBtn) {
    rejectBtn.textContent = pair.reject;
    rejectBtn.disabled = filter === "promoted";
    rejectBtn.title = filter === "promoted"
      ? "Accepted memory is not removed by batch reject"
      : `Reject the entire ${filter === "candidate" ? "pending" : filter} queue (not only the shown list)`;
  }
}

function closeCandidatesMoreMenu() {
  document.querySelectorAll(".candidates-more[open]").forEach(item => {
    item.open = false;
  });
}
function renderMemoryFiles() {
  const list = document.querySelector("#memory-file-list");
  if (!list) return;

  if (!state.memoryFiles.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📁</div>
        <div class="empty-state-title">No files selected</div>
        <div class="empty-state-text">Choose text, docs, or code files to write into memory.</div>
      </div>
    `;
    return;
  }

  list.innerHTML = "";
  for (const file of state.memoryFiles) {
    const el = document.createElement("article");
    el.className = "result";
    el.innerHTML = `<div class="row"><strong>${escapeHtml(file.name)}</strong><button data-remove-memory-file="${escapeHtml(file.id)}" type="button">Remove</button></div><p>${escapeHtml(formatBytes(file.size))}${file.text_extracted ? ` · ${escapeHtml(String(file.chars || 0))} chars` : " · no text extracted"}</p><span class="badge">${escapeHtml(file.mime || "file")}</span>`;
    list.appendChild(el);
  }
  list.querySelectorAll("[data-remove-memory-file]").forEach(button => button.addEventListener("click", () => {
    state.memoryFiles = state.memoryFiles.filter(file => file.id !== button.dataset.removeMemoryFile);
    renderMemoryFiles();
  }));
}
async function handleMemoryFileSelect(fileList) {
  const summary = document.querySelector("#memory-file-summary");
  const files = Array.from(fileList || []);
  if (!files.length) return;
  if (summary) { summary.className = "provider-test"; summary.textContent = "uploading files..."; }
  for (const file of files) {
    const content = await readFileAsDataUrl(file);
    const result = await api("/api/files", { method: "POST", body: JSON.stringify({ project_id: state.projectId, name: file.name, content }) });
    if (result.file) state.memoryFiles.push(result.file);
  }
  renderMemoryFiles();
  if (summary) { summary.className = "provider-test ok"; summary.textContent = `${state.memoryFiles.length} file(s) ready`; }
}
async function importMemoryFiles() {
  const summary = document.querySelector("#memory-file-summary");
  if (!state.memoryFiles.length) throw new Error("Choose at least one file first.");
  if (summary) { summary.className = "provider-test"; summary.textContent = "writing files to memory..."; }
  const payload = await api("/api/memory/files", {
    method: "POST",
    body: JSON.stringify({
      project_id: state.projectId,
      file_ids: state.memoryFiles.map(file => file.id),
      type: document.querySelector("#memory-file-type")?.value || "Artifact",
      scope: document.querySelector("#memory-file-scope")?.value || "project",
    }),
  });
  state.memoryFiles = [];
  renderMemoryFiles();
  if (summary) {
    summary.className = payload.skipped && payload.skipped.length ? "provider-test error" : "provider-test ok";
    summary.textContent = `${payload.count} file memory item(s) added${payload.skipped && payload.skipped.length ? `, ${payload.skipped.length} skipped` : ""}`;
  }
  await runSearch("file memory");
  await refreshWorkspace();
  scheduleGraphLoad();
}

export {
  batchUpdateCandidates, handleMemoryFileSelect, importMemoryFiles,
  loadMemoryCandidates, loadMemoryLifecycle, loadMemoryLifecycleItems, loadMemoryList,
  renderMemoryFiles, switchMemoryTab, syncCandidateBatchActions,
};
