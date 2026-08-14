/* Memory graph engine: state, physics, canvas, detail/actions — extracted from app.js */
let graphSuggestions = [];

async function suggestGraphLinks() {
  const panel = document.querySelector("#graph-suggest-panel");
  const status = document.querySelector("#graph-suggest-status");
  const list = document.querySelector("#graph-suggest-list");
  const applyButton = document.querySelector("#graph-suggest-apply");
  if (!panel || !status || !list) return;
  // The panel lives inside the graph layout: invisible while List tab is active.
  if (document.querySelector("#memory-tab-list")?.classList.contains("active")) {
    switchMemoryTab("graph");
  }
  const suggestButton = document.querySelector("#graph-suggest-links");
  if (suggestButton) suggestButton.disabled = true;
  panel.hidden = false;
  applyButton.disabled = true;
  list.innerHTML = "";
  status.textContent = "AI is analyzing unlinked memory nodes — usually 10–30 seconds...";
  let payload;
  try {
    payload = await api("/api/graph/suggest-links", {
      method: "POST",
      body: JSON.stringify({ project_id: state.projectId, limit: 24, pair_limit: 10 }),
    });
  } catch (error) {
    status.textContent = `Suggest failed: ${error.message || error}`;
    if (suggestButton) suggestButton.disabled = false;
    throw error;
  }
  if (suggestButton) suggestButton.disabled = false;
  graphSuggestions = (payload.suggestions || []).filter(item => item.related && !item.llm_error);
  const skipped = (payload.suggestions || []).length - graphSuggestions.length;
  status.textContent = `pool ${payload.pool_size} · pairs ${payload.pairs_considered} · ${graphSuggestions.length} suggested` +
    (skipped > 0 ? ` · ${skipped} rejected by AI` : "");
  if (!graphSuggestions.length) {
    list.innerHTML = '<div class="graph-suggest-empty">No contextual links found among the least-linked nodes.</div>';
    return;
  }
  list.innerHTML = "";
  graphSuggestions.forEach((item, index) => {
    const row = document.createElement("label");
    row.className = "graph-suggest-item";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    checkbox.dataset.index = String(index);
    checkbox.addEventListener("change", () => {
      applyButton.disabled = !list.querySelector("input[type=checkbox]:checked");
    });
    const body = document.createElement("span");
    body.className = "graph-suggest-body";
    body.textContent = `${item.source_label} ↔ ${item.target_label}`;
    const meta = document.createElement("span");
    meta.className = "graph-suggest-meta";
    meta.textContent = `${item.edge_type} · conf ${item.confidence}${item.reason ? ` · ${item.reason}` : ""}`;
    row.appendChild(checkbox);
    row.appendChild(body);
    row.appendChild(meta);
    list.appendChild(row);
  });
  applyButton.disabled = false;
}

async function applySuggestedLinks() {
  const list = document.querySelector("#graph-suggest-list");
  const status = document.querySelector("#graph-suggest-status");
  const applyButton = document.querySelector("#graph-suggest-apply");
  if (!list || !status) return;
  const selected = [...list.querySelectorAll("input[type=checkbox]:checked")]
    .map(box => graphSuggestions[Number(box.dataset.index)])
    .filter(Boolean);
  if (!selected.length) return;
  applyButton.disabled = true;
  let created = 0;
  for (const item of selected) {
    try {
      await api("/api/graph/edges", {
        method: "POST",
        body: JSON.stringify({ source: item.source_id, target: item.target_id, type: item.edge_type, confidence: item.confidence }),
      });
      created += 1;
    } catch (error) {
      console.warn("suggest-links: edge failed", item, error);
    }
  }
  status.textContent = `Applied ${created} link(s).`;
  list.innerHTML = "";
  graphSuggestions = [];
  await loadGraph();
}

const graphState = {
  nodes: [],
  edges: [],
  allNodes: [],
  allEdges: [],
  particles: [],
  selectedId: "",
  hoverId: "",
  draggingId: "",
  dragMoved: false,
  modalOpen: false,
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  userZoomed: false,
  animationId: 0,
  initialized: false,
  expanded: false,
  stageObserver: null,
  physicsTicks: 0,
  physicsMax: 90,
  physicsActive: true,
  searchQuery: "",
  searchTimer: 0,
  groupFilter: "",
  densityLevel: 2,
};

const graphGroupPalette = {
  Projects: "#60a5fa",
  Decisions: "#2563eb",
  Rules: "#f97316",
  Requirements: "#22c55e",
  Knowledge: "#14b8a6",
  Sources: "#f59e0b",
  Code: "#0ea5e9",
  Operations: "#a78bfa",
  People: "#f472b6",
  Data: "#2dd4bf",
  Other: "#94a3b8",
};

const graphPalette = {
  Project: "#4f46e5",
  Decision: "#2563eb",
  Lesson: "#15803d",
  Constraint: "#b42318",
  Feature: "#b45309",
  Provider: "#7c3aed",
  Doc: "#0891b2",
  Artifact: "#475569",
  Symbol: "#0ea5e9",
  Task: "#0f766e",
  Concept: "#6b7280",
};

// Code-structure edges get a subtle per-type tint so the code graph reads apart
// from knowledge links. INFERRED code edges (CALLS) still render dashed/faint via
// their provenance in the draw loop.
const graphCodeEdgeColors = {
  DEFINES: "#0ea5e9",
  CONTAINS: "#38bdf8",
  IMPORTS: "#0d9488",
  CALLS: "#8b5cf6",
};

function graphNodeGroup(node) {
  const meta = node?.metadata || {};
  const source = String(meta.source_type || meta.source || node?.source_type || "").toLowerCase();
  const type = String(node?.type || "Concept");
  if (type === "Project") return "Projects";
  if (type === "Symbol") return "Code";
  if (["Decision", "Constraint"].includes(type)) return "Decisions";
  if (type === "Rule") return "Rules";
  if (["Requirement", "Feature"].includes(type)) return "Requirements";
  if (["Doc", "Artifact", "Meeting"].includes(type) || /docs?|code|git|wiki|boards|teams|granola|pr|issue/.test(source)) return "Sources";
  if (["Provider", "Task"].includes(type)) return "Operations";
  if (/user|people|person|team/.test(source)) return "People";
  if (/data|metric|dataset/.test(source)) return "Data";
  if (["Lesson", "Concept"].includes(type)) return "Knowledge";
  return "Other";
}

/** Canonical ingest/source key for a memory node (mirrors backend _source_key_for_node). */
function graphNodeSourceKey(node) {
  const meta = node?.metadata || {};
  if (meta.source_key) return String(meta.source_key).toLowerCase();
  const raw = String(meta.source_type || meta.source || node?.source_type || "").trim().toLowerCase();
  if (!raw || raw === "source_hub" || raw === "scope_root" || raw === "project_profile") return "other";
  const aliases = {
    doc: "docs", documentation: "docs", ui: "manual", memory_candidate: "manual",
    promoted_candidate: "manual", azure_boards: "azure-boards", boards: "azure-boards",
    azure_wiki: "azure-wiki", wiki: "azure-wiki", azure_git: "azure-git",
    azure_repos: "azure-git", ado_git: "azure-git", ado_repos: "azure-git",
    teams: "teams-meetings", teams_meetings: "teams-meetings", ms_teams: "teams-meetings",
    facilitator: "teams-meetings", pr: "prs", pull_request: "prs", pull_requests: "prs",
  };
  return aliases[raw] || raw;
}

function graphGroupColor(group) {
  return graphGroupPalette[group] || graphPalette[group] || "#64748b";
}

// Distinct, colorblind-friendly palette for detected communities (themes).
const graphCommunityPalette = [
  "#4E79A7", "#F28E2B", "#59A14F", "#E15759", "#B07AA1",
  "#76B7B2", "#EDC948", "#FF9DA7", "#9C755F", "#72B043",
  "#8CD17D", "#D37295", "#B6992D", "#86BCB6", "#D4A6C8",
];

