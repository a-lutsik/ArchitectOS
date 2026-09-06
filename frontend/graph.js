/* Memory graph engine: state, physics, canvas, detail/actions — extracted from app.js */
import { api } from "./api-client.js";
import { queueAskFollowUp, switchView } from "./ask-ui.js";
import { escapeHtml, on, setElementValue, showSnackbar, trapFocus } from "./dom-utils.js";
import { fileEditor } from "./file-editor.js";
import {
  GRAPH_VIEW_GALAXY,
  GRAPH_VIEW_MAP,
  canvasPointFromEvent,
  createGalaxyCamera,
  dollyGalaxyCamera,
  drawGalaxy,
  fitGalaxyCamera,
  galaxyHasPositions,
  hitGalaxyNode,
  orbitGalaxyCamera,
  panGalaxyCamera,
  persistGraphView,
  prefersGraphReducedMotion,
  readSavedGraphView,
  scaleGalaxyPositions,
  seedGalaxyPositions,
  stepGalaxyPhysics,
  parseGraphColor,
} from "./graph-galaxy.js";
import {
  FORCE_ALPHA_MIN,
  graphForceParams,
  seedForcePositions,
  stepForceSimulation,
} from "./graph-force.js";
import { switchMemoryTab } from "./memory-panel.js";
import { runSearch } from "./projects.js";
import { projectParam, state, t } from "./state.js";
import { showError } from "./ui.js";

let graphSuggestions = [];
// Release functions returned by trapFocus() while a graph overlay is open.
let releaseGraphNodeModalFocus = null;
let releaseGraphExpandFocus = null;

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
  status.textContent = t("graph.suggest.analyzing");
  let payload;
  try {
    payload = await api("/api/graph/suggest-links", {
      method: "POST",
      body: JSON.stringify({ project_id: state.projectId, limit: 24, pair_limit: 10 }),
    });
  } catch (error) {
    status.textContent = t("graph.suggest.failed").replace("{message}", String(error.message || error));
    if (suggestButton) suggestButton.disabled = false;
    throw error;
  }
  if (suggestButton) suggestButton.disabled = false;
  graphSuggestions = (payload.suggestions || []).filter(item => item.related && !item.llm_error);
  const skipped = (payload.suggestions || []).length - graphSuggestions.length;
  status.textContent = `pool ${payload.pool_size} · pairs ${payload.pairs_considered} · ${graphSuggestions.length} suggested` +
    (skipped > 0 ? ` · ${skipped} rejected by AI` : "");
  if (!graphSuggestions.length) {
    list.innerHTML = `<div class="graph-suggest-empty">${escapeHtml(t("graph.suggest.empty"))}</div>`;
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
    const kind = document.createElement("span");
    kind.className = "graph-suggest-kind";
    const type = document.createElement("span");
    type.className = "graph-suggest-type";
    type.textContent = String(item.edge_type || "related");
    const conf = document.createElement("span");
    conf.className = "graph-suggest-conf";
    const confidence = Number(item.confidence);
    conf.textContent = Number.isFinite(confidence) ? confidence.toFixed(2) : "";
    kind.append(type, conf);
    const body = document.createElement("span");
    body.className = "graph-suggest-body";
    body.textContent = `${item.source_label || "?"} ↔ ${item.target_label || "?"}`;
    body.title = body.textContent;
    row.append(checkbox, kind, body);
    const reason = String(item.reason || "").trim();
    if (reason) {
      const why = document.createElement("span");
      why.className = "graph-suggest-reason";
      why.textContent = reason;
      why.title = reason;
      row.appendChild(why);
    }
    list.appendChild(row);
  });
  applyButton.disabled = false;
}

async function applySuggestedLinks() {
  const panel = document.querySelector("#graph-suggest-panel");
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
  status.textContent = t("graph.suggest.applied").replace("{count}", String(created));
  list.innerHTML = "";
  graphSuggestions = [];
  if (panel) panel.hidden = true;
  await loadGraph();
}

let graphLoadGeneration = 0;

function setGraphLoading(loading) {
  const canvas = document.querySelector("#graph-canvas");
  const stage = canvas?.closest(".graph-stage, .graph-expand-stage") || document.querySelector(".memory-graph-section .graph-stage");
  if (!stage) return;
  let el = document.querySelector("#graph-loading");
  if (!el) {
    el = document.createElement("div");
    el.id = "graph-loading";
    el.className = "graph-loading";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.innerHTML = `
      <span class="graph-loading-spinner" aria-hidden="true"></span>
      <span class="graph-loading-label">${escapeHtml(t("graph.loading"))}</span>`;
  }
  if (el.parentElement !== stage) {
    stage.appendChild(el);
  }
  const label = el.querySelector(".graph-loading-label");
  if (label) label.textContent = t("graph.loading");
  el.hidden = !loading;
  stage.classList.toggle("is-loading", Boolean(loading));
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
  modalControlsBound: false,
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
  simAlpha: 1,
  simAlphaTarget: 0,
  fittedAfterSettle: false,
  searchQuery: "",
  searchTimer: 0,
  neighborhoodId: "",
  groupFilter: "",
  densityLevel: 40,
  nodeSize: 5,
  nodeSpread: 1,
  densityAuto: true,
  sizeAuto: true,
  spreadAuto: true,
  view: readSavedGraphView(),
  galaxyCamera: createGalaxyCamera(),
  orbiting: false,
  galaxyPan: false,
  mapPanning: false,
  orbitLastX: 0,
  orbitLastY: 0,
  galaxyHitId: "",
  galaxyLaidSpread: 1,
};

function wantsGalaxyPan(event) {
  return Boolean(event.shiftKey || event.altKey || event.metaKey || event.button === 1 || event.button === 2);
}

function isGalaxyView() {
  return graphState.view === GRAPH_VIEW_GALAXY;
}

function graphStageVisible() {
  const memory = document.querySelector("#memory-view");
  if (memory && !memory.classList.contains("active")) return false;
  const listTab = document.querySelector("#memory-tab-list");
  if (listTab?.classList.contains("active")) return false;
  if (typeof document !== "undefined" && document.hidden) return false;
  return true;
}

function ensureGalaxyLayout({ force = false } = {}) {
  if (!isGalaxyView() || !graphState.particles.length) return;
  const ready = galaxyHasPositions(graphState.particles);
  if (!force && ready) return;
  seedGalaxyPositions(graphState.particles, { force: true });
  const spread = graphNodeSpread();
  if (spread !== 1) scaleGalaxyPositions(graphState.particles, spread);
  graphState.galaxyLaidSpread = spread;
}

function frameGalaxy({ resetOrientation = false, ease = false } = {}) {
  const canvas = document.querySelector("#graph-canvas");
  fitGalaxyCamera(graphState.particles, graphState.galaxyCamera, {
    width: canvas?.width || 800,
    height: canvas?.height || 600,
    resetOrientation,
    ease,
  });
}

function syncGraphViewChrome() {
  const galaxy = isGalaxyView();
  document.querySelectorAll(".graph-view-toggle").forEach(group => {
    group.setAttribute("aria-label", t("graph.view.group"));
  });
  document.querySelectorAll("[data-graph-view]").forEach(button => {
    const on = button.getAttribute("data-graph-view") === graphState.view;
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.classList.toggle("is-active", on);
  });
  document.querySelectorAll(".graph-stage").forEach(stage => {
    stage.classList.toggle("is-galaxy", galaxy);
    stage.classList.toggle("is-orbiting", Boolean(galaxy && graphState.orbiting && !graphState.galaxyPan));
    stage.classList.toggle("is-panning", Boolean(galaxy && graphState.orbiting && graphState.galaxyPan));
  });
  const hint = document.querySelector("#graph-inspect-hint");
  if (hint) {
    const key = galaxy ? "graph.inspectHintGalaxy" : "graph.inspectHint";
    hint.dataset.i18n = key;
    hint.textContent = t(key);
  }
  const expandHint = document.querySelector("#graph-expand-overlay .graph-expand-toolbar p");
  if (expandHint) {
    const key = galaxy ? "graph.expandHintGalaxy" : "graph.expandHint";
    expandHint.dataset.i18n = key;
    expandHint.textContent = t(key);
  }
}

function setGraphView(view) {
  const next = view === GRAPH_VIEW_GALAXY ? GRAPH_VIEW_GALAXY : GRAPH_VIEW_MAP;
  if (graphState.view === next) {
    syncGraphViewChrome();
    return;
  }
  graphState.view = next;
  persistGraphView(next);
  graphState.orbiting = false;
  graphState.galaxyPan = false;
  graphState.mapPanning = false;
  graphState.draggingId = "";
  graphState.userZoomed = false;
  graphState.physicsTicks = 0;
  graphState.physicsActive = true;
  graphState.simAlpha = 1;
  graphState.simAlphaTarget = 0;
  graphState.fittedAfterSettle = false;
  if (next === GRAPH_VIEW_GALAXY) {
    resizeGraphCanvas();
    ensureGalaxyLayout({ force: !galaxyHasPositions(graphState.particles) });
    frameGalaxy({ resetOrientation: true });
  } else {
    reheatGraphForce(1);
  }
  syncGraphViewChrome();
  wakeGraphAnimation();
}

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
  if (meta.source_id) return String(meta.source_id);
  if (meta.source_key && String(meta.source_key).startsWith("source_")) return String(meta.source_key);
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
function graphDensityPercent(densityLevel = graphState.densityLevel) {
  const raw = Number(densityLevel);
  const value = Number.isFinite(raw) ? raw : 40;
  return Math.max(5, Math.min(100, Math.round(value / 5) * 5));
}

function graphVisibleBudget(total, densityLevel = graphState.densityLevel) {
  const count = Math.max(0, Number(total) || 0);
  if (!count) return 0;
  const percent = graphDensityPercent(densityLevel);
  if (percent >= 100) return count;
  return Math.max(1, Math.round(count * percent / 100));
}

function graphBackendLimit() {
  return Math.max(Number(graphState.totalNodesCount) || 0, 10000);
}

function graphDensityLabel(densityLevel = graphState.densityLevel) {
  const percent = graphDensityPercent(densityLevel);
  return percent >= 100 ? "all" : `${percent}%`;
}

/** Stage area → target visible node budget (hubs + leaves). */
function graphAutoTargetBudget(width, height) {
  const area = Math.max(1, Number(width) || 800) * Math.max(1, Number(height) || 600);
  // ~65 on a 640×560 stage — small/medium graphs prefer showing everything.
  return Math.max(24, Math.min(90, Math.round(area / 5500)));
}

/** Node radius from visible count and stage area — denser graphs get smaller dots. */
function graphAutoNodeSize(visible, width, height) {
  const area = Math.max(1, Number(width) || 800) * Math.max(1, Number(height) || 600);
  const stageScale = Math.sqrt(area) / 300;
  const count = Math.max(1, Number(visible) || 1);
  // ~58 visible → size 10; fewer → up to 12; crowded → toward 5–6.
  const size = Math.round(14 - count / 14 + stageScale * 0.2);
  return Math.max(5, Math.min(12, size));
}

/** Pick readable density / node size / spacing from graph size and stage area. */
function graphAutoLayout({ total = 0, width = 0, height = 0 } = {}) {
  const canvas = document.querySelector("#graph-canvas");
  const w = Math.max(1, Number(width) || canvas?.width || 800);
  const h = Math.max(1, Number(height) || canvas?.height || 600);
  const count = Math.max(0, Number(total) || 0);
  const targetBudget = graphAutoTargetBudget(w, h);
  let density = 100;
  if (count > targetBudget) {
    density = graphDensityPercent(Math.max(5, Math.round((targetBudget / count) * 100)));
  }
  const visible = graphVisibleBudget(count, density);
  const nodeSize = graphAutoNodeSize(visible, w, h);
  return { density, nodeSize, nodeSpread: 1, targetBudget, visible };
}

function syncGraphAutoControls({ density, nodeSize, nodeSpread } = {}) {
  if (density != null) {
    graphState.densityLevel = graphDensityPercent(density);
    const input = document.querySelector("#graph-density-level");
    const value = document.querySelector("#graph-density-value");
    if (input) input.value = String(graphState.densityLevel);
    if (value) value.textContent = graphDensityLabel(graphState.densityLevel);
  }
  if (nodeSize != null) {
    graphState.nodeSize = Math.max(3, Math.min(12, Number(nodeSize) || 5));
    const input = document.querySelector("#graph-node-size");
    const value = document.querySelector("#graph-node-size-value");
    if (input) input.value = String(graphState.nodeSize);
    if (value) value.textContent = String(graphState.nodeSize);
  }
  if (nodeSpread != null) {
    graphState.nodeSpread = Math.max(1, Math.min(20, Number(nodeSpread) || 1));
    const input = document.querySelector("#graph-node-spread");
    const value = document.querySelector("#graph-node-spread-value");
    if (input) input.value = String(graphState.nodeSpread);
    if (value) value.textContent = String(graphState.nodeSpread);
  }
}

/** Apply auto density/size/spread for any flag still in auto mode. Returns true if layout inputs changed. */
function applyGraphAutoLayout({ reseed = false } = {}) {
  if (!graphState.densityAuto && !graphState.sizeAuto && !graphState.spreadAuto) return false;
  const canvas = document.querySelector("#graph-canvas");
  const total = graphState.totalNodesCount || graphState.allNodes.length || 0;
  const auto = graphAutoLayout({
    total,
    width: canvas?.width,
    height: canvas?.height,
  });
  const prev = {
    density: graphState.densityLevel,
    size: graphState.nodeSize,
    spread: graphState.nodeSpread,
  };
  const next = {};
  if (graphState.densityAuto) next.density = auto.density;
  if (graphState.sizeAuto) next.nodeSize = auto.nodeSize;
  if (graphState.spreadAuto) next.nodeSpread = auto.nodeSpread;
  syncGraphAutoControls(next);
  const changed =
    prev.density !== graphState.densityLevel
    || prev.size !== graphState.nodeSize
    || prev.spread !== graphState.nodeSpread;
  if (changed && reseed && graphState.allNodes.length) {
    const layoutChanged = prev.density !== graphState.densityLevel || prev.spread !== graphState.nodeSpread;
    if (!isGalaxyView() || layoutChanged) {
      applyGraphVisibility();
      seedGraphParticles({ relayout: true });
      if (!graphState.userZoomed) fitGraphToView();
    }
    wakeGraphAnimation();
  }
  return changed;
}