function graphCommunityColor(cid) {
  if (!Number.isInteger(cid) || cid < 0) return "#94a3b8";
  return graphCommunityPalette[cid % graphCommunityPalette.length];
}

/** Fill color for a node, honoring the theme/group color mode. */
function graphNodeColor(node) {
  const mode = graphState.colorMode || "community";
  const synthetic = Boolean(node?.metadata?.synthetic) || ["Project", "Task", "Provider"].includes(node?.type);
  if (mode === "community" && !synthetic && Number.isInteger(node?.community) && node.community >= 0) {
    // Grey out singleton "themes" — one node is not a subsystem.
    const size = graphState.communitySize?.get(node.community);
    if (!size || size > 1) return graphCommunityColor(node.community);
  }
  return node.color || graphGroupColor(node.group) || graphPalette[node.type] || "#64748b";
}

function enrichGraphNode(node) {
  const meta = node.metadata || {};
  const group = graphNodeGroup(node);
  return {
    ...node,
    group,
    color: graphGroupColor(group),
    pinned: Boolean(meta.favorite || meta.pinned || node.pinned),
  };
}

/** Density budget: large graphs show a readable subset, fair-split by source. */
function graphVisibleBudget(total, densityLevel = graphState.densityLevel) {
  const level = Math.max(1, Math.min(5, Number(densityLevel) || 2));
  if (level >= 5) return Math.min(total, 500);
  if (level === 4) return Math.min(total, 350);
  if (level === 3) return Math.min(total, 280);
  if (level === 2) return Math.min(total, 200);
  return Math.min(total, 100);
}

function graphBackendLimit() {
  return { 1: 100, 2: 200, 3: 280, 4: 360, 5: 520 }[Math.max(1, Math.min(5, Number(graphState.densityLevel) || 2))] || 200;
}

function selectVisibleGraphNodes(nodes, edges, budget, selectedId) {
  if (nodes.length <= budget) return nodes.slice();
  const degree = new Map();
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  }
  const neighborIds = new Set();
  if (selectedId) {
    for (const edge of edges) {
      if (edge.source === selectedId) neighborIds.add(edge.target);
      if (edge.target === selectedId) neighborIds.add(edge.source);
    }
  }
  const must = [];
  const pools = new Map();
  const seen = new Set();
  for (const node of nodes) {
    if (!node?.id || seen.has(node.id)) continue;
    seen.add(node.id);
    const isMust =
      node.type === "Project"
      || node.pinned
      || node.id === selectedId
      || neighborIds.has(node.id);
    if (isMust) {
      must.push(node);
      continue;
    }
    const key = graphNodeSourceKey(node) || "other";
    if (!pools.has(key)) pools.set(key, []);
    pools.get(key).push(node);
  }
  const rank = (a, b) => {
    const da = degree.get(a.id) || 0;
    const db = degree.get(b.id) || 0;
    if (db !== da) return db - da;
    return String(a.label || "").localeCompare(String(b.label || ""));
  };
  for (const bucket of pools.values()) bucket.sort(rank);

  const out = [];
  const outIds = new Set();
  for (const node of must) {
    if (outIds.has(node.id)) continue;
    out.push(node);
    outIds.add(node.id);
  }
  const hardCap = Math.max(budget, must.length);
  const remaining = Math.max(0, hardCap - out.length);
  const sourceKeys = [...pools.keys()].sort();
  if (!sourceKeys.length || remaining <= 0) return out.slice(0, hardCap);

  const base = Math.floor(remaining / sourceKeys.length);
  const bonus = remaining % sourceKeys.length;
  const leftovers = new Map();
  sourceKeys.forEach((key, index) => {
    const quota = base + (index < bonus ? 1 : 0);
    const bucket = pools.get(key) || [];
    const take = Math.min(quota, bucket.length);
    for (let i = 0; i < take; i += 1) {
      const node = bucket[i];
      if (outIds.has(node.id)) continue;
      out.push(node);
      outIds.add(node.id);
    }
    leftovers.set(key, bucket.slice(take));
  });

  let slotsLeft = hardCap - out.length;
  while (slotsLeft > 0) {
    let progressed = false;
    for (const key of sourceKeys) {
      const queue = leftovers.get(key) || [];
      if (!queue.length) continue;
      const node = queue.shift();
      if (!node || outIds.has(node.id)) continue;
      out.push(node);
      outIds.add(node.id);
      slotsLeft -= 1;
      progressed = true;
      if (slotsLeft <= 0) break;
    }
    if (!progressed) break;
  }
  return out.slice(0, hardCap);
}

function applyGraphVisibility() {
  const query = graphState.searchQuery.trim().toLowerCase();
  const group = graphState.groupFilter || "";
  let baseNodes = graphState.allNodes;
  // Groups → Projects: show the project root plus its immediate hubs/children,
  // otherwise the filter collapses to a single lonely node and looks "broken".
  if (group === "Projects") {
    const projectIds = new Set(
      graphState.allNodes
        .filter(node => node.type === "Project" || node.group === "Projects")
        .map(node => node.id)
    );
    const keep = new Set(projectIds);
    for (const edge of graphState.allEdges) {
      if (projectIds.has(edge.source)) keep.add(edge.target);
      if (projectIds.has(edge.target)) keep.add(edge.source);
    }
    baseNodes = graphState.allNodes.filter(node => keep.has(node.id));
  }
  const filteredNodes = baseNodes.filter(node => {
    if (group && group !== "Projects" && node.group !== group) return false;
    if (!query) return true;
    const meta = node.metadata || {};
    const haystack = [
      node.id,
      node.label,
      node.text,
      node.type,
      node.scope,
      node.group,
      meta.source,
      meta.work_item_id,
      meta.meeting_id,
      meta.wiki_page_key,
    ].map(value => String(value || "").toLowerCase()).join(" ");
    return haystack.includes(query);
  });
  const filteredIds = new Set(filteredNodes.map(node => node.id));
  const filteredEdges = graphState.allEdges.filter(edge => filteredIds.has(edge.source) && filteredIds.has(edge.target));
  const budget = graphVisibleBudget(filteredNodes.length);
  graphState.nodes = selectVisibleGraphNodes(
    filteredNodes,
    filteredEdges,
    budget,
    graphState.selectedId,
  );
  const visible = new Set(graphState.nodes.map(node => node.id));
  graphState.edges = filteredEdges.filter(edge => visible.has(edge.source) && visible.has(edge.target));
  renderGraphLegend();
  updateGraphDensityHint();
}

function updateGraphDensityHint() {
  const el = document.querySelector("#graph-density-hint");
  if (!el) return;
  const total = graphState.totalNodesCount || graphState.allNodes.length;
  const shown = graphState.nodes.length;
  if (!total || shown >= total) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = `Showing ${shown} of ${total} node(s) · density ${graphState.densityLevel}/5 · fair by source`;
}