function resetGraphAutoLayoutFlags() {
  graphState.densityAuto = true;
  graphState.sizeAuto = true;
  graphState.spreadAuto = true;
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
      || neighborIds.has(node.id)
      || isGraphParentNode(node);
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

function graphNeighborhoodIds(seedIds) {
  const seeds = new Set((seedIds || []).filter(Boolean));
  if (!seeds.size) return seeds;
  const keep = new Set(seeds);
  const edges = graphState.allEdges.length ? graphState.allEdges : graphState.edges;
  for (const edge of edges) {
    if (seeds.has(edge.source)) keep.add(edge.target);
    if (seeds.has(edge.target)) keep.add(edge.source);
  }
  return keep;
}

function exactGraphFocusIds(query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return [];
  const workMatch = q.match(/^(?:ab#|#)?(\d{3,7})$/i);
  const workId = workMatch ? workMatch[1] : "";
  const ids = [];
  for (const node of graphState.allNodes) {
    const nodeId = String(node.id || "").toLowerCase();
    if (nodeId === q || (q.startsWith("node_") && nodeId.startsWith(q))) {
      ids.push(node.id);
      continue;
    }
    const meta = node.metadata || {};
    const nodeWork = String(meta.work_item_id || meta.workItemId || "").trim();
    if (workId && nodeWork === workId) ids.push(node.id);
  }
  return ids;
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
  const focusSeeds = [];
  if (graphState.neighborhoodId) focusSeeds.push(graphState.neighborhoodId);
  focusSeeds.push(...exactGraphFocusIds(query));
  const neighborhood = focusSeeds.length ? graphNeighborhoodIds(focusSeeds) : null;
  const filteredNodes = baseNodes.filter(node => {
    if (group && group !== "Projects" && node.group !== group && node.id !== graphState.neighborhoodId) return false;
    if (neighborhood) return neighborhood.has(node.id);
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
    el.removeAttribute("title");
    return;
  }
  el.hidden = false;
  const text = `${shown} of ${total} · ${graphDensityLabel()}`;
  el.textContent = text;
  el.title = `Showing ${shown} of ${total} nodes (${graphDensityLabel()}).`;
}

async function loadGraph() {
  const loadId = ++graphLoadGeneration;
  setGraphLoading(true);
  try {
  await syncGraphFilters();
  const taskFilterValue = document.querySelector("#graph-task-filter")?.value || "";
  const providerFilterValue = document.querySelector("#graph-provider-filter")?.value || "";
  const pinnedOnly = document.querySelector("#graph-pinned-filter")?.checked;
  graphState.searchQuery = document.querySelector("#graph-search")?.value || "";
  graphState.groupFilter = document.querySelector("#graph-group-filter")?.value || "";
  // Manual overrides only: auto density/size/spread are applied after the payload arrives.
  if (!graphState.densityAuto) {
    graphState.densityLevel = graphDensityPercent(document.querySelector("#graph-density-level")?.value || graphState.densityLevel || 40);
  }
  if (!graphState.sizeAuto) {
    graphState.nodeSize = Number(document.querySelector("#graph-node-size")?.value || graphState.nodeSize || 5);
  }
  if (!graphState.spreadAuto) {
    graphState.nodeSpread = Number(document.querySelector("#graph-node-spread")?.value || graphState.nodeSpread || 1);
  }
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
  if (loadId !== graphLoadGeneration) return;
  const sourceOptions = Array.isArray(payload.sources) ? payload.sources : [];
  if (sourceFilter) {
    const current = sourceFilter.value;
    sourceFilter.innerHTML = `<option value="">${escapeHtml(t("graph.filter.allSources"))}</option>` + sourceOptions.map(item => {
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
  if (graphState.neighborhoodId && allVisible.has(graphState.neighborhoodId)) {
    graphState.selectedId = graphState.neighborhoodId;
  } else if (graphState.selectedId && !allVisible.has(graphState.selectedId)) {
    graphState.selectedId = "";
  }
  resizeGraphCanvas();
  applyGraphAutoLayout();
  syncGraphFiltersDisclosure();
  applyGraphVisibility();
  graphState.physicsTicks = 0;
  graphState.physicsMax = Math.max(50, Math.round(140 / Math.sqrt(Math.max(1, graphState.nodes.length) / 50)));
  graphState.physicsActive = true;
  graphState.simAlpha = 1;
  graphState.simAlphaTarget = 0;
  graphState.fittedAfterSettle = false;
  seedGraphParticles({ relayout: true });
  bindGraphOnce();
  const needle = searchQuery.toLowerCase();
  let focusNode = null;
  if (graphState.neighborhoodId) {
    focusNode =
      graphState.nodes.find(node => node.id === graphState.neighborhoodId)
      || graphState.allNodes.find(node => node.id === graphState.neighborhoodId)
      || null;
  }
  if (!focusNode && needle) {
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
    seedGraphParticles({ relayout: true });
    renderGraphSelection(focusNode);
  } else {
    renderGraphSelection(graphState.nodes.find(node => node.id === graphState.selectedId) || null);
  }
  startGraphAnimation();
  graphState.userZoomed = false;
  syncGraphViewChrome();
  if (isGalaxyView()) {
    requestAnimationFrame(() => {
      fitGraphToView();
      requestAnimationFrame(() => fitGraphToView());
    });
  } else {
    graphState.scale = 1;
    graphState.offsetX = 0;
    graphState.offsetY = 0;
    graphState.fittedAfterSettle = false;
  }
  } finally {
    if (loadId === graphLoadGeneration) setGraphLoading(false);
  }
}

function reheatGraphForce(alpha = 0.9) {
  if (isGalaxyView()) return;
  graphState.simAlpha = Math.max(graphState.simAlpha || 0, Number(alpha) || 0.9);
  graphState.simAlphaTarget = 0;
  graphState.physicsActive = true;
  graphState.physicsTicks = 0;
  graphState.fittedAfterSettle = false;
  wakeGraphAnimation();
}

function scaleMapLayout(factor) {
  const list = graphState.particles;
  const scale = Number(factor);
  if (!list.length || !Number.isFinite(scale) || scale === 1 || scale <= 0) return;
  let cx = 0;
  let cy = 0;
  for (const particle of list) {
    cx += particle.x;
    cy += particle.y;
  }
  const n = list.length;
  cx /= n;
  cy /= n;
  for (const particle of list) {
    particle.x = cx + (particle.x - cx) * scale;
    particle.y = cy + (particle.y - cy) * scale;
    particle.vx = 0;
    particle.vy = 0;
  }
}

function seedGraphParticles({ relayout = false } = {}) {
  const canvas = document.querySelector("#graph-canvas");
  const width = canvas?.width || 800;
  const height = canvas?.height || 600;
  const existing = new Map(graphState.particles.map(item => [item.id, item]));
  graphState.particles = graphState.nodes.map((node, index) => {
    const old = existing.get(node.id);
    const keep = !relayout && old && Number.isFinite(old.x) && Number.isFinite(old.y);
    return {
      id: node.id,
      node,
      x: keep ? old.x : undefined,
      y: keep ? old.y : undefined,
      z: old && Number.isFinite(old.z) ? old.z : ((index % 7) - 3) / 3,
      vx: keep && Number.isFinite(old.vx) ? old.vx : 0,
      vy: keep && Number.isFinite(old.vy) ? old.vy : 0,
      fx: undefined,
      fy: undefined,
      gx: old && Number.isFinite(old.gx) ? old.gx : undefined,
      gy: old && Number.isFinite(old.gy) ? old.gy : undefined,
      gz: old && Number.isFinite(old.gz) ? old.gz : undefined,
      gvx: 0,
      gvy: 0,
      gvz: 0,
    };
  });
  seedForcePositions(graphState.particles, { width, height, force: relayout });
  graphState.simAlpha = relayout ? 1 : Math.max(graphState.simAlpha || 0, 0.4);
  graphState.simAlphaTarget = 0;
  graphState.fittedAfterSettle = false;
  if (isGalaxyView()) ensureGalaxyLayout({ force: !galaxyHasPositions(graphState.particles) });
}

function syncGraphFiltersDisclosure() {
  const details = document.querySelector(".graph-filters-more");
  if (!details) return;
  const source = document.querySelector("#graph-source-filter")?.value || "";
  const scope = document.querySelector("#graph-scope-filter")?.value || "";
  const task = document.querySelector("#graph-task-filter")?.value || "";
  const provider = document.querySelector("#graph-provider-filter")?.value || "";
  const pinned = Boolean(document.querySelector("#graph-pinned-filter")?.checked);
  const colorMode = document.querySelector("#graph-color-mode")?.value || "community";
  const manualLayout = !graphState.densityAuto || !graphState.sizeAuto || !graphState.spreadAuto;
  const active = Boolean(source || scope || task || provider || pinned || colorMode !== "community" || manualLayout);
  const summary = details.querySelector(".graph-filters-summary");
  if (summary) summary.textContent = active ? "More filters · on" : "More filters";
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
  const nodeSizeInput = document.querySelector("#graph-node-size");
  const nodeSpreadInput = document.querySelector("#graph-node-spread");
  const fitButton = document.querySelector("#graph-fit");
  const expandButton = document.querySelector("#graph-expand");
  const rebuildButton = document.querySelector("#graph-rebuild-links");
  const inspectHint = document.querySelector("#graph-inspect-hint");
  if (inspectHint) inspectHint.textContent = t(isGalaxyView() ? "graph.inspectHintGalaxy" : "graph.inspectHint");
  bindGraphNodeModalControls();
  syncGraphViewChrome();
  document.querySelectorAll("[data-graph-view]").forEach(button => {
    button.addEventListener("click", () => setGraphView(button.getAttribute("data-graph-view")));
  });
  const colorModeSelect = document.querySelector("#graph-color-mode");
  on(colorModeSelect, "change", () => {
    // Color depends only on data already in the payload — recolor, don't refetch.
    graphState.colorMode = colorModeSelect.value || "community";
    renderGraphLegend();
    wakeGraphAnimation();
    syncGraphFiltersDisclosure();
  });
  on(sourceFilter, "change", () => {
    syncGraphFiltersDisclosure();
    loadGraph().catch(showError);
  });
  on(scopeFilter, "change", () => {
    syncGraphFiltersDisclosure();
    loadGraph().catch(showError);
  });
  on(taskFilter, "change", () => {
    syncGraphFiltersDisclosure();
    loadGraph().catch(showError);
  });
  on(providerFilter, "change", () => {
    syncGraphFiltersDisclosure();
    loadGraph().catch(showError);
  });
  on(pinnedFilter, "change", () => {
    syncGraphFiltersDisclosure();
    loadGraph().catch(showError);
  });
  on(groupFilter, "change", () => {
    graphState.groupFilter = groupFilter.value || "";
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    applyGraphVisibility();
    seedGraphParticles({ relayout: true });
    fitGraphToView();
    wakeGraphAnimation();
  });
  on(searchInput, "input", () => {
    graphState.searchQuery = searchInput.value || "";
    if (graphState.neighborhoodId && searchInput.value.trim() !== graphState.neighborhoodId) {
      graphState.neighborhoodId = "";
    }
    graphState.physicsTicks = 0;
    graphState.physicsActive = true;
    // Local filter for already-loaded nodes (label/text), then debounced backend
    // fetch so ID search can find nodes outside the density budget.
    applyGraphVisibility();
    seedGraphParticles({ relayout: true });
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
    const syncDensityBadge = () => {
      if (!densityValue) return;
      graphState.densityLevel = graphDensityPercent(densityInput.value || 40);
      densityValue.textContent = graphDensityLabel();
    };
    const applyDensity = () => {
      graphState.densityAuto = false;
      syncDensityBadge();
      syncGraphFiltersDisclosure();
      if (!graphState.allNodes.length) {
        loadGraph().catch(showError);
        return;
      }
      applyGraphVisibility();
      seedGraphParticles({ relayout: true });
      if (!graphState.userZoomed) fitGraphToView();
      wakeGraphAnimation();
    };
    syncDensityBadge();
    on(densityInput, "input", applyDensity);
    on(densityInput, "change", applyDensity);
  }
  if (nodeSizeInput) {
    const nodeSizeValue = document.querySelector("#graph-node-size-value");
    const syncNodeSize = () => {
      graphState.nodeSize = Number(nodeSizeInput.value || 5);
      if (nodeSizeValue) nodeSizeValue.textContent = String(graphState.nodeSize);
    };
    syncNodeSize();
    on(nodeSizeInput, "input", () => {
      graphState.sizeAuto = false;
      syncNodeSize();
      syncGraphFiltersDisclosure();
      graphState.physicsActive = true;
      wakeGraphAnimation();
      drawGraph();
    });
    on(nodeSizeInput, "change", () => {
      graphState.sizeAuto = false;
      syncNodeSize();
      syncGraphFiltersDisclosure();
      graphState.physicsActive = true;
      wakeGraphAnimation();
      drawGraph();
    });
  }
  if (nodeSpreadInput) {
    const nodeSpreadValue = document.querySelector("#graph-node-spread-value");
    const syncNodeSpread = () => {
      graphState.nodeSpread = Number(nodeSpreadInput.value || 1);
      if (nodeSpreadValue) nodeSpreadValue.textContent = String(graphState.nodeSpread);
    };
    syncNodeSpread();
    on(nodeSpreadInput, "input", () => {
      graphState.spreadAuto = false;
      const previous = Math.max(1, Number(graphState.nodeSpread) || 1);
      syncNodeSpread();
      syncGraphFiltersDisclosure();
      const next = Math.max(1, Number(graphState.nodeSpread) || 1);
      if (isGalaxyView()) {
        scaleGalaxyPositions(graphState.particles, next / previous);
        graphState.galaxyLaidSpread = next;
        if (!graphState.userZoomed) frameGalaxy();
      }
      graphState.physicsActive = true;
      if (!isGalaxyView()) reheatGraphForce(0.85);
      wakeGraphAnimation();
      drawGraph();
    });
    on(nodeSpreadInput, "change", () => {
      graphState.spreadAuto = false;
      syncNodeSpread();
      syncGraphFiltersDisclosure();
      graphState.physicsActive = true;
      wakeGraphAnimation();
    });
  }
  syncGraphFiltersDisclosure();
  const filtersMore = document.querySelector(".graph-filters-more");
  const closeGraphFiltersMore = event => {
    if (!filtersMore?.open) return;
    if (event.type === "keydown" && event.key !== "Escape") return;
    if (event.type !== "keydown" && filtersMore.contains(event.target)) return;
    filtersMore.open = false;
  };
  document.addEventListener("click", closeGraphFiltersMore);
  document.addEventListener("keydown", closeGraphFiltersMore);
  on(fitButton, "click", () => {
    graphState.userZoomed = false;
    if (isGalaxyView()) {
      frameGalaxy();
      wakeGraphAnimation();
      return;
    }
    fitGraphToView();
  });
  on(expandButton, "click", () => toggleGraphExpand(true));
  on("#graph-fit-expanded", "click", () => {
    graphState.userZoomed = false;
    if (isGalaxyView()) {
      frameGalaxy();
      wakeGraphAnimation();
      return;
    }
    fitGraphToView();
  });
  on("#graph-close-expand", "click", () => toggleGraphExpand(false));
  on(rebuildButton, "click", async () => {
    const summary = document.querySelector("#graph-link-summary");
    if (summary) {
      summary.hidden = false;
      summary.textContent = t("graph.rebuild.linking");
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
  canvas.addEventListener("contextmenu", event => {
    if (isGalaxyView() || graphState.mapPanning) event.preventDefault();
  });
  canvas.addEventListener("pointerdown", event => {
    if (isGalaxyView()) {
      const hit = hitGraphNode(event);
      graphState.dragMoved = false;
      graphState.draggingId = "";
      graphState.galaxyHitId = event.button === 0 && !wantsGalaxyPan(event) && hit ? hit.id : "";
      graphState.galaxyPan = wantsGalaxyPan(event);
      graphState.orbiting = true;
      graphState.orbitLastX = event.clientX;
      graphState.orbitLastY = event.clientY;
      canvas.setPointerCapture(event.pointerId);
      syncGraphViewChrome();
      wakeGraphAnimation();
      return;
    }
    const hit = hitGraphNode(event);
    if (hit && event.button === 0 && !event.shiftKey) {
      graphState.draggingId = hit.id;
      graphState.dragMoved = false;
      hit.fx = hit.x;
      hit.fy = hit.y;
      graphState.simAlphaTarget = 0.3;
      graphState.physicsActive = true;
      selectGraphNode(hit.node, { openModal: false });
      canvas.setPointerCapture(event.pointerId);
      wakeGraphAnimation();
      return;
    }
    graphState.mapPanning = true;
    graphState.dragMoved = false;
    graphState.orbitLastX = event.clientX;
    graphState.orbitLastY = event.clientY;
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", event => {
    const hit = hitGraphNode(event);
    const nextHoverId = hit ? hit.id : "";
    if (nextHoverId !== graphState.hoverId) {
      graphState.hoverId = nextHoverId;
      wakeGraphAnimation();
    }
    if (isGalaxyView() && graphState.orbiting) {
      const dx = event.clientX - graphState.orbitLastX;
      const dy = event.clientY - graphState.orbitLastY;
      if (Math.hypot(dx, dy) > 8) {
        graphState.dragMoved = true;
        graphState.userZoomed = true;
      }
      graphState.orbitLastX = event.clientX;
      graphState.orbitLastY = event.clientY;
      if (graphState.galaxyPan) {
        const rect = canvas.getBoundingClientRect();
        const sx = canvas.width / Math.max(rect.width, 1);
        const sy = canvas.height / Math.max(rect.height, 1);
        panGalaxyCamera(graphState.galaxyCamera, dx * sx, dy * sy, canvas.width, canvas.height);
      } else {
        orbitGalaxyCamera(graphState.galaxyCamera, dx, dy);
      }
      wakeGraphAnimation();
      return;
    }
    if (graphState.mapPanning) {
      const dx = event.clientX - graphState.orbitLastX;
      const dy = event.clientY - graphState.orbitLastY;
      if (Math.hypot(dx, dy) > 8) {
        graphState.dragMoved = true;
        graphState.userZoomed = true;
      }
      graphState.orbitLastX = event.clientX;
      graphState.orbitLastY = event.clientY;
      const rect = canvas.getBoundingClientRect();
      graphState.offsetX += dx * (canvas.width / Math.max(rect.width, 1));
      graphState.offsetY += dy * (canvas.height / Math.max(rect.height, 1));
      wakeGraphAnimation();
      return;
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
        particle.fx = point.x;
        particle.fy = point.y;
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
    const wasMapPanning = graphState.mapPanning;
    const noDrag = !graphState.dragMoved;
    const galaxyClick = isGalaxyView() && graphState.orbiting && !graphState.galaxyPan && noDrag && event.button === 0;
    const hitId = graphState.galaxyHitId;
    graphState.draggingId = "";
    graphState.orbiting = false;
    graphState.galaxyPan = false;
    graphState.mapPanning = false;
    graphState.galaxyHitId = "";
    graphState.simAlphaTarget = 0;
    for (const particle of graphState.particles) {
      particle.fx = undefined;
      particle.fy = undefined;
    }
    try { canvas.releasePointerCapture(event.pointerId); } catch (_) {}
    if (galaxyClick) {
      const particle = hitId ? graphState.particles.find(item => item.id === hitId) : null;
      selectGraphNode(particle?.node || null);
    } else if (!isGalaxyView() && wasMapPanning && noDrag && event.button === 0) {
      selectGraphNode(null);
    }
    syncGraphViewChrome();
    wakeGraphAnimation();
  });
  canvas.addEventListener("pointercancel", () => {
    graphState.draggingId = "";
    graphState.orbiting = false;
    graphState.galaxyPan = false;
    graphState.mapPanning = false;
    graphState.galaxyHitId = "";
    graphState.simAlphaTarget = 0;
    for (const particle of graphState.particles) {
      particle.fx = undefined;
      particle.fy = undefined;
    }
    syncGraphViewChrome();
    wakeGraphAnimation();
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
    if (isGalaxyView()) {
      if (event.shiftKey) {
        const rect = canvasEl.getBoundingClientRect();
        const sx = canvasEl.width / Math.max(rect.width, 1);
        const sy = canvasEl.height / Math.max(rect.height, 1);
        panGalaxyCamera(graphState.galaxyCamera, -event.deltaX * sx, -event.deltaY * sy, canvasEl.width, canvasEl.height);
      } else {
        dollyGalaxyCamera(graphState.galaxyCamera, event.deltaY);
      }
      graphState.userZoomed = true;
      wakeGraphAnimation();
      return;
    }
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
    const resized = resizeGraphCanvas();
    if (resized) applyGraphAutoLayout({ reseed: true });
    if (!graphState.userZoomed) fitGraphToView();
    wakeGraphAnimation();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) wakeGraphAnimation();
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
    const loading = document.querySelector("#graph-loading");
    if (loading) expandStage.appendChild(loading);
    overlay.removeAttribute("hidden");
    document.body.style.overflow = "hidden";
    releaseGraphExpandFocus = trapFocus(overlay);
  } else {
    normalStage.appendChild(canvas);
    const loading = document.querySelector("#graph-loading");
    if (loading) normalStage.appendChild(loading);
    overlay.setAttribute("hidden", "");
    document.body.style.overflow = "";
    if (releaseGraphExpandFocus) {
      releaseGraphExpandFocus();
      releaseGraphExpandFocus = null;
    }
  }
  resizeGraphCanvas();
  graphState.userZoomed = false;
  applyGraphAutoLayout({ reseed: true });
  if (!graphState.densityAuto && !graphState.sizeAuto && !graphState.spreadAuto) {
    seedGraphParticles({ relayout: true });
  }
  fitGraphToView();
}

function resizeGraphCanvas() {
  const canvas = document.querySelector("#graph-canvas");
  const stage = canvas?.closest(".graph-stage, .graph-expand-stage");
  if (!canvas || !stage) return false;
  const rect = stage.getBoundingClientRect();
  if (rect.width < 8 || rect.height < 8) return false;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, Math.floor(rect.width * ratio));
  const height = Math.max(240, Math.floor(rect.height * ratio));
  if (canvas.width === width && canvas.height === height) return false;
  canvas.width = width;
  canvas.height = height;
  if (graphState.particles.length && !graphState.userZoomed) {
    // Keep world layout; only reseed when empty.
  } else if (!graphState.particles.length) {
    seedGraphParticles();
  }
  return true;
}

function observeGraphStage() {
  if (graphState.stageObserver) return;
  graphState.stageObserver = new ResizeObserver(() => {
    const resized = resizeGraphCanvas();
    if (resized) applyGraphAutoLayout({ reseed: true });
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
    if (isGalaxyView() && ![particle.gx, particle.gy, particle.gz].every(Number.isFinite)) {
      broken = true;
      break;
    }
  }
  if (broken) {
    seedGraphParticles({ relayout: true });
    return true;
  }
  return false;
}

function fitGraphToView() {
  if (isGalaxyView()) {
    resizeGraphCanvas();
    ensureGalaxyLayout();
    frameGalaxy();
    wakeGraphAnimation();
    return;
  }
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
    const pad = graphNodeRadius(particle) + 36;
    minX = Math.min(minX, particle.x - pad);
    maxX = Math.max(maxX, particle.x + pad);
    minY = Math.min(minY, particle.y - pad);
    maxY = Math.max(maxY, particle.y + pad);
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
  const scale = Math.max(0.04, Number.isFinite(fitScale) ? fitScale : 1);
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
  if (isGalaxyView()) {
    const canvas = document.querySelector("#graph-canvas");
    if (!canvas) return null;
    const point = canvasPointFromEvent(event, canvas);
    return hitGalaxyNode(
      point.x,
      point.y,
      graphState.particles,
      graphState.galaxyCamera,
      canvas.width,
      canvas.height,
      graphState.nodeSize,
    );
  }
  const point = graphPointer(event);
  for (const particle of [...graphState.particles].reverse()) {
    const radius = graphNodeWorldRadius(particle);
    const dx = point.x - particle.x;
    const dy = point.y - particle.y;
    if (Math.sqrt(dx * dx + dy * dy) <= radius + 14 / Math.max(graphState.scale, 0.001)) return particle;
  }
  return null;
}

function startGraphAnimation() {
  cancelAnimationFrame(graphState.animationId);
  graphState.animationId = 0;
  resizeGraphCanvas();
  const tick = () => {
    graphState.animationId = 0;
    if (!graphStageVisible()) return;
    const reduced = prefersGraphReducedMotion();
    if (isGalaxyView()) {
      if (!reduced || graphState.physicsActive || graphState.draggingId) {
        ensureGalaxyLayout();
        stepGalaxyPhysics(graphState.particles, graphState.edges, {
          draggingId: graphState.draggingId,
          live: !reduced,
        });
        graphState.physicsTicks += 1;
        if (!graphState.userZoomed && !graphState.orbiting && !graphState.draggingId) {
          frameGalaxy({ ease: true });
        }
        if (reduced && !graphState.draggingId) {
          if (graphState.physicsTicks >= Math.min(36, graphState.physicsMax)) graphState.physicsActive = false;
        } else graphState.physicsActive = true;
      }
    } else {
      const running = graphState.particles.length
        && (graphState.physicsActive || graphState.draggingId || graphState.simAlpha > FORCE_ALPHA_MIN);
      if (running) {
        const settled = stepGraphPhysics({ live: !reduced });
        if (!graphState.userZoomed && graphState.simAlpha > 0.02 && !graphState.draggingId) {
          graphState.physicsTicks += 1;
          if (graphState.physicsTicks % 10 === 0) fitGraphToView();
        }
        if (settled && !graphState.draggingId) {
          graphState.physicsActive = false;
          if (!graphState.userZoomed && !graphState.fittedAfterSettle) {
            graphState.fittedAfterSettle = true;
            fitGraphToView();
          }
        } else {
          graphState.physicsActive = true;
        }
      }
    }
    drawGraph();
    const keep = isGalaxyView()
      ? (!reduced || graphState.physicsActive || graphState.draggingId || graphState.hoverId || graphState.orbiting)
      : (graphState.physicsActive || graphState.draggingId || graphState.hoverId || graphState.mapPanning || graphState.simAlpha > FORCE_ALPHA_MIN);
    if (keep) graphState.animationId = requestAnimationFrame(tick);
  };
  tick();
}

function wakeGraphAnimation() {
  if (!graphState.animationId) startGraphAnimation();
}

function stepGraphPhysics({ live = true } = {}) {
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas) return true;
  const particles = graphState.particles;
  if (!particles.length) {
    graphState.simAlpha = 0;
    return true;
  }
  sanitizeGraphParticles();
  const centerX = canvas.width / 2;
  const centerY = canvas.height / 2;
  const layoutEdges = graphState.edges || [];
  const params = graphForceParams({
    nodeSpread: graphNodeSpread(),
    nodeCount: particles.length,
  });
  const ticks = live ? 1 : 24;
  let settled = false;
  for (let i = 0; i < ticks; i += 1) {
    const result = stepForceSimulation(particles, layoutEdges, {
      alpha: graphState.simAlpha,
      alphaTarget: graphState.simAlphaTarget,
      draggingId: graphState.draggingId,
      centerX,
      centerY,
      params,
      radiusOf: graphNodeRadius,
    });
    graphState.simAlpha = result.alpha;
    settled = result.settled;
  }
  const span = Math.max(canvas.width, canvas.height) * 8;
  for (const particle of particles) {
    if (!Number.isFinite(particle.x) || !Number.isFinite(particle.y)) {
      particle.x = centerX;
      particle.y = centerY;
      particle.vx = 0;
      particle.vy = 0;
    } else {
      particle.x = Math.max(centerX - span, Math.min(centerX + span, particle.x));
      particle.y = Math.max(centerY - span, Math.min(centerY + span, particle.y));
      particle.vx = Math.max(-40, Math.min(40, particle.vx));
      particle.vy = Math.max(-40, Math.min(40, particle.vy));
    }
  }
  return settled && !graphState.draggingId;
}

function isDarkTheme() { return document.documentElement.dataset.theme === "dark"; }
function graphColors() {
  if (isDarkTheme()) {
    return {
      bg0: "#07070a", bg1: "#000000",
      edge: "rgba(180, 190, 210, 0.22)", edgeActive: "rgba(255, 255, 255, 0.92)",
      nodeStroke: "rgba(255,255,255,0.18)", nodeStrokeActive: "rgba(255,255,255,0.95)",
      nodeShadow: "rgba(255,255,255,0.18)", nodeShadowActive: "rgba(255,255,255,0.35)",
      label: "rgba(245,245,247,0.92)", labelShadow: "rgba(0,0,0,0.85)",
    };
  }
  return {
    bg0: "#f4f4f5", bg1: "#ececee",
    edge: "rgba(24, 24, 27, 0.18)", edgeActive: "rgba(24, 24, 27, 0.72)",
    nodeStroke: "rgba(24,24,27,0.12)", nodeStrokeActive: "rgba(24,24,27,0.7)",
    nodeShadow: "rgba(24,24,27,0.12)", nodeShadowActive: "rgba(24,24,27,0.22)",
    label: "#18181b", labelShadow: "rgba(255,255,255,0.75)",
  };
}

function drawGraph() {
  const canvas = document.querySelector("#graph-canvas");
  if (!canvas) return;
  if (isGalaxyView()) {
    ensureGalaxyLayout();
    const ctx = canvas.getContext("2d");
    drawGalaxy(ctx, canvas, {
      particles: graphState.particles,
      edges: graphState.edges,
      camera: graphState.galaxyCamera,
      selectedId: graphState.selectedId,
      hoverId: graphState.hoverId,
      nodeColor: graphNodeColor,
      nodeSize: graphState.nodeSize,
      labelText: graphLabelText,
      alwaysLabel: graphLabelAlwaysVisible,
      reducedMotion: prefersGraphReducedMotion(),
    });
    return;
  }
  const ctx = canvas.getContext("2d");
  const colors = graphColors();
  const scale = Math.max(graphState.scale, 0.001);
  const inv = 1 / scale;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = colors.bg1;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const glow = ctx.createRadialGradient(canvas.width * 0.5, canvas.height * 0.48, 12, canvas.width * 0.5, canvas.height * 0.5, Math.max(canvas.width, canvas.height) * 0.7);
  glow.addColorStop(0, colors.bg0);
  glow.addColorStop(1, colors.bg1);
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (![scale, graphState.offsetX, graphState.offsetY].every(Number.isFinite)) return;
  ctx.setTransform(scale, 0, 0, scale, graphState.offsetX, graphState.offsetY);
  const byId = new Map(graphState.particles.map(item => [item.id, item]));
  const focusId = graphState.hoverId;
  const linked = new Set(focusId ? [focusId] : []);
  if (focusId) {
    for (const edge of graphState.edges) {
      if (edge.source === focusId) linked.add(edge.target);
      if (edge.target === focusId) linked.add(edge.source);
    }
  }
  const isolating = Boolean(focusId);
  for (const edge of graphState.edges) {
    const a = byId.get(edge.source);
    const b = byId.get(edge.target);
    if (!a || !b) continue;
    const inferred = String(edge.provenance || "").toUpperCase() === "INFERRED";
    const hot = graphState.selectedId && (edge.source === graphState.selectedId || edge.target === graphState.selectedId);
    const active = (focusId && (edge.source === focusId || edge.target === focusId)) || hot;
    if (inferred && !active) continue;
    const rgb = mixGraphEdgeColor(graphNodeColor(a.node), graphNodeColor(b.node));
    let alpha = active ? 0.92 : inferred ? 0.18 : 0.38;
    if (isolating && !active) alpha *= 0.12;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.strokeStyle = `rgba(${rgb.r},${rgb.g},${rgb.b},${alpha})`;
    ctx.lineWidth = (active ? 1.7 : 0.9) * inv;
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  for (const particle of graphState.particles) {
    drawGraphNode(ctx, particle, colors, inv, isolating && !linked.has(particle.id));
  }
  drawGraphLabels(ctx, graphState.particles, colors, scale, inv);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}

function mixGraphEdgeColor(a, b) {
  const left = parseGraphColor(a);
  const right = parseGraphColor(b);
  return {
    r: Math.round((left.r + right.r) / 2),
    g: Math.round((left.g + right.g) / 2),
    b: Math.round((left.b + right.b) / 2),
  };
}

function graphNodeRadius(particle) {
  const size = Math.max(3, Math.min(12, Number(graphState.nodeSize) || 5));
  const degree = Number(particle?.node?.degree) || 0;
  const hub = particle?.node?.type === "Project" ? 1.35 : 1;
  return Math.max(2.1, size * hub * (0.3 + Math.log1p(degree) * 0.18));
}

function graphNodeWorldRadius(particle) {
  return graphNodeRadius(particle);
}

function graphNodeSpread() {
  return Math.max(1, Math.min(20, Number(graphState.nodeSpread) || 1));
}

function isGraphParentNode(node) {
  const meta = node?.metadata || {};
  return Boolean(meta.hub || meta.scope_root || meta.synthetic)
    || node?.type === "Project"
    || node?.type === "Task"
    || node?.type === "Provider";
}

function drawGraphNode(ctx, particle, colors, inv = 1, dim = false) {
  colors = colors || graphColors();
  const node = particle.node;
  const radius = graphNodeWorldRadius(particle);
  const color = graphNodeColor(node);
  const active = particle.id === graphState.selectedId;
  const hover = particle.id === graphState.hoverId;
  const superseded = Boolean(node.superseded || node?.metadata?.invalid_at);
  ctx.save();
  ctx.globalAlpha = dim ? 0.16 : superseded ? 0.32 : 1;
  if (!dim && (active || hover)) {
    ctx.beginPath();
    ctx.arc(particle.x, particle.y, radius * (active ? 2.4 : 1.9), 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.globalAlpha = active ? 0.28 : 0.18;
    ctx.fill();
    ctx.globalAlpha = dim ? 0.16 : 1;
  }
  ctx.beginPath();
  ctx.arc(particle.x, particle.y, radius + (hover ? 0.6 * inv : 0), 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  if (active) {
    ctx.lineWidth = 1.4 * inv;
    ctx.strokeStyle = colors.nodeStrokeActive;
    ctx.stroke();
  }
  ctx.restore();
}

function graphLabelText(node) {
  const raw = String(node?.label || "");
  return raw.length > 22 ? `${raw.slice(0, 21)}…` : raw;
}

function graphLabelFontPx(inv) {
  return Math.max(10, 11 * Math.min(inv, 1.35));
}

/** Main hubs keep a persistent label; leaves only appear on hover/selection. */
function graphLabelAlwaysVisible(node) {
  return Boolean(node?.pinned) || isGraphParentNode(node);
}

function graphLabelPriority(particle) {
  const node = particle.node;
  if (particle.id === graphState.selectedId) return 100;
  if (particle.id === graphState.hoverId) return 90;
  if (node.type === "Project") return 80;
  if (node.pinned) return 70;
  if (isGraphParentNode(node)) return 60;
  return 0;
}

function graphLabelBoxesOverlap(a, b) {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

function drawGraphLabels(ctx, particles, colors, scale, inv) {
  if (!particles.length) return;
  const fontPx = graphLabelFontPx(inv);
  const fade = scale;
  const candidates = particles.filter(particle => {
    const id = particle.id;
    if (id === graphState.selectedId || id === graphState.hoverId) return true;
    if (graphLabelAlwaysVisible(particle.node)) return fade >= 1.15;
    return fade >= 1.85;
  }).map(particle => {
    const text = graphLabelText(particle.node);
    const radius = graphNodeWorldRadius(particle);
    const pad = 5 * inv;
    const lx = particle.x;
    const ly = particle.y + radius + pad;
    const textW = Math.max(16, text.length * fontPx * 0.72) + 8;
    const textH = fontPx * 1.45;
    return {
      particle,
      text,
      lx,
      ly,
      align: "center",
      baseline: "top",
      priority: graphLabelPriority(particle),
      box: { x: lx - textW / 2, y: ly, w: textW, h: textH },
    };
  });
  candidates.sort((a, b) => b.priority - a.priority || String(a.text).localeCompare(String(b.text)));
  const placed = [];
  for (const item of candidates) {
    // Hover/selection always wins; hubs skip collisions against lower-priority leaves only.
    const force = item.priority >= 90;
    if (!force && placed.some(other => graphLabelBoxesOverlap(item.box, other.box))) continue;
    placed.push(item);
  }
  ctx.save();
  ctx.font = `${fontPx}px Segoe UI, Arial`;
  ctx.fillStyle = colors.label;
  ctx.shadowColor = colors.labelShadow;
  ctx.shadowBlur = 3 * inv;
  for (const item of placed) {
    ctx.textAlign = item.align;
    ctx.textBaseline = item.baseline;
    ctx.fillText(item.text, item.lx, item.ly);
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
      : `<span class="graph-legend-item">${escapeHtml(t("graph.legend.noThemes"))}</span>`;
  } else {
    legend.innerHTML = groups.map(group => `<span class="graph-legend-item"><span class="graph-legend-dot" style="background:${escapeHtml(graphGroupColor(group))}"></span>${escapeHtml(group)}</span>`).join("");
  }
  if (groupFilter) {
    const current = graphState.groupFilter || groupFilter.value || "";
    const known = new Set(groups);
    if (current && !known.has(current)) known.add(current);
    const options = [`<option value="">${escapeHtml(t("graph.filter.allGroups"))}</option>`]
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
  taskFilter.innerHTML = `<option value="">${escapeHtml(t("graph.filter.allTasks"))}</option>` + tasksPayload.tasks.map(task => `<option value="${escapeHtml(task.id)}">${escapeHtml(task.title)}</option>`).join("");
  providerFilter.innerHTML = `<option value="">${escapeHtml(t("graph.filter.allProviders"))}</option>` + providersPayload.providers.map(provider => `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.label)}</option>`).join("");
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

// Close/Esc must work even when the card is opened from Search before the
// graph canvas has ever been initialized (bindGraphOnce waits on loadGraph).
function bindGraphNodeModalControls() {
  if (graphState.modalControlsBound) return;
  const modal = document.querySelector("#graph-node-modal");
  if (!modal || typeof modal.addEventListener !== "function") return;
  graphState.modalControlsBound = true;
  modal.addEventListener("click", event => {
    if (!event.target.closest("[data-close-graph-node-modal]")) return;
    event.preventDefault();
    closeGraphNodeModal();
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (graphState.modalOpen) {
      event.preventDefault();
      closeGraphNodeModal();
      return;
    }
    if (graphState.expanded) {
      event.preventDefault();
      toggleGraphExpand(false);
      return;
    }
    if (graphState.selectedId && graphStageVisible()) {
      event.preventDefault();
      selectGraphNode(null);
    }
  });
}

function mergeGraphPayload(payload) {
  const incoming = Array.isArray(payload?.nodes) ? payload.nodes.map(enrichGraphNode) : [];
  if (!incoming.length) return;
  const byId = new Map(graphState.allNodes.map(node => [node.id, node]));
  for (const node of incoming) {
    if (node?.id) byId.set(node.id, node);
  }
  graphState.allNodes = [...byId.values()];
  const edgeKey = edge => edge.id || `${edge.source}|${edge.target}|${edge.type || ""}`;
  const seen = new Set(graphState.allEdges.map(edgeKey));
  for (const edge of payload.edges || []) {
    const key = edgeKey(edge);
    if (seen.has(key)) continue;
    seen.add(key);
    graphState.allEdges.push(edge);
  }
}

async function prefetchGraphNeighborhood(nodeId) {
  const id = String(nodeId || "").trim();
  if (!id) return null;
  const payload = await api(`/api/graph?project_id=${projectParam()}&q=${encodeURIComponent(id)}&limit=80`);
  mergeGraphPayload(payload);
  return graphState.allNodes.find(node => node.id === id) || null;
}

async function openMemoryNodeCard(nodeId, fallbackNode) {
  const id = String(nodeId || fallbackNode?.id || "").trim();
  if (!id) return;
  const existing =
    graphState.allNodes.find(node => node.id === id)
    || graphState.nodes.find(node => node.id === id)
    || fallbackNode;
  if (existing) openGraphNodeModal(existing, { skipGraphUpdate: true });
  const fetched = await prefetchGraphNeighborhood(id).catch(() => null);
  const node = fetched || existing;
  if (!node) return;
  if (graphState.selectedId && graphState.selectedId !== id && graphState.selectedId !== node.id) return;
  if (existing && !isGraphNodeModalOpen()) return;
  openGraphNodeModal(node, { skipGraphUpdate: true });
}

async function showNodeInMemoryGraph(nodeId) {
  const id = String(nodeId || "").trim();
  if (!id) return;
  closeGraphNodeModal();
  graphState.neighborhoodId = id;
  graphState.selectedId = id;
  graphState.searchQuery = id;
  setElementValue("#graph-search", id);
  setElementValue("#graph-source-filter", "");
  setElementValue("#graph-scope-filter", "");
  setElementValue("#graph-group-filter", "");
  const pinned = document.querySelector("#graph-pinned-filter");
  if (pinned) pinned.checked = false;
  graphState.groupFilter = "";
  switchMemoryTab("graph");
  switchView("memory");
  await loadGraph();
}

function openGraphNodeModal(node, options = {}) {
  if (!node) return;
  bindGraphNodeModalControls();
  const skipGraphUpdate = Boolean(options.skipGraphUpdate);
  const prevSelected = graphState.selectedId;
  graphState.selectedId = node.id;
  if (!skipGraphUpdate) {
    graphState.physicsActive = true;
    graphState.physicsTicks = Math.min(graphState.physicsTicks, Math.floor(graphState.physicsMax * 0.6));
    if (prevSelected !== node.id || !graphState.particles.some(item => item.id === node.id)) {
      const prevIds = graphState.nodes.map(item => item.id).join("\0");
      applyGraphVisibility();
      const nextIds = graphState.nodes.map(item => item.id).join("\0");
      if (prevIds !== nextIds || !graphState.particles.some(item => item.id === node.id)) {
        seedGraphParticles();
      }
      graphState.physicsTicks = 0;
      graphState.physicsActive = true;
    }
    wakeGraphAnimation();
  }
  renderGraphDetail(node);
  const modal = document.querySelector("#graph-node-modal");
  if (!modal) return;
  modal.removeAttribute("hidden");
  // Install the trap only on a fresh open so re-inspecting a neighbour keeps
  // the original trigger as the focus-restore target.
  if (!graphState.modalOpen) releaseGraphNodeModalFocus = trapFocus(modal);
  graphState.modalOpen = true;
  modal.querySelector(".modal-close")?.focus();
}

function closeGraphNodeModal() {
  const modal = document.querySelector("#graph-node-modal");
  if (modal) modal.setAttribute("hidden", "");
  graphState.modalOpen = false;
  if (releaseGraphNodeModalFocus) {
    releaseGraphNodeModalFocus();
    releaseGraphNodeModalFocus = null;
  }
}

function restoreGraphVisibility() {
  const prevIds = graphState.nodes.map(item => item.id).join("\0");
  applyGraphVisibility();
  const nextIds = graphState.nodes.map(item => item.id).join("\0");
  if (prevIds === nextIds) return;
  seedGraphParticles();
  if (!graphState.userZoomed) fitGraphToView();
}

function selectGraphNode(node, options = {}) {
  if (!node) {
    const hadSelection = Boolean(graphState.selectedId);
    graphState.selectedId = "";
    if (hadSelection) restoreGraphVisibility();
    wakeGraphAnimation();
    return;
  }
  graphState.selectedId = node.id;
  graphState.physicsActive = true;
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
  add(meta.granola_url, t("graph.link.granola"), "granola");
  add(meta.wiki_url || meta.web_url, t("graph.link.page"), "source");
  add(meta.source_ref, t("graph.link.source"), "source");
  add(meta.url, t("graph.link.link"), "source");
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
  if (!raw) return `<p class="mem-empty">${escapeHtml(t("graph.node.noDescription"))}</p>`;
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
  return blocks.map(block => {
    const inner = renderMemoryMarkdown(block.lines, node);
    if (!inner.trim() && !block.label) return "";
    const labelHtml = block.label ? `<div class="mem-label">${escapeHtml(block.label)}</div>` : "";
    const cls = block.label ? `mem-block mem-block-${block.label.toLowerCase()}` : "mem-block";
    return `<div class="${cls}">${labelHtml}<div class="mem-content">${inner}</div></div>`;
  }).join("");
}

function formatGraphNodeDate(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw.slice(0, 10);
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function graphNodeConfidencePct(node) {
  const value = Number(node?.confidence);
  if (!Number.isFinite(value) || value <= 0) return 0;
  return Math.round(value <= 1 ? value * 100 : value);
}

function graphNodeBadgeClass(kind, value = "") {
  const text = String(value || "").toLowerCase();
  if (kind === "status" && (text === "active" || text === "done" || text === "closed")) return "graph-node-pill graph-node-pill-ok";
  if (kind === "confidence") return "graph-node-pill graph-node-pill-ok";
  if (kind === "ado") return "badge badge-azure";
  if (kind === "code") return "badge badge-code";
  if (kind === "type") {
    if (/(bug|issue|defect)/.test(text)) return "graph-node-pill graph-node-pill-warn";
    if (/(rule|decision|constraint)/.test(text)) return "graph-node-pill graph-node-pill-accent";
    if (/(meeting|granola)/.test(text)) return "graph-node-pill graph-node-pill-cyan";
  }
  return "graph-node-pill";
}

function graphNodeMetaCells(node) {
  const meta = node?.metadata || {};
  const cells = [];
  const push = (label, valueHtml) => {
    if (!valueHtml) return;
    cells.push({ label, valueHtml });
  };
  if (meta.assigned_to) {
    push(t("graph.node.assignee"), `<span class="graph-node-meta-person">${escapeHtml(String(meta.assigned_to))}</span>`);
  }
  if (meta.iteration_path) push(t("graph.node.iteration"), escapeHtml(String(meta.iteration_path)));
  if (meta.area_path) push(t("graph.node.area"), escapeHtml(String(meta.area_path)));
  const created = formatGraphNodeDate(node?.created_at);
  if (created) push(t("graph.node.created"), escapeHtml(created));
  return cells;
}

function setSectionHidden(selector, hidden) {
  const el = document.querySelector(selector);
  if (!el) return;
  if (hidden) el.setAttribute("hidden", "");
  else el.removeAttribute("hidden");
}

async function copyGraphNodeText(text, okMessage) {
  const value = String(text || "");
  if (!value) return;
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
    else {
      const area = document.createElement("textarea");
      area.value = value;
      area.setAttribute("readonly", "");
      area.style.position = "absolute";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    showSnackbar(okMessage, "success");
  } catch (error) {
    showSnackbar(error?.message || t("graph.node.copyFailed"), "error");
  }
}

function askAboutGraphNode(node) {
  if (!node) return;
  const snippet = String(node.text || "").replace(/\s+/g, " ").trim().slice(0, 500);
  const lines = [
    t("graph.node.askPromptIntro"),
    `Title: ${node.label || ""}`,
    `Type: ${node.type || ""}`,
    `Scope: ${node.scope || ""}`,
    `ID: ${node.id || ""}`,
  ];
  if (snippet) lines.push(`Content: ${snippet}`);
  closeGraphNodeModal();
  queueAskFollowUp(lines.join("\n"), { send: false }).catch(showError);
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
  const kickerType = document.querySelector("#graph-detail-kicker-type");
  const metaStrip = document.querySelector("#graph-detail-meta-strip");
  const sourcesEl = document.querySelector("#graph-detail-sources");
  const footer = document.querySelector("#graph-node-footer");
  const copyIdBtn = document.querySelector("#graph-node-copy-id");
  const pinBtn = document.querySelector("#graph-node-pin");
  const openSource = document.querySelector("#graph-node-open-source");
  const askBtn = document.querySelector("#graph-node-ask-ai");
  const openGraphBtn = document.querySelector("#graph-node-open-graph");
  const copyJsonBtn = document.querySelector("#graph-node-copy-json");
  if (!title || !text || !meta || !actions || !neighbors) return;

  if (!node) {
    title.textContent = t("graph.empty.title");
    text.innerHTML = `<p class="mem-empty">${escapeHtml(t("graph.empty.hint"))}</p>`;
    meta.innerHTML = "";
    actions.innerHTML = "";
    neighbors.innerHTML = "";
    if (filesEl) filesEl.innerHTML = "";
    if (metaStrip) metaStrip.innerHTML = "";
    if (sourcesEl) sourcesEl.innerHTML = "";
    if (kickerType) kickerType.textContent = "";
    setSectionHidden("#graph-node-meta-section", true);
    setSectionHidden("#graph-node-linked-section", true);
    setSectionHidden("#graph-node-files-section", true);
    setSectionHidden("#graph-node-advanced-section", true);
    setSectionHidden("#graph-node-footer", true);
    if (copyIdBtn) copyIdBtn.hidden = true;
    if (pinBtn) pinBtn.hidden = true;
    if (openSource) openSource.hidden = true;
    return;
  }

  const synthetic = Boolean(node.metadata && node.metadata.synthetic);
  const pinned = Boolean(node.metadata && (node.metadata.favorite || node.metadata.pinned));
  const wi = node.metadata || {};
  const edgeSource = graphState.allEdges.length ? graphState.allEdges : graphState.edges;
  const nodeSource = graphState.allNodes.length ? graphState.allNodes : graphState.nodes;
  const linked = edgeSource.filter(edge => edge.source === node.id || edge.target === node.id);
  const confidence = graphNodeConfidencePct(node);
  const sourceLinks = memorySourceLinks(node);
  const metaCells = graphNodeMetaCells(node);
  const files = graphNodeRelatedFiles(node);

  title.textContent = node.label;
  if (kickerType) kickerType.textContent = String(node.type || "").toUpperCase();

  text.innerHTML = renderMemoryBodyHtml(node);
  bindMemoryBodyRefs(text, node);

  let codeBadges = "";
  if (node.type === "Symbol") {
    const where = wi.line ? `${wi.path || ""}:${wi.line}` : (wi.path || "");
    const callers = linked.filter(edge => edge.type === "CALLS" && edge.target === node.id).length;
    const callees = linked.filter(edge => edge.type === "CALLS" && edge.source === node.id).length;
    codeBadges =
      `${wi.kind ? `<span class="${graphNodeBadgeClass("code")}">${escapeHtml(wi.kind)}</span>` : ""}` +
      `${where ? `<span class="${graphNodeBadgeClass("code")}">${escapeHtml(where)}</span>` : ""}` +
      `<span class="${graphNodeBadgeClass("code")}">${callers} caller${callers === 1 ? "" : "s"}</span>` +
      `<span class="${graphNodeBadgeClass("code")}">${callees} call${callees === 1 ? "" : "s"}</span>`;
  }

  const pills = [
    `<span class="${graphNodeBadgeClass("type", node.type)}">${escapeHtml(node.type)}</span>`,
    `<span class="graph-node-pill">${escapeHtml(t("graph.node.scopeLabel").replace("{scope}", String(node.scope || "")))}</span>`,
    `<span class="graph-node-pill">${escapeHtml(t("graph.node.edgesCount").replace("{count}", String(linked.length)))}</span>`,
  ];
  if (confidence) pills.push(`<span class="${graphNodeBadgeClass("confidence")}">${confidence}% ${escapeHtml(t("graph.node.confidence"))}</span>`);
  if (pinned) pills.push(`<span class="graph-node-pill">${escapeHtml(t("graph.node.pinned"))}</span>`);
  if (wi.invalid_at || node.superseded) {
    const predecessor = String(wi.superseded_by || "").trim();
    pills.push(`<span class="${graphNodeBadgeClass("status", "superseded")}">superseded</span>`);
    if (predecessor) {
      pills.push(`<button type="button" class="graph-node-pill graph-node-pill-link" data-open-predecessor="${escapeHtml(predecessor)}">← predecessor</button>`);
    }
  } else if (wi.revision_of) {
    pills.push(`<span class="graph-node-pill">revision</span>`);
    pills.push(`<button type="button" class="graph-node-pill graph-node-pill-link" data-open-predecessor="${escapeHtml(String(wi.revision_of))}">← previous</button>`);
  }
  if (wi.work_item_id) {
    pills.push(`<span class="${graphNodeBadgeClass("ado")}">ADO #${escapeHtml(wi.work_item_id)}</span>`);
  }
  const statusLabel = String(wi.work_item_state || node.status || "").trim();
  if (statusLabel) pills.push(`<span class="${graphNodeBadgeClass("status", statusLabel)}">${escapeHtml(statusLabel)}</span>`);
  if (Array.isArray(wi.tags)) {
    for (const tag of wi.tags.slice(0, 4)) {
      if (tag) pills.push(`<span class="graph-node-pill">#${escapeHtml(String(tag))}</span>`);
    }
  }
  meta.innerHTML = pills.join("") + codeBadges;
  meta.querySelectorAll("[data-open-predecessor]").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-open-predecessor");
      if (!id) return;
      const target = (graphState.allNodes.length ? graphState.allNodes : graphState.nodes).find(n => n.id === id);
      if (target) openGraphNodeModal(target);
    });
  });

  const hasMeta = metaCells.length > 0 || sourceLinks.length > 0;
  if (metaStrip) {
    metaStrip.innerHTML = metaCells.length
      ? metaCells.map(cell => `<div class="graph-node-meta-cell"><span class="graph-node-meta-label">${escapeHtml(cell.label)}</span><span class="graph-node-meta-value">${cell.valueHtml}</span></div>`).join("")
      : "";
  }
  if (sourcesEl) {
    sourcesEl.innerHTML = sourceLinks.length
      ? sourceLinks.map(link => `<a class="mem-source-link mem-source-${link.kind}" href="${escapeHtml(link.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.label)} <span aria-hidden="true">↗</span></a>`).join("")
      : "";
  }
  setSectionHidden("#graph-node-meta-section", !hasMeta);

  neighbors.innerHTML = "";
  const byId = new Map(nodeSource.map(item => [item.id, item]));
  let neighborCount = 0;
  for (const edge of linked) {
    const other = byId.get(edge.source === node.id ? edge.target : edge.source);
    if (!other) continue;
    neighborCount += 1;
    const item = document.createElement("article");
    item.className = "result graph-link-card";
    item.tabIndex = 0;
    const label = graphEdgeLabel(edge.type);
    const edgeConfidence = edge.confidence ? Math.round(Number(edge.confidence) * 100) : 0;
    const outbound = edge.source === node.id;
    item.innerHTML =
      `<div class="row"><strong>${escapeHtml(other.label)}</strong><span class="badge">${escapeHtml(label)}</span></div>` +
      `<p class="graph-link-direction" aria-hidden="true">${outbound ? "→" : "←"}</p>` +
      `<span class="badge">${escapeHtml(other.type)}</span>` +
      `<span class="badge">${escapeHtml(other.scope)}</span>` +
      `${edgeConfidence ? `<span class="badge graph-node-pill-ok">${edgeConfidence}% ${escapeHtml(t("graph.node.confidence"))}</span>` : ""}`;
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
  setSectionHidden("#graph-node-linked-section", neighborCount === 0);

  if (filesEl) {
    if (!files.length) {
      filesEl.innerHTML = "";
    } else {
      filesEl.innerHTML = files.map(file => {
        const kind = file.kind === "evidence"
          ? t("graph.node.fileEvidence")
          : file.kind === "source"
            ? t("graph.node.fileSource")
            : t("graph.node.filePath");
        if (file.openable) {
          return `<button type="button" class="graph-file-row" data-graph-file="${escapeHtml(file.path)}"><span class="badge">${escapeHtml(kind)}</span><span class="graph-file-path">${escapeHtml(file.path)}</span></button>`;
        }
        return `<div class="graph-file-row is-static"><span class="badge">${escapeHtml(kind)}</span><span class="graph-file-path">${escapeHtml(file.path)}</span></div>`;
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
  setSectionHidden("#graph-node-files-section", files.length === 0);

  if (copyIdBtn) {
    copyIdBtn.hidden = !node.id;
    copyIdBtn.onclick = () => copyGraphNodeText(node.id, t("graph.node.idCopied"));
  }
  if (pinBtn) {
    pinBtn.hidden = synthetic;
    pinBtn.title = pinned ? t("graph.node.unpin") : t("graph.node.pin");
    pinBtn.setAttribute("aria-pressed", pinned ? "true" : "false");
    pinBtn.classList.toggle("is-active", pinned);
    pinBtn.onclick = async () => {
      await api(`/api/graph/nodes/${node.id}/pin`, { method: "POST", body: "{}" });
      await loadGraph();
    };
  }
  if (openSource) {
    const firstLink = sourceLinks[0];
    if (firstLink) {
      openSource.hidden = false;
      openSource.href = firstLink.href;
    } else {
      openSource.hidden = true;
      openSource.removeAttribute("href");
    }
  }

  if (footer) footer.hidden = false;
  if (askBtn) {
    askBtn.hidden = synthetic;
    askBtn.onclick = () => askAboutGraphNode(node);
  }
  if (openGraphBtn) {
    openGraphBtn.onclick = () => showNodeInMemoryGraph(node.id).catch(showError);
  }
  if (copyJsonBtn) {
    copyJsonBtn.onclick = () => copyGraphNodeText(JSON.stringify(node, null, 2), t("graph.node.jsonCopied"));
  }

  if (synthetic) {
    actions.innerHTML = `<div class="provider-test">${escapeHtml(t("graph.node.synthetic"))}</div>`;
    setSectionHidden("#graph-node-advanced-section", false);
  } else {
    const options = graphNodeOptions(node.id);
    actions.innerHTML =
      `<details class="graph-node-advanced"><summary>${escapeHtml(t("graph.node.advancedEdges"))}</summary>` +
      `<label>${escapeHtml(t("graph.node.targetLabel"))}<select data-graph-target><option value="">${escapeHtml(t("graph.node.selectNode"))}</option>${options}</select></label>` +
      `<label>${escapeHtml(t("graph.node.edgeLabel"))}<select data-graph-edge-type><option>RELATED_TO</option><option>SUPPORTS</option><option>DEPENDS_ON</option><option>IMPLEMENTS</option><option>DOCUMENTED_IN</option></select></label>` +
      `<div class="provider-actions"><button data-graph-edge="${escapeHtml(node.id)}" type="button">${escapeHtml(t("graph.node.createEdge"))}</button>` +
      `<button data-graph-path="${escapeHtml(node.id)}" type="button">${escapeHtml(t("graph.node.explainPath"))}</button>` +
      `<button data-graph-merge="${escapeHtml(node.id)}" type="button">${escapeHtml(t("graph.node.merge"))}</button></div></details>` +
      `<div class="provider-test" data-graph-action-result></div>`;
    setSectionHidden("#graph-node-advanced-section", false);
  }
  bindGraphActions(actions, node);
}

function bindGraphActions(container, node) {
  const result = container.querySelector("[data-graph-action-result]");
  const targetSelect = container.querySelector("[data-graph-target]");
  const edgeType = container.querySelector("[data-graph-edge-type]");
  const setResult = (message, ok = true) => { if (result) { result.className = `provider-test ${ok ? "ok" : "error"}`; result.textContent = message; } };
  const createEdge = container.querySelector("[data-graph-edge]");
  if (createEdge) createEdge.addEventListener("click", async () => {
    if (!targetSelect.value) return setResult(t("graph.node.selectTarget"), false);
    await api("/api/graph/edges", { method: "POST", body: JSON.stringify({ source: node.id, target: targetSelect.value, type: edgeType.value, scope: node.scope }) });
    setResult(t("graph.node.edgeCreated"));
    await loadGraph();
  });
  const explain = container.querySelector("[data-graph-path]");
  if (explain) explain.addEventListener("click", async () => {
    if (!targetSelect.value) return setResult(t("graph.node.selectTarget"), false);
    const payload = await api(`/api/graph/path?source=${encodeURIComponent(node.id)}&target=${encodeURIComponent(targetSelect.value)}`);
    setResult(payload.explanation, payload.found);
  });
  const merge = container.querySelector("[data-graph-merge]");
  if (merge) merge.addEventListener("click", async () => {
    if (!targetSelect.value) return setResult(t("graph.node.selectTarget"), false);
    await api(`/api/graph/nodes/${node.id}/merge`, { method: "POST", body: JSON.stringify({ target_id: targetSelect.value }) });
    setResult(t("graph.node.merged"));
    graphState.selectedId = targetSelect.value;
    await loadGraph();
  });
}


bindGraphNodeModalControls();

export { graphState, loadGraph, openGraphNodeModal, openMemoryNodeCard, showNodeInMemoryGraph, resizeGraphCanvas, resetGraphAutoLayoutFlags, applyGraphAutoLayout, wakeGraphAnimation, syncGraphViewChrome };