async function loadGraph() {
  await syncGraphFilters();
  const taskFilterValue = document.querySelector("#graph-task-filter")?.value || "";
  const providerFilterValue = document.querySelector("#graph-provider-filter")?.value || "";
  const pinnedOnly = document.querySelector("#graph-pinned-filter")?.checked;
  graphState.searchQuery = document.querySelector("#graph-search")?.value || "";
  graphState.groupFilter = document.querySelector("#graph-group-filter")?.value || "";
  graphState.densityLevel = Number(document.querySelector("#graph-density-level")?.value || graphState.densityLevel || 2);
  const sourceFilter = document.querySelector("#graph-source-filter");
  const scopeFilter = document.querySelector("#graph-scope-filter");
  const selectedSource = sourceFilter ? sourceFilter.value : "";
  const selectedScope = scopeFilter ? scopeFilter.value : "";
  const searchQuery = String(graphState.searchQuery || "").trim();
  
  const params = [`project_id=${projectParam()}`];
  if (taskFilterValue) params.push(`task_id=${encodeURIComponent(taskFilterValue)}`);
  if (providerFilterValue) params.push(`provider_id=${encodeURIComponent(providerFilterValue)}`);
  if (pinnedOnly) params.push("pinned=1");
  if (selectedSource) params.push(`source=${encodeURIComponent(selectedSource)}`);
  if (selectedScope) params.push(`scope=${encodeURIComponent(selectedScope)}`);
  if (searchQuery) params.push(`q=${encodeURIComponent(searchQuery)}`);
  params.push(`limit=${graphBackendLimit()}`);
  
  const payload = await api(`/api/graph?${params.join("&")}`);
  const sourceOptions = Array.isArray(payload.sources) ? payload.sources : [];
  if (sourceFilter) {
    const current = sourceFilter.value;
    sourceFilter.innerHTML = '<option value="">All sources</option>' + sourceOptions.map(item => {
      const id = typeof item === "string" ? item : (item.id || "");
      const label = typeof item === "string" ? item : (item.label || item.id || "");
      if (!id) return "";
      return `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`;
    }).join("");
    sourceFilter.value = sourceOptions.some(item => (typeof item === "string" ? item : item.id) === current) ? current : "";
  }
  
  graphState.totalNodesCount = payload.total_nodes || payload.nodes.length;
  graphState.colorMode = document.querySelector("#graph-color-mode")?.value || graphState.colorMode || "community";
  graphState.communities = Array.isArray(payload.communities) ? payload.communities : [];
  graphState.communitySize = new Map(graphState.communities.map(item => [item.id, item.size]));
  
  graphState.allNodes = payload.nodes
    .map(enrichGraphNode)
    .filter(node => {
      if (!selectedSource) return true;
      if (node.type === "Project" || node.group === "Projects") return true;
      return graphNodeSourceKey(node) === selectedSource;
    })
    .filter(node => !selectedScope || node.scope === selectedScope);
  const allVisible = new Set(graphState.allNodes.map(node => node.id));
  graphState.allEdges = payload.edges.filter(edge => allVisible.has(edge.source) && allVisible.has(edge.target));
  if (graphState.selectedId && !allVisible.has(graphState.selectedId)) graphState.selectedId = "";
  applyGraphVisibility();
  graphState.physicsTicks = 0;
  graphState.physicsMax = Math.max(50, Math.round(140 / Math.sqrt(Math.max(1, graphState.nodes.length) / 50)));
  graphState.physicsActive = true;
  seedGraphParticles();
  bindGraphOnce();
  const needle = searchQuery.toLowerCase();
  let focusNode = null;
  if (needle) {
    focusNode =
      graphState.nodes.find(node => String(node.id || "").toLowerCase() === needle)
      || graphState.allNodes.find(node => String(node.id || "").toLowerCase() === needle)
      || graphState.nodes.find(node => String(node.id || "").toLowerCase().startsWith(needle))
      || graphState.allNodes.find(node => String(node.id || "").toLowerCase().startsWith(needle))
      || graphState.nodes.find(node => String(node.label || "").toLowerCase().includes(needle))
      || null;
  }
  if (focusNode) {
    graphState.selectedId = focusNode.id;
    applyGraphVisibility();
    seedGraphParticles();
    renderGraphSelection(focusNode);
  } else {
    renderGraphSelection(graphState.nodes.find(node => node.id === graphState.selectedId) || graphState.nodes[0] || null);
  }
  startGraphAnimation();
  graphState.userZoomed = false;
  requestAnimationFrame(() => {
    fitGraphToView();
    requestAnimationFrame(() => fitGraphToView());
  });
}

function seedGraphParticles() {
  const canvas = document.querySelector("#graph-canvas");
  const width = canvas?.width || 800;
  const height = canvas?.height || 600;
  const existing = new Map(graphState.particles.map(item => [item.id, item]));
  const byType = new Map();
  for (const node of graphState.nodes) {
    const key = node.group || node.type || "Other";
    if (!byType.has(key)) byType.set(key, []);
    byType.get(key).push(node);
  }
  const typeKeys = [...byType.keys()];
  const cx = width / 2;
  const cy = height / 2;
  const groupRadius = Math.min(width, height) * 0.28;
  const next = [];
  typeKeys.forEach((type, typeIndex) => {
    const group = byType.get(type) || [];
    const baseAngle = (typeIndex / Math.max(typeKeys.length, 1)) * Math.PI * 2;
    const gx = cx + Math.cos(baseAngle) * groupRadius;
    const gy = cy + Math.sin(baseAngle) * groupRadius;
    group.forEach((node, index) => {
      const old = existing.get(node.id);
      if (old && Number.isFinite(old.x) && Number.isFinite(old.y)) {
        next.push({ ...old, node, vx: 0, vy: 0 });
        return;
      }
      const angle = (index / Math.max(group.length, 1)) * Math.PI * 2;
      const radius = 28 + (index % 9) * 10;
      next.push({
        id: node.id,
        node,
        x: gx + Math.cos(angle) * radius,
        y: gy + Math.sin(angle) * radius,
        z: ((index % 7) - 3) / 3,
        vx: 0,
        vy: 0,
      });
    });
  });
  graphState.particles = next;
}

function bindGraphOnce() {
  if (graphState.initialized) return;
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas) return;
  graphState.initialized = true;
  const sourceFilter = document.querySelector("#graph-source-filter");
  const scopeFilter = document.querySelector("#graph-scope-filter");
  const taskFilter = document.querySelector("#graph-task-filter");
  const providerFilter = document.querySelector("#graph-provider-filter");
  const pinnedFilter = document.querySelector("#graph-pinned-filter");
  const groupFilter = document.querySelector("#graph-group-filter");
  const searchInput = document.querySelector("#graph-search");
  const densityInput = document.querySelector("#graph-density-level");
  const fitButton = document.querySelector("#graph-fit");
  const expandButton = document.querySelector("#graph-expand");
  const rebuildButton = document.querySelector("#graph-rebuild-links");
  const inspectHint = document.querySelector("#graph-inspect-hint");
  if (inspectHint) inspectHint.textContent = t("graph.inspectHint");
  on(sourceFilter, "change", () => loadGraph().catch(showError));
  on(scopeFilter, "change", () => loadGraph().catch(showError));
  on(taskFilter, "change", () => loadGraph().catch(showError));
  on(providerFilter, "change", () => loadGraph().catch(showError));
  on(pinnedFilter, "change", () => loadGraph().catch(showError));
  on(groupFilter, "change", () => {
    graphState.groupFilter = groupFilter.value || "";
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    applyGraphVisibility();
    seedGraphParticles();
    fitGraphToView();
    wakeGraphAnimation();
  });
  const colorModeSelect = document.querySelector("#graph-color-mode");
  on(colorModeSelect, "change", () => {
    // Color depends only on data already in the payload — recolor, don't refetch.
    graphState.colorMode = colorModeSelect.value || "community";
    renderGraphLegend();
    wakeGraphAnimation();
  });
  on(searchInput, "input", () => {
    graphState.searchQuery = searchInput.value || "";
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    // Local filter for already-loaded nodes (label/text), then debounced backend
    // fetch so ID search can find nodes outside the density budget.
    applyGraphVisibility();
    seedGraphParticles();
    fitGraphToView();
    wakeGraphAnimation();
    clearTimeout(graphState.searchTimer);
    graphState.searchTimer = window.setTimeout(() => {
      loadGraph().catch(showError);
    }, 280);
  });
  on(searchInput, "keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    clearTimeout(graphState.searchTimer);
    loadGraph().catch(showError);
  });
  if (densityInput) {
    const densityValue = document.querySelector("#graph-density-value");
    const syncDensityBadge = () => { if (densityValue) densityValue.textContent = String(densityInput.value || "2"); };
    syncDensityBadge();
    on(densityInput, "input", syncDensityBadge);
    on(densityInput, "change", () => { syncDensityBadge(); loadGraph().catch(showError); });
  }
  on(fitButton, "click", () => {
    graphState.userZoomed = false;
    fitGraphToView();
  });
  on(expandButton, "click", () => toggleGraphExpand(true));
  on("#graph-fit-expanded", "click", () => {
    graphState.userZoomed = false;
    fitGraphToView();
  });
  on("#graph-close-expand", "click", () => toggleGraphExpand(false));
  document.querySelectorAll("[data-close-graph-node-modal]").forEach(el => {
    el.addEventListener("click", () => closeGraphNodeModal());
  });
  on(rebuildButton, "click", async () => {
    const summary = document.querySelector("#graph-link-summary");
    if (summary) {
      summary.hidden = false;
      summary.textContent = "linking...";
    }
    const payload = await api("/api/graph/rebuild-links", { method: "POST", body: JSON.stringify({ project_id: state.projectId }) });
    if (summary) {
      summary.hidden = false;
      summary.textContent = `${payload.created} new link(s), ${payload.linked_nodes} node(s) touched`;
    }
    await loadGraph();
  });
  on("#graph-suggest-links", "click", () => suggestGraphLinks().catch(showError));
  on("#graph-suggest-apply", "click", () => applySuggestedLinks().catch(showError));
  on("#graph-suggest-close", "click", () => {
    const panel = document.querySelector("#graph-suggest-panel");
    if (panel) panel.hidden = true;
  });
  canvas.addEventListener("pointerdown", event => {
    const hit = hitGraphNode(event);
    if (hit) {
      graphState.draggingId = hit.id;
      graphState.dragMoved = false;
      selectGraphNode(hit.node, { openModal: false });
      canvas.setPointerCapture(event.pointerId);
      wakeGraphAnimation();
    }
  });
  canvas.addEventListener("pointermove", event => {
    const hit = hitGraphNode(event);
    const nextHoverId = hit ? hit.id : "";
    if (nextHoverId !== graphState.hoverId) {
      graphState.hoverId = nextHoverId;
      wakeGraphAnimation();
    }
    if (graphState.draggingId) {
      const point = graphPointer(event);
      const particle = graphState.particles.find(item => item.id === graphState.draggingId);
      if (particle) {
        const dx = point.x - particle.x;
        const dy = point.y - particle.y;
        if (Math.hypot(dx, dy) > 2) graphState.dragMoved = true;
        particle.x = point.x;
        particle.y = point.y;
        particle.vx = 0;
        particle.vy = 0;
      }
      wakeGraphAnimation();
    }
  });
  canvas.addEventListener("pointerleave", () => {
    graphState.hoverId = "";
    wakeGraphAnimation();
  });
  canvas.addEventListener("pointerup", event => {
    graphState.draggingId = "";
    try { canvas.releasePointerCapture(event.pointerId); } catch (_) {}
  });
  canvas.addEventListener("dblclick", event => {
    event.preventDefault();
    if (graphState.dragMoved) return;
    const hit = hitGraphNode(event);
    if (hit) openGraphNodeModal(hit.node);
  });
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    const canvasEl = document.querySelector("#graph-canvas");
    if (!canvasEl) return;
    const rect = canvasEl.getBoundingClientRect();
    const ratioX = canvasEl.width / Math.max(rect.width, 1);
    const ratioY = canvasEl.height / Math.max(rect.height, 1);
    const screenX = (event.clientX - rect.left) * ratioX;
    const screenY = (event.clientY - rect.top) * ratioY;
    const worldX = (screenX - graphState.offsetX) / Math.max(graphState.scale, 0.001);
    const worldY = (screenY - graphState.offsetY) / Math.max(graphState.scale, 0.001);
    const direction = event.deltaY > 0 ? -0.08 : 0.08;
    const next = Math.max(0.2, Math.min(2.8, graphState.scale * (1 + direction)));
    graphState.scale = next;
    graphState.offsetX = screenX - worldX * next;
    graphState.offsetY = screenY - worldY * next;
    graphState.userZoomed = true;
    wakeGraphAnimation();
  }, { passive: false });
  window.addEventListener("resize", () => {
    resizeGraphCanvas();
    if (!graphState.userZoomed) fitGraphToView();
    wakeGraphAnimation();
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (graphState.modalOpen) {
      closeGraphNodeModal();
      event.preventDefault();
      return;
    }
    if (graphState.expanded) toggleGraphExpand(false);
  });
  observeGraphStage();
}

function toggleGraphExpand(open) {
  const overlay = document.querySelector("#graph-expand-overlay");
  const canvas = document.querySelector("#graph-canvas");
  const normalStage = document.querySelector(".memory-graph-section .graph-stage");
  const expandStage = document.querySelector(".graph-expand-stage");
  if (!overlay || !canvas || !normalStage || !expandStage) return;
  graphState.expanded = open;
  if (open) {
    expandStage.appendChild(canvas);
    overlay.removeAttribute("hidden");
    document.body.style.overflow = "hidden";
  } else {
    normalStage.appendChild(canvas);
    overlay.setAttribute("hidden", "");
    document.body.style.overflow = "";
  }
  resizeGraphCanvas();
  graphState.userZoomed = false;
  fitGraphToView();
}

function resizeGraphCanvas() {
  const canvas = document.querySelector("#graph-canvas");
  const stage = canvas?.closest(".graph-stage, .graph-expand-stage");
  if (!canvas || !stage) return;
  const rect = stage.getBoundingClientRect();
  if (rect.width < 8 || rect.height < 8) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, Math.floor(rect.width * ratio));
  const height = Math.max(240, Math.floor(rect.height * ratio));
  if (canvas.width === width && canvas.height === height) return;
  canvas.width = width;
  canvas.height = height;
  if (graphState.particles.length && !graphState.userZoomed) {
    // Keep world layout; only reseed when empty.
  } else if (!graphState.particles.length) {
    seedGraphParticles();
  }
}

function observeGraphStage() {
  if (graphState.stageObserver) return;
  graphState.stageObserver = new ResizeObserver(() => {
    resizeGraphCanvas();
    if (!graphState.userZoomed) fitGraphToView();
  });
  const normal = document.querySelector(".memory-graph-section .graph-stage");
  const expanded = document.querySelector(".graph-expand-stage");
  if (normal) graphState.stageObserver.observe(normal);
  if (expanded) graphState.stageObserver.observe(expanded);
}

function sanitizeGraphParticles() {
  let broken = false;
  for (const particle of graphState.particles) {
    if (![particle.x, particle.y, particle.vx, particle.vy, particle.z].every(Number.isFinite)) {
      broken = true;
      break;
    }
  }
  if (broken) {
    seedGraphParticles();
    return true;
  }
  return false;
}

function fitGraphToView() {
  resizeGraphCanvas();
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas || !graphState.particles.length) {
    graphState.scale = 1;
    graphState.offsetX = 0;
    graphState.offsetY = 0;
    if (!graphState.particles.length) seedGraphParticles();
    return;
  }
  sanitizeGraphParticles();
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const particle of graphState.particles) {
    const pad = graphNodeRadius(particle) + 14;
    minX = Math.min(minX, particle.x - pad);
    maxX = Math.max(maxX, particle.x + pad);
    minY = Math.min(minY, particle.y - pad);
    maxY = Math.max(maxY, particle.y + pad + 12);
  }
  if (![minX, minY, maxX, maxY].every(Number.isFinite)) {
    seedGraphParticles();
    graphState.scale = 1;
    graphState.offsetX = 0;
    graphState.offsetY = 0;
    return;
  }
  const graphW = Math.max(1, maxX - minX);
  const graphH = Math.max(1, maxY - minY);
  const dpr = window.devicePixelRatio || 1;
  // Keep a slim margin so the graph fills most of the stage but still fits.
  const padding = 12 * dpr;
  const availW = Math.max(1, canvas.width - padding * 2);
  const availH = Math.max(1, canvas.height - padding * 2);
  const fitScale = Math.min(availW / graphW, availH / graphH, 2.6);
  const scale = Math.max(0.12, Number.isFinite(fitScale) ? fitScale : 1);
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  graphState.scale = scale;
  graphState.offsetX = canvas.width / 2 - centerX * scale;
  graphState.offsetY = canvas.height / 2 - centerY * scale;
  if (![graphState.scale, graphState.offsetX, graphState.offsetY].every(Number.isFinite)) {
    graphState.scale = 1;
    graphState.offsetX = 0;
    graphState.offsetY = 0;
  }
}

function graphPointer(event) {
  const canvas = document.querySelector("#graph-canvas");
  const rect = canvas.getBoundingClientRect();
  const ratioX = canvas.width / Math.max(rect.width, 1);
  const ratioY = canvas.height / Math.max(rect.height, 1);
  const screenX = (event.clientX - rect.left) * ratioX;
  const screenY = (event.clientY - rect.top) * ratioY;
  return {
    x: (screenX - graphState.offsetX) / Math.max(graphState.scale, 0.001),
    y: (screenY - graphState.offsetY) / Math.max(graphState.scale, 0.001),
  };
}

function hitGraphNode(event) {
  const point = graphPointer(event);
  for (const particle of [...graphState.particles].reverse()) {
    const radius = graphNodeRadius(particle);
    const dx = point.x - particle.x;
    const dy = point.y - particle.y;
    if (Math.sqrt(dx * dx + dy * dy) <= radius + 6) return particle;
  }
  return null;
}

function startGraphAnimation() {
  cancelAnimationFrame(graphState.animationId);
  graphState.animationId = 0;
  resizeGraphCanvas();
  const tick = () => {
    graphState.animationId = 0;
    // MF0-style cooldown: simulate briefly, then freeze so the view stays readable.
    if (graphState.physicsActive || graphState.draggingId) {
      stepGraphPhysics();
      if (!graphState.draggingId) {
        graphState.physicsTicks += 1;
        if (graphState.physicsTicks >= graphState.physicsMax) {
          graphState.physicsActive = false;
          if (!graphState.userZoomed) fitGraphToView();
        }
      }
    }
    drawGraph();
    // Keep ticking only while there is visible work: active physics, a drag,
    // or a hovered node. Otherwise the canvas is static — no idle CPU burn.
    if (graphState.physicsActive || graphState.draggingId || graphState.hoverId) {
      graphState.animationId = requestAnimationFrame(tick);
    }
  };
  tick();
}

function wakeGraphAnimation() {
  if (!graphState.animationId) startGraphAnimation();
}

function stepGraphPhysics() {
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas) return;
  const particles = graphState.particles;
  if (!particles.length) return;
  sanitizeGraphParticles();
  const centerX = canvas.width / 2;
  const centerY = canvas.height / 2;
  const byId = new Map(particles.map(item => [item.id, item]));
  const n = particles.length;
  // MF0 adaptive charge: consistent density across graph sizes.
  const charge = -140 * Math.sqrt(Math.max(1, n) / 100);
  const linkDistance = n > 80 ? 90 : 110;
  if (n <= 140) {
    for (let i = 0; i < n; i++) {
      const a = particles[i];
      for (let j = i + 1; j < n; j++) {
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distance = Math.max(24, Math.sqrt(dx * dx + dy * dy));
        const force = Math.abs(charge) / (distance * distance);
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
  } else {
    const windowSize = 16;
    for (let i = 0; i < n; i++) {
      const a = particles[i];
      const limit = Math.min(n, i + 1 + windowSize);
      for (let j = i + 1; j < limit; j++) {
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distance = Math.max(24, Math.sqrt(dx * dx + dy * dy));
        const force = Math.abs(charge) * 0.55 / (distance * distance);
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
  }
  for (const edge of graphState.edges) {
    const a = byId.get(edge.source);
    const b = byId.get(edge.target);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy));
    if (!Number.isFinite(distance)) continue;
    const force = (distance - linkDistance) * 0.01;
    const fx = (dx / distance) * force;
    const fy = (dy / distance) * force;
    a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
  }
  // MF0 uses velocity decay ~0.22; here we apply per tick after integration.
  const damp = 0.78;
  for (const particle of particles) {
    if (particle.id !== graphState.draggingId) {
      particle.vx += (centerX - particle.x) * 0.00045;
      particle.vy += (centerY - particle.y) * 0.00045;
      particle.x += particle.vx;
      particle.y += particle.vy;
      particle.vx *= damp;
      particle.vy *= damp;
    }
    if (!Number.isFinite(particle.x) || !Number.isFinite(particle.y)) {
      particle.x = centerX;
      particle.y = centerY;
      particle.vx = 0;
      particle.vy = 0;
    } else {
      const span = Math.max(canvas.width, canvas.height) * 4;
      particle.x = Math.max(centerX - span, Math.min(centerX + span, particle.x));
      particle.y = Math.max(centerY - span, Math.min(centerY + span, particle.y));
      particle.vx = Math.max(-28, Math.min(28, particle.vx));
      particle.vy = Math.max(-28, Math.min(28, particle.vy));
    }
    particle.z = Math.max(-1, Math.min(1, Number.isFinite(particle.z) ? particle.z : 0));
  }
}

function isDarkTheme() { return document.documentElement.dataset.theme === "dark"; }
function graphColors() {
  if (isDarkTheme()) {
    return {
      bg0: "#172033", bg1: "#090d17", grid: "rgba(148, 163, 184, 0.08)",
      edge: "rgba(148, 163, 184, 0.24)", edgeActive: "rgba(125, 211, 252, 0.86)",
      nodeStroke: "rgba(255,255,255,0.72)", nodeStrokeActive: "#e0f2fe",
      nodeShadow: "rgba(15, 23, 42, 0.55)", nodeShadowActive: "rgba(125, 211, 252, 0.78)",
      label: "#f8fafc", labelShadow: "rgba(0,0,0,0.55)",
    };
  }
  return {
    bg0: "#ffffff", bg1: "#e7ecf6", grid: "rgba(71, 85, 105, 0.09)",
    edge: "rgba(71, 85, 105, 0.30)", edgeActive: "rgba(99, 102, 241, 0.85)",
    nodeStroke: "rgba(255,255,255,0.95)", nodeStrokeActive: "#6366f1",
    nodeShadow: "rgba(15, 23, 42, 0.20)", nodeShadowActive: "rgba(99, 102, 241, 0.5)",
    label: "#1e293b", labelShadow: "rgba(255,255,255,0.85)",
  };
}

function drawGraph() {
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const colors = graphColors();
  const scale = Math.max(graphState.scale, 0.001);
  const inv = 1 / scale;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const gradient = ctx.createRadialGradient(canvas.width * 0.52, canvas.height * 0.45, 20, canvas.width * 0.5, canvas.height * 0.5, canvas.width * 0.75);
  gradient.addColorStop(0, colors.bg0);
  gradient.addColorStop(1, colors.bg1);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  drawGraphGrid(ctx, canvas, colors);
  if (![scale, graphState.offsetX, graphState.offsetY].every(Number.isFinite)) return;
  ctx.setTransform(scale, 0, 0, scale, graphState.offsetX, graphState.offsetY);
  const byId = new Map(graphState.particles.map(item => [item.id, item]));
  for (const edge of graphState.edges) {
    const a = byId.get(edge.source);
    const b = byId.get(edge.target);
    if (!a || !b) continue;
    const active = graphState.selectedId && (edge.source === graphState.selectedId || edge.target === graphState.selectedId);
    // Machine-inferred links (similarity/auto/LLM) render dashed and faint so
    // they read as "guessed", explicit relationships as solid.
    const inferred = edge.provenance === "INFERRED";
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.strokeStyle = active ? colors.edgeActive : (graphCodeEdgeColors[edge.type] || colors.edge);
    // Keep strokes screen-constant so zoom/fit never produces a thick blur smear.
    ctx.lineWidth = (active ? 2.2 : inferred ? 0.7 : 1.1) * inv;
    ctx.globalAlpha = active ? 1 : inferred ? 0.5 : 0.85;
    ctx.setLineDash(inferred && !active ? [4 * inv, 4 * inv] : []);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);
  for (const particle of [...graphState.particles].sort((a, b) => a.z - b.z)) {
    drawGraphNode(ctx, particle, colors, inv);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}

function drawGraphGrid(ctx, canvas, colors) {
  ctx.save();
  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  const gap = 64;
  for (let x = canvas.width % gap; x < canvas.width; x += gap) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); ctx.stroke();
  }
  for (let y = canvas.height % gap; y < canvas.height; y += gap) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke();
  }
  ctx.restore();
}

function graphNodeRadius(particle) {
  const node = particle.node;
  // Symbols are numerous and fine-grained: draw them smaller than knowledge
  // nodes (classes a touch larger than functions) so files/decisions still lead.
  const symbolBase = (node.metadata && node.metadata.kind === "Class") ? 8 : 6;
  const base = node.type === "Project" ? 18 : node.type === "Symbol" ? symbolBase : node.pinned ? 14 : 10;
  // God-node sizing: the more connected a node, the bigger it reads.
  const degree = Math.max(0, Number(node.degree) || 0);
  const degreeBoost = Math.min(9, Math.sqrt(degree) * 2.2);
  return base + degreeBoost + particle.z * 2;
}

function drawGraphNode(ctx, particle, colors, inv = 1) {
  colors = colors || graphColors();
  const node = particle.node;
  const radius = graphNodeRadius(particle);
  const color = graphNodeColor(node);
  const active = particle.id === graphState.selectedId;
  const hover = particle.id === graphState.hoverId;
  ctx.save();
  if (active || hover) {
    ctx.shadowColor = active ? colors.nodeShadowActive : colors.nodeShadow;
    ctx.shadowBlur = (active ? 14 : 8) * inv;
  }
  // Superseded facts (retired by a newer one) read as dimmed history.
  const superseded = Boolean(node.superseded || node?.metadata?.invalid_at);
  ctx.beginPath();
  ctx.arc(particle.x, particle.y, radius + (hover ? 2 : 0), 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.globalAlpha = superseded ? 0.3 : 0.9;
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.lineWidth = (active ? 3 : 1.5) * inv;
  ctx.strokeStyle = active ? colors.nodeStrokeActive : colors.nodeStroke;
  ctx.stroke();
  const denseGraph = graphState.nodes.length > 55;
  const showLabel = !denseGraph || active || hover || node.type === "Project" || node.pinned;
  if (showLabel) {
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.fillStyle = colors.label;
    ctx.font = `${Math.max(10, 11 * Math.min(inv, 1.4))}px Segoe UI, Arial`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const label = node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label;
    ctx.fillText(label, particle.x, particle.y + radius + 5);
  }
  ctx.restore();
}

function renderGraphLegend() {
  const legend = document.querySelector("#graph-legend");
  const groupFilter = document.querySelector("#graph-group-filter");
  if (!legend) return;
  const groups = [...new Set(graphState.allNodes.map(node => node.group || "Other"))].sort();
  // Always offer Projects when a project root exists in the unfiltered payload,
  // so the Groups → Projects control does not disappear under density/source filters.
  if (!groups.includes("Projects") && graphState.allNodes.some(node => node.type === "Project")) {
    groups.push("Projects");
    groups.sort();
  }
  const colorMode = graphState.colorMode || "community";
  if (colorMode === "community" && Array.isArray(graphState.communities) && graphState.communities.length) {
    const themes = graphState.communities.filter(item => item.size > 1).slice(0, 8);
    legend.innerHTML = themes.length
      ? themes.map(item => `<span class="graph-legend-item"><span class="graph-legend-dot" style="background:${escapeHtml(graphCommunityColor(item.id))}"></span>${escapeHtml(item.label || ("Cluster " + item.id))} <small>(${item.size})</small></span>`).join("")
      : '<span class="graph-legend-item">No themes yet — link more memory</span>';
  } else {
    legend.innerHTML = groups.map(group => `<span class="graph-legend-item"><span class="graph-legend-dot" style="background:${escapeHtml(graphGroupColor(group))}"></span>${escapeHtml(group)}</span>`).join("");
  }
  if (groupFilter) {
    const current = graphState.groupFilter || groupFilter.value || "";
    const known = new Set(groups);
    if (current && !known.has(current)) known.add(current);
    const options = ['<option value="">All groups</option>']
      .concat([...known].sort().map(group => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`));
    groupFilter.innerHTML = options.join("");
    if (current && [...groupFilter.options].some(opt => opt.value === current)) {
      groupFilter.value = current;
      graphState.groupFilter = current;
    } else {
      groupFilter.value = "";
      graphState.groupFilter = "";
    }
  }
}

async function syncGraphFilters() {
  const taskFilter = document.querySelector("#graph-task-filter");
  const providerFilter = document.querySelector("#graph-provider-filter");
  if (!taskFilter || !providerFilter || taskFilter.dataset.ready) return;
  const [tasksPayload, providersPayload] = await Promise.all([api(`/api/tasks?project_id=${projectParam()}`), api("/api/providers")]);
  taskFilter.innerHTML = '<option value="">All tasks</option>' + tasksPayload.tasks.map(task => `<option value="${escapeHtml(task.id)}">${escapeHtml(task.title)}</option>`).join("");
  providerFilter.innerHTML = '<option value="">All providers</option>' + providersPayload.providers.map(provider => `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.label)}</option>`).join("");
  taskFilter.dataset.ready = "1";
}

function graphNodeOptions(excludeId = "") {
  const source = graphState.allNodes.length ? graphState.allNodes : graphState.nodes;
  return source
    .filter(node => node.id !== excludeId && !(node.metadata && node.metadata.synthetic))
    .map(node => `<option value="${escapeHtml(node.id)}">${escapeHtml(node.label)}</option>`)
    .join("");
}

function graphEdgeLabel(type) {
  return ({
    HAS_MEMORY: "Has memory",
    DOCUMENTED_IN: "Documents",
    IMPLEMENTS: "Implements",
    RELATED_TO: "Related",
    SUPPORTS: "Supports",
    DEPENDS_ON: "Depends on",
    DECIDED_IN: "Decided in",
    TASK_LINK: "Task link",
    PROVIDER_RELATED: "Provider related",
    DEFINES: "Defines",
    CONTAINS: "Contains",
    IMPORTS: "Imports",
    CALLS: "Calls",
  })[type] || String(type || "Related").replaceAll("_", " ").toLowerCase().replace(/^./, char => char.toUpperCase());
}

function graphEdgeDirection(edge, selectedNode, otherNode) {
  if (edge.source === selectedNode.id) return `${escapeHtml(selectedNode.label)} -> ${escapeHtml(otherNode.label)}`;
  return `${escapeHtml(otherNode.label)} -> ${escapeHtml(selectedNode.label)}`;
}

function looksLikeProjectPath(value) {
  const text = String(value || "").trim();
  if (!text) return false;
  if (/^https?:\/\//i.test(text)) return false;
  if (text.startsWith("evidence/")) return false;
  return /[./\\]/.test(text) || /\.[a-z0-9]{1,8}$/i.test(text);
}

function graphNodeRelatedFiles(node) {
  const seen = new Set();
  const files = [];
  const push = (raw, kind) => {
    const value = String(raw || "").trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    files.push({ path: value, kind, openable: looksLikeProjectPath(value) && !value.startsWith("evidence/") });
  };
  const meta = node?.metadata || {};
  for (const key of ["path", "relative_path", "source_ref", "file", "source_path"]) {
    if (meta[key]) push(meta[key], key === "source_ref" ? "source" : "path");
  }
  for (const item of node?.evidence || []) push(item, "evidence");
  if (looksLikeProjectPath(node?.label)) push(node.label, "artifact");
  return files;
}

function isGraphNodeModalOpen() {
  const modal = document.querySelector("#graph-node-modal");
  return Boolean(modal && !modal.hasAttribute("hidden"));
}

function openGraphNodeModal(node) {
  if (!node) return;
  const prevSelected = graphState.selectedId;
  graphState.selectedId = node.id;
  graphState.physicsActive = true;
  graphState.physicsTicks = Math.min(graphState.physicsTicks, Math.floor(graphState.physicsMax * 0.6));
  if (prevSelected !== node.id || !graphState.particles.some(item => item.id === node.id)) {
    applyGraphVisibility();
    seedGraphParticles();
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    graphState.userZoomed = false;
  }
  wakeGraphAnimation();
  renderGraphDetail(node);
  const modal = document.querySelector("#graph-node-modal");
  if (!modal) return;
  modal.removeAttribute("hidden");
  graphState.modalOpen = true;
  modal.querySelector(".modal-close")?.focus();
}

function closeGraphNodeModal() {
  const modal = document.querySelector("#graph-node-modal");
  if (modal) modal.setAttribute("hidden", "");
  graphState.modalOpen = false;
}

function selectGraphNode(node, options = {}) {
  if (!node) {
    graphState.selectedId = "";
    return;
  }
  const prevSelected = graphState.selectedId;
  graphState.selectedId = node.id;
  graphState.physicsActive = true;
  graphState.physicsTicks = Math.min(graphState.physicsTicks, Math.floor(graphState.physicsMax * 0.6));
  if (prevSelected !== node.id || !graphState.particles.some(item => item.id === node.id)) {
    applyGraphVisibility();
    seedGraphParticles();
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    graphState.userZoomed = false;
  }
  wakeGraphAnimation();
  if (options.openModal) {
    openGraphNodeModal(node);
    return;
  }
  if (isGraphNodeModalOpen()) renderGraphDetail(node);
}

function renderGraphSelection(node) {
  if (!node) {
    graphState.selectedId = "";
    return;
  }
  selectGraphNode(node, { openModal: false });
}

// Labelled sub-sections we split a memory body into so a "User: … / Context: …"
// chat capture reads as distinct blocks instead of one wall of text.
const MEMORY_BLOCK_LABELS = new Map([
  ["user", "User"], ["context", "Context"], ["assistant", "Assistant"],
  ["question", "Question"], ["answer", "Answer"], ["source", "Source"],
  ["note", "Note"], ["overview", "Overview"], ["summary", "Summary"],
]);

// Turn Azure Boards' REST work-item URL into a browser-openable board link.
function azureBoardsUrl(meta) {
  const raw = String((meta && meta.work_item_url) || "").trim();
  if (!raw) return "";
  if (raw.includes("/_apis/wit/workItems/")) return raw.replace("/_apis/wit/workItems/", "/_workitems/edit/");
  return raw;
}

function memorySourceLinks(node) {
  const meta = (node && node.metadata) || {};
  const links = [];
  const seen = new Set();
  const add = (href, label, kind) => {
    const url = String(href || "").trim();
    if (!/^https?:\/\//i.test(url) || seen.has(url)) return;
    seen.add(url);
    links.push({ href: url, label, kind });
  };
  add(azureBoardsUrl(meta), `Azure Boards${meta.work_item_id ? " #" + meta.work_item_id : ""}`, "azure");
  add(meta.granola_url, "Open in Granola", "granola");
  add(meta.wiki_url || meta.web_url, "Open page", "source");
  add(meta.source_ref, "Open source", "source");
  add(meta.url, "Open link", "source");
  return links;
}

function renderMemoryInline(str, node) {
  let html = escapeHtml(str);
  html = html.replace(/`([^`]+)`/g, (_m, code) => `<code class="mem-code">${code}</code>`);
  html = html.replace(/\*\*([^*]+)\*\*/g, (_m, bold) => `<strong>${bold}</strong>`);
  // node_<hex> references become chips that jump to that memory node.
  html = html.replace(/\bnode_[0-9a-f]{6,}\b/g, id => `<a href="#" class="mem-ref" data-node-ref="${id}">${id}</a>`);
  // Evidence/source file paths: surfaced as provenance chips (not project-openable).
  html = html.replace(/\b(evidence\/[^\s<>`)]+\.[A-Za-z0-9]{1,8})/g, path => `<span class="mem-ref mem-ref-file">${path}</span>`);
  html = html.replace(/(^|[\s(])((?:https?:\/\/)[^\s<>()]+)(?=$|[\s).,;])/g, (_m, pre, url) => `${pre}<a href="${url}" class="mem-link" target="_blank" rel="noopener noreferrer">${url}</a>`);
  // A backticked ref (e.g. `node_…`) becomes a chip already — drop the redundant code wrapper.
  html = html.replace(/<code class="mem-code">(<a href="#" class="mem-ref"[^>]*>[^<]*<\/a>)<\/code>/g, "$1");
  html = html.replace(/<code class="mem-code">(<span class="mem-ref mem-ref-file">[^<]*<\/span>)<\/code>/g, "$1");
  return html;
}

function renderMemoryMarkdown(lines, node) {
  const out = [];
  let para = [];
  let list = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(line => renderMemoryInline(line, node)).join("<br>")}</p>`); para = []; } };
  const flushList = () => { if (list.length) { out.push(`<ul class="mem-list">${list.map(item => `<li>${renderMemoryInline(item, node)}</li>`).join("")}</ul>`); list = []; } };
  for (const rawLine of lines) {
    const line = rawLine.replace(/\s+$/, "");
    if (!line.trim()) { flushList(); flushPara(); continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) { flushList(); flushPara(); const tag = heading[1].length <= 2 ? "h5" : "h6"; out.push(`<${tag} class="mem-h">${renderMemoryInline(heading[2], node)}</${tag}>`); continue; }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) { flushPara(); list.push(bullet[1]); continue; }
    flushList(); para.push(line.trim());
  }
  flushList(); flushPara();
  return out.join("");
}

function renderMemoryBodyHtml(node) {
  const raw = String((node && node.text) || "").replace(/\r\n/g, "\n").trim();
  const links = memorySourceLinks(node);
  const linksHtml = links.length
    ? `<div class="mem-source-links">${links.map(link => `<a class="mem-source-link mem-source-${link.kind}" href="${escapeHtml(link.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.label)} <span aria-hidden="true">↗</span></a>`).join("")}</div>`
    : "";
  if (!raw) return linksHtml + '<p class="mem-empty">No description for this memory node.</p>';
  const blocks = [];
  let current = { label: "", lines: [] };
  const flush = () => { if (current.label || current.lines.length) blocks.push(current); current = { label: "", lines: [] }; };
  for (const line of raw.split("\n")) {
    const match = line.match(/^([A-Za-z][A-Za-z ]{0,18}):\s*$/);
    const key = match ? match[1].trim().toLowerCase() : "";
    if (match && MEMORY_BLOCK_LABELS.has(key)) { flush(); current.label = MEMORY_BLOCK_LABELS.get(key); continue; }
    current.lines.push(line);
  }
  flush();
  const body = blocks.map(block => {
    const inner = renderMemoryMarkdown(block.lines, node);
    if (!inner.trim() && !block.label) return "";
    const labelHtml = block.label ? `<div class="mem-label">${escapeHtml(block.label)}</div>` : "";
    const cls = block.label ? `mem-block mem-block-${block.label.toLowerCase()}` : "mem-block";
    return `<div class="${cls}">${labelHtml}<div class="mem-content">${inner}</div></div>`;
  }).join("");
  return linksHtml + body;
}

function bindMemoryBodyRefs(container, node) {
  if (!container) return;
  container.querySelectorAll("[data-node-ref]").forEach(el => {
    el.addEventListener("click", event => {
      event.preventDefault();
      const id = el.getAttribute("data-node-ref");
      const nodeSource = graphState.allNodes.length ? graphState.allNodes : graphState.nodes;
      const target = nodeSource.find(item => item.id === id);
      if (target) { openGraphNodeModal(target); return; }
      closeGraphNodeModal();
      switchView("memory");
      setElementValue("#search-query", id);
      runSearch(id).catch(showError);
    });
  });
}

function renderGraphDetail(node) {
  const title = document.querySelector("#graph-detail-title");
  const text = document.querySelector("#graph-detail-text");
  const meta = document.querySelector("#graph-detail-meta");
  const actions = document.querySelector("#graph-actions");
  const neighbors = document.querySelector("#graph-neighbors");
  const filesEl = document.querySelector("#graph-related-files");
  if (!title || !text || !meta || !actions || !neighbors) return;
  if (!node) {
    title.textContent = "No nodes";
    text.textContent = "Scan or add memory to populate the graph.";
    meta.innerHTML = "";
    actions.innerHTML = "";
    neighbors.innerHTML = "";
    if (filesEl) filesEl.innerHTML = "";
    return;
  }
  title.textContent = node.label;
  text.innerHTML = renderMemoryBodyHtml(node);
  bindMemoryBodyRefs(text, node);
  const synthetic = Boolean(node.metadata && node.metadata.synthetic);
  const pinned = Boolean(node.metadata && (node.metadata.favorite || node.metadata.pinned));
  const wi = node.metadata || {};
  const abBadges = wi.work_item_id
    ? `<span class="badge badge-azure">ADO #${escapeHtml(wi.work_item_id)}</span>${wi.work_item_state ? `<span class="badge">${escapeHtml(wi.work_item_state)}</span>` : ""}`
    : "";
  const edgeSource = graphState.allEdges.length ? graphState.allEdges : graphState.edges;
  const nodeSource = graphState.allNodes.length ? graphState.allNodes : graphState.nodes;
  const linked = edgeSource.filter(edge => edge.source === node.id || edge.target === node.id);
  let codeBadges = "";
  if (node.type === "Symbol") {
    const where = wi.line ? `${wi.path || ""}:${wi.line}` : (wi.path || "");
    const callers = linked.filter(edge => edge.type === "CALLS" && edge.target === node.id).length;
    const callees = linked.filter(edge => edge.type === "CALLS" && edge.source === node.id).length;
    codeBadges =
      `${wi.kind ? `<span class="badge badge-code">${escapeHtml(wi.kind)}</span>` : ""}` +
      `${where ? `<span class="badge badge-code">${escapeHtml(where)}</span>` : ""}` +
      `<span class="badge badge-code">${callers} caller${callers === 1 ? "" : "s"}</span>` +
      `<span class="badge badge-code">${callees} call${callees === 1 ? "" : "s"}</span>`;
  }
  meta.innerHTML = `<span class="badge">${escapeHtml(node.type)}</span><span class="badge">${escapeHtml(node.scope)}</span><span class="badge">${linked.length} links</span>${pinned ? '<span class="badge">pinned</span>' : ""}${codeBadges}${abBadges}`;
  const options = graphNodeOptions(node.id);
  actions.innerHTML = synthetic
    ? '<div class="provider-test">Synthetic filter nodes cannot be edited.</div>'
    : `<div class="provider-actions graph-node-primary-actions"><button data-graph-open="${escapeHtml(node.id)}" type="button">Open in Search</button><button data-graph-pin="${escapeHtml(node.id)}" type="button">${pinned ? "Unpin" : "Pin"}</button></div><details class="graph-node-advanced"><summary>Advanced edges</summary><label>Target<select data-graph-target><option value="">Select node</option>${options}</select></label><label>Edge<select data-graph-edge-type><option>RELATED_TO</option><option>SUPPORTS</option><option>DEPENDS_ON</option><option>IMPLEMENTS</option><option>DOCUMENTED_IN</option></select></label><div class="provider-actions"><button data-graph-edge="${escapeHtml(node.id)}" type="button">Create Edge</button><button data-graph-path="${escapeHtml(node.id)}" type="button">Explain Path</button><button data-graph-merge="${escapeHtml(node.id)}" type="button">Merge Into Target</button></div></details><div class="provider-test" data-graph-action-result></div>`;
  bindGraphActions(actions, node);
  neighbors.innerHTML = linked.length
    ? ""
    : '<div class="result"><strong>No linked memory</strong><p>Use Rebuild Links or create an edge.</p></div>';
  const byId = new Map(nodeSource.map(item => [item.id, item]));
  for (const edge of linked) {
    const other = byId.get(edge.source === node.id ? edge.target : edge.source);
    if (!other) continue;
    const item = document.createElement("article");
    item.className = "result graph-link-card";
    item.tabIndex = 0;
    const label = graphEdgeLabel(edge.type);
    const direction = graphEdgeDirection(edge, node, other);
    const confidence = edge.confidence ? Math.round(Number(edge.confidence) * 100) : 0;
    item.innerHTML = `<div class="row"><strong>${escapeHtml(other.label)}</strong><span class="badge">${escapeHtml(label)}</span></div><p>${direction}</p><span class="badge">${escapeHtml(other.type)}</span><span class="badge">${escapeHtml(other.scope)}</span>${confidence ? `<span class="badge">${confidence}% confidence</span>` : ""}`;
    const openLinked = () => openGraphNodeModal(other);
    item.addEventListener("click", openLinked);
    item.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openLinked();
      }
    });
    neighbors.appendChild(item);
  }
  if (filesEl) {
    const files = graphNodeRelatedFiles(node);
    if (!files.length) {
      filesEl.innerHTML = '<div class="result"><strong>No related files</strong><p>Evidence paths and source refs will show up here when available.</p></div>';
    } else {
      filesEl.innerHTML = files.map(file => {
        const kind = file.kind === "evidence" ? "Evidence" : file.kind === "source" ? "Source" : "File";
        if (file.openable) {
          return `<button type="button" class="graph-file-row" data-graph-file="${escapeHtml(file.path)}"><span class="badge">${kind}</span><span class="graph-file-path">${escapeHtml(file.path)}</span></button>`;
        }
        return `<div class="graph-file-row is-static"><span class="badge">${kind}</span><span class="graph-file-path">${escapeHtml(file.path)}</span></div>`;
      }).join("");
      filesEl.querySelectorAll("[data-graph-file]").forEach(button => {
        button.addEventListener("click", () => {
          const path = button.getAttribute("data-graph-file");
          if (!path) return;
          closeGraphNodeModal();
          switchView("workspace");
          if (typeof fileEditor !== "undefined" && fileEditor?.openFile) {
            fileEditor.openFile(path).catch(showError);
          }
        });
      });
    }
  }
}

function bindGraphActions(container, node) {
  const result = container.querySelector("[data-graph-action-result]");
  const targetSelect = container.querySelector("[data-graph-target]");
  const edgeType = container.querySelector("[data-graph-edge-type]");
  const setResult = (message, ok = true) => { if (result) { result.className = `provider-test ${ok ? "ok" : "error"}`; result.textContent = message; } };
  const open = container.querySelector("[data-graph-open]");
  if (open) open.addEventListener("click", () => {
    closeGraphNodeModal();
    switchView("memory");
    setElementValue("#search-query", node.label);
    runSearch(node.label).catch(showError);
  });
  const pin = container.querySelector("[data-graph-pin]");
  if (pin) pin.addEventListener("click", async () => { await api(`/api/graph/nodes/${node.id}/pin`, { method: "POST", body: "{}" }); await loadGraph(); });
  const createEdge = container.querySelector("[data-graph-edge]");
  if (createEdge) createEdge.addEventListener("click", async () => {
    if (!targetSelect.value) return setResult("Select a target node.", false);
    await api("/api/graph/edges", { method: "POST", body: JSON.stringify({ source: node.id, target: targetSelect.value, type: edgeType.value, scope: node.scope }) });
    setResult("Edge created.");
    await loadGraph();
  });
  const explain = container.querySelector("[data-graph-path]");
  if (explain) explain.addEventListener("click", async () => {
    if (!targetSelect.value) return setResult("Select a target node.", false);
    const payload = await api(`/api/graph/path?source=${encodeURIComponent(node.id)}&target=${encodeURIComponent(targetSelect.value)}`);
    setResult(payload.explanation, payload.found);
  });
  const merge = container.querySelector("[data-graph-merge]");
  if (merge) merge.addEventListener("click", async () => {
    if (!targetSelect.value) return setResult("Select a target node.", false);
    await api(`/api/graph/nodes/${node.id}/merge`, { method: "POST", body: JSON.stringify({ target_id: targetSelect.value }) });
    setResult("Node merged.");
    graphState.selectedId = targetSelect.value;
    await loadGraph();
  });
}
