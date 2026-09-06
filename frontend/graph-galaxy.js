/* 3D galaxy projection for the memory graph.
   Canvas 2D + perspective camera — no WebGL/Three.js (frontend has no bundler).
   Physics matches the 2D Obsidian/d3-force model (charge + links + center). */

import { forceHash01, graphForceParams } from "./graph-force.js";

export const GRAPH_VIEW_MAP = "map";
export const GRAPH_VIEW_GALAXY = "galaxy";
export const GRAPH_VIEW_STORAGE_KEY = "architectos.graph.view";

const PITCH_LIMIT = 1.18;
const FOCAL_RATIO = 0.62;
/** Side/bottom margin as a fraction of the short canvas axis. */
const FIT_MARGIN = 0.018;
/** Extra top margin so the cloud sits under the floating Map/Galaxy toolbar. */
const FIT_TOP_MARGIN = 0.052;
const MIN_DISTANCE = 16;
const MAX_DISTANCE = 8000;

export function readSavedGraphView() {
  try {
    return localStorage.getItem(GRAPH_VIEW_STORAGE_KEY) === GRAPH_VIEW_GALAXY
      ? GRAPH_VIEW_GALAXY
      : GRAPH_VIEW_MAP;
  } catch {
    return GRAPH_VIEW_MAP;
  }
}

export function persistGraphView(view) {
  try {
    localStorage.setItem(
      GRAPH_VIEW_STORAGE_KEY,
      view === GRAPH_VIEW_GALAXY ? GRAPH_VIEW_GALAXY : GRAPH_VIEW_MAP,
    );
  } catch {
    /* ignore quota / private mode */
  }
}

export function createGalaxyCamera() {
  return {
    yaw: 0.55,
    pitch: 0.28,
    distance: 520,
    targetX: 0,
    targetY: 0,
    targetZ: 0,
  };
}

export function prefersGraphReducedMotion() {
  try {
    return Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
  } catch {
    return false;
  }
}

export function seedGalaxyPositions(particles, { force = false } = {}) {
  const list = Array.isArray(particles) ? particles : [];
  if (!list.length) return;
  const reach = 520 + Math.sqrt(list.length) * 42;
  for (const particle of list) {
    if (!force && Number.isFinite(particle.gx) && Number.isFinite(particle.gy) && Number.isFinite(particle.gz)) continue;
    const u = forceHash01(particle.id);
    const v = forceHash01(`${particle.id}:y`);
    const w = forceHash01(`${particle.id}:z`);
    particle.gx = (u - 0.5) * 2 * reach;
    particle.gy = (v - 0.5) * 2 * reach * 0.42;
    particle.gz = (w - 0.5) * 2 * reach;
    particle.gvx = 0;
    particle.gvy = 0;
    particle.gvz = 0;
  }
}

export function scaleGalaxyPositions(particles, factor) {
  const list = Array.isArray(particles) ? particles : [];
  const scale = Number(factor);
  if (!list.length || !Number.isFinite(scale) || scale === 1 || scale <= 0) return;
  let cx = 0;
  let cy = 0;
  let cz = 0;
  for (const particle of list) {
    cx += particle.gx;
    cy += particle.gy;
    cz += particle.gz;
  }
  const n = list.length;
  cx /= n;
  cy /= n;
  cz /= n;
  for (const particle of list) {
    particle.gx = cx + (particle.gx - cx) * scale;
    particle.gy = cy + (particle.gy - cy) * scale;
    particle.gz = cz + (particle.gz - cz) * scale;
    particle.gvx = 0;
    particle.gvy = 0;
    particle.gvz = 0;
  }
}

export function galaxyHasPositions(particles) {
  return Boolean(particles?.length) && particles.every(item => (
    Number.isFinite(item.gx) && Number.isFinite(item.gy) && Number.isFinite(item.gz)
  ));
}

function cameraTrig(camera) {
  return {
    cy: Math.cos(camera.yaw),
    sy: Math.sin(camera.yaw),
    cp: Math.cos(camera.pitch),
    sp: Math.sin(camera.pitch),
  };
}

export function cameraAxes(camera) {
  const { cy, sy, cp, sp } = cameraTrig(camera);
  return {
    right: { x: cy, y: 0, z: -sy },
    up: { x: sy * sp, y: cp, z: cy * sp },
  };
}

export function projectGalaxyPoint(x, y, z, camera, width, height) {
  const { cy, sy, cp, sp } = cameraTrig(camera);
  const dx = x - camera.targetX;
  const dy = y - camera.targetY;
  const dz = z - camera.targetZ;
  const x1 = dx * cy - dz * sy;
  const z1 = dx * sy + dz * cy;
  const y2 = dy * cp - z1 * sp;
  const z2 = dy * sp + z1 * cp;
  const focal = Math.min(width, height) * FOCAL_RATIO;
  const depth = z2 + camera.distance;
  if (depth < 8) {
    return { sx: width / 2, sy: height / 2, scale: 0.02, depth: 8, behind: true };
  }
  const scale = focal / depth;
  return {
    sx: width / 2 + x1 * scale,
    sy: height / 2 + y2 * scale,
    scale,
    depth,
    behind: false,
  };
}

export function orbitGalaxyCamera(camera, dx, dy) {
  camera.yaw += dx * 0.0055;
  camera.pitch = Math.max(-PITCH_LIMIT, Math.min(PITCH_LIMIT, camera.pitch + dy * 0.0055));
}

export function panGalaxyCamera(camera, dx, dy, width, height) {
  const focal = Math.min(width, height) * FOCAL_RATIO;
  const k = camera.distance / Math.max(focal, 1);
  const { cy, sy, cp, sp } = cameraTrig(camera);
  const right = { x: cy, y: 0, z: -sy };
  const screenDown = { x: -sy * sp, y: cp, z: -cy * sp };
  camera.targetX -= right.x * dx * k + screenDown.x * dy * k;
  camera.targetY -= right.y * dx * k + screenDown.y * dy * k;
  camera.targetZ -= right.z * dx * k + screenDown.z * dy * k;
}

export function dollyGalaxyCamera(camera, deltaY) {
  const factor = deltaY > 0 ? 1.09 : 1 / 1.09;
  camera.distance = Math.max(MIN_DISTANCE, Math.min(MAX_DISTANCE, camera.distance * factor));
}

function galaxyViewFill(width, height) {
  const short = Math.min(width, height);
  const pad = Math.max(14, short * FIT_MARGIN);
  const padTop = Math.max(pad + 36, height * FIT_TOP_MARGIN);
  const availW = Math.max(64, width - pad * 2);
  const availH = Math.max(64, height - padTop - pad);
  return {
    focal: short * FOCAL_RATIO,
    fill: Math.min(availW, availH) / 2,
  };
}

function galaxyCloudStats(particles) {
  const n = particles.length;
  let cx = 0;
  let cy = 0;
  let cz = 0;
  for (const particle of particles) {
    cx += particle.gx;
    cy += particle.gy;
    cz += particle.gz;
  }
  cx /= n;
  cy /= n;
  cz /= n;
  const radii = new Array(n);
  for (let i = 0; i < n; i += 1) {
    const particle = particles[i];
    radii[i] = Math.hypot(particle.gx - cx, particle.gy - cy, particle.gz - cz);
  }
  radii.sort((a, b) => a - b);
  const idx = Math.min(n - 1, Math.max(0, Math.ceil(n * 0.94) - 1));
  return { cx, cy, cz, radius: Math.max(8, radii[idx]) };
}

function desiredGalaxyDistance(radius, width, height) {
  const { focal, fill } = galaxyViewFill(width, height);
  // World radius → camera distance. The 0.88 pack fills the short axis without
  // pulling front nodes through the near plane (that made zoom pulse).
  return Math.max(MIN_DISTANCE, Math.min(MAX_DISTANCE, radius * (focal / Math.max(fill, 1) + 0.16) * 0.88));
}

export function fitGalaxyCamera(particles, camera, {
  width = 800,
  height = 600,
  resetOrientation = false,
  ease = false,
} = {}) {
  if (!particles?.length) {
    camera.distance = 280;
    camera.targetX = 0;
    camera.targetY = 0;
    camera.targetZ = 0;
    return;
  }
  const stats = galaxyCloudStats(particles);
  const desired = desiredGalaxyDistance(stats.radius, width, height);
  if (resetOrientation) {
    camera.yaw = 0.55;
    camera.pitch = 0.28;
  }
  if (!ease) {
    camera.targetX = stats.cx;
    camera.targetY = stats.cy;
    camera.targetZ = stats.cz;
    camera.distance = desired;
    return;
  }
  const follow = 0.08;
  camera.targetX += (stats.cx - camera.targetX) * follow;
  camera.targetY += (stats.cy - camera.targetY) * follow;
  camera.targetZ += (stats.cz - camera.targetZ) * follow;
  const ratio = desired / Math.max(camera.distance, 1);
  if (Math.abs(ratio - 1) < 0.08) return;
  camera.distance = Math.max(
    MIN_DISTANCE,
    Math.min(MAX_DISTANCE, camera.distance + (desired - camera.distance) * follow),
  );
}

export function dragGalaxyParticle(particle, camera, dx, dy, width, height) {
  if (!particle) return;
  const proj = projectGalaxyPoint(particle.gx, particle.gy, particle.gz, camera, width, height);
  const focal = Math.min(width, height) * FOCAL_RATIO;
  const k = proj.depth / Math.max(focal, 1);
  const axes = cameraAxes(camera);
  particle.gx += axes.right.x * dx * k - axes.up.x * dy * k;
  particle.gy += axes.right.y * dx * k - axes.up.y * dy * k;
  particle.gz += axes.right.z * dx * k - axes.up.z * dy * k;
  particle.gvx = 0;
  particle.gvy = 0;
  particle.gvz = 0;
}

export function stepGalaxyPhysics(particles, edges, { draggingId = "", live = true } = {}) {
  const list = particles || [];
  const n = list.length;
  if (!n) return;
  const skip = draggingId || "";
  const params = graphForceParams({ nodeCount: n });
  const alpha = live ? 0.28 : 0.8;
  const links = edges || [];
  const byId = new Map(list.map(item => [item.id, item]));
  const count = new Map();
  for (const particle of list) count.set(particle.id, 0);
  for (const edge of links) {
    if (!count.has(edge.source) || !count.has(edge.target) || edge.source === edge.target) continue;
    count.set(edge.source, count.get(edge.source) + 1);
    count.set(edge.target, count.get(edge.target) + 1);
  }

  const chargeCap = n <= 420 ? n : 280;
  const charge = params.charge * 0.85;
  for (let i = 0; i < chargeCap; i += 1) {
    const a = list[i];
    for (let j = i + 1; j < chargeCap; j += 1) {
      const b = list[j];
      let dx = b.gx - a.gx;
      let dy = b.gy - a.gy;
      let dz = b.gz - a.gz;
      let dist2 = dx * dx + dy * dy + dz * dz;
      if (dist2 > 220000) continue;
      if (dist2 < 16) dist2 = 16;
      const mag = charge * alpha / dist2;
      const dist = Math.sqrt(dist2);
      const fx = (dx / dist) * mag;
      const fy = (dy / dist) * mag;
      const fz = (dz / dist) * mag;
      if (a.id !== skip) {
        a.gvx += fx;
        a.gvy += fy;
        a.gvz += fz;
      }
      if (b.id !== skip) {
        b.gvx -= fx;
        b.gvy -= fy;
        b.gvz -= fz;
      }
    }
  }

  const rest = params.linkDistance * 5.4;
  for (const edge of links) {
    const a = byId.get(edge.source);
    const b = byId.get(edge.target);
    if (!a || !b || a === b) continue;
    const ca = Math.max(1, count.get(a.id) || 1);
    const cb = Math.max(1, count.get(b.id) || 1);
    const base = String(edge.provenance || "").toUpperCase() === "INFERRED" ? params.inferredStrength : params.linkStrength;
    const strength = base / Math.min(ca, cb);
    let dx = b.gx - a.gx;
    let dy = b.gy - a.gy;
    let dz = b.gz - a.gz;
    let dist = Math.hypot(dx, dy, dz);
    if (dist < 1e-6) {
      dx = 1;
      dy = 0;
      dz = 0;
      dist = 1;
    }
    const l = ((dist - rest) / dist) * strength * alpha;
    const bias = ca / (ca + cb);
    if (b.id !== skip) {
      b.gvx -= dx * l * bias;
      b.gvy -= dy * l * bias;
      b.gvz -= dz * l * bias;
    }
    if (a.id !== skip) {
      a.gvx += dx * l * (1 - bias);
      a.gvy += dy * l * (1 - bias);
      a.gvz += dz * l * (1 - bias);
    }
  }

  const centerK = params.centerStrength * alpha * 0.012;
  const damp = live ? 0.82 : 0.55;
  for (const particle of list) {
    if (particle.id === skip) {
      particle.gvx = 0;
      particle.gvy = 0;
      particle.gvz = 0;
      continue;
    }
    particle.gvx += -particle.gx * centerK;
    particle.gvy += -particle.gy * centerK;
    particle.gvz += -particle.gz * centerK;
    particle.gx += particle.gvx;
    particle.gy += particle.gvy;
    particle.gz += particle.gvz;
    particle.gvx *= damp;
    particle.gvy *= damp;
    particle.gvz *= damp;
  }

  if (!live) return;
  let cx = 0;
  let cy = 0;
  let cz = 0;
  for (const particle of list) {
    cx += particle.gx;
    cy += particle.gy;
    cz += particle.gz;
  }
  cx /= n;
  cy /= n;
  cz /= n;
  const angle = 0.0009;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  for (const particle of list) {
    if (particle.id === skip) continue;
    particle.gx -= cx;
    particle.gy -= cy;
    particle.gz -= cz;
    const dx = particle.gx;
    const dz = particle.gz;
    particle.gx = dx * cos - dz * sin;
    particle.gz = dx * sin + dz * cos;
  }
}

export function parseGraphColor(color) {
  const raw = String(color || "#94a3b8").trim();
  const hexMatch = raw.match(/^#([0-9a-f]{3,8})$/i);
  if (hexMatch) {
    let hex = hexMatch[1];
    if (hex.length === 3 || hex.length === 4) hex = hex.split("").map(ch => ch + ch).join("");
    const n = parseInt(hex.slice(0, 6), 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  const rgb = raw.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (rgb) return { r: Number(rgb[1]), g: Number(rgb[2]), b: Number(rgb[3]) };
  return { r: 148, g: 163, b: 184 };
}

function rgba(rgb, alpha) {
  return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})`;
}

function mixRgb(a, b) {
  return {
    r: Math.round((a.r + b.r) / 2),
    g: Math.round((a.g + b.g) / 2),
    b: Math.round((a.b + b.b) / 2),
  };
}

function nodeDegree(node) {
  const value = Number(node?.degree);
  return Number.isFinite(value) ? value : 0;
}

export function galaxyNodeRadius(particle, nodeSize, scale) {
  const size = Math.max(3, Number(nodeSize) || 5);
  const degree = nodeDegree(particle.node);
  const hub = particle.node?.type === "Project" ? 1.4 : 1;
  const depth = Math.max(0.4, Math.min(1.85, Number(scale) || 1));
  const world = size * hub * (0.55 + Math.log1p(degree) * 0.18) * depth * 0.85;
  return Math.max(size * 0.5, Math.min(28, world));
}

function neighborIds(selectedId, edges) {
  const ids = new Set([selectedId]);
  if (!selectedId) return ids;
  for (const edge of edges || []) {
    if (edge.source === selectedId) ids.add(edge.target);
    if (edge.target === selectedId) ids.add(edge.source);
  }
  return ids;
}

export function hitGalaxyNode(screenX, screenY, particles, camera, width, height, nodeSize) {
  let best = null;
  let bestDist = Infinity;
  for (const particle of particles || []) {
    const proj = projectGalaxyPoint(particle.gx, particle.gy, particle.gz, camera, width, height);
    if (proj.behind) continue;
    const radius = galaxyNodeRadius(particle, nodeSize, proj.scale);
    const dist = Math.hypot(screenX - proj.sx, screenY - proj.sy);
    if (dist <= radius + 8 && dist < bestDist) {
      best = particle;
      bestDist = dist;
    }
  }
  return best;
}

function drawGalaxyBackground(ctx, canvas) {
  const { width, height } = canvas;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const glow = ctx.createRadialGradient(width * 0.5, height * 0.46, 12, width * 0.5, height * 0.5, Math.max(width, height) * 0.72);
  glow.addColorStop(0, "#14161f");
  glow.addColorStop(0.42, "#0a0b10");
  glow.addColorStop(1, "#05060a");
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, width, height);
}

export function drawGalaxy(ctx, canvas, options) {
  const {
    particles = [],
    edges = [],
    camera,
    selectedId = "",
    hoverId = "",
    nodeColor,
    nodeSize = 5,
    labelText,
    alwaysLabel,
    reducedMotion = false,
  } = options;
  const { width, height } = canvas;
  drawGalaxyBackground(ctx, canvas);
  if (!particles.length || !camera) return;

  const byId = new Map(particles.map(item => [item.id, item]));
  const projected = new Map();
  for (const particle of particles) {
    projected.set(particle.id, projectGalaxyPoint(particle.gx, particle.gy, particle.gz, camera, width, height));
  }
  const linked = neighborIds(hoverId, edges);
  const isolating = Boolean(hoverId);

  const edgeCap = 2800;
  const drawableEdges = edges.length > edgeCap
    ? edges.filter(edge => (
      String(edge.provenance || "").toUpperCase() !== "INFERRED"
      || edge.source === selectedId
      || edge.target === selectedId
    )).slice(0, edgeCap)
    : edges;

  ctx.save();
  ctx.lineCap = "round";
  for (const edge of drawableEdges) {
    const a = projected.get(edge.source);
    const b = projected.get(edge.target);
    const pa = byId.get(edge.source);
    const pb = byId.get(edge.target);
    if (!a || !b || a.behind || b.behind || !pa || !pb) continue;
    const active = (selectedId && (edge.source === selectedId || edge.target === selectedId))
      || (hoverId && (edge.source === hoverId || edge.target === hoverId));
    const inferred = String(edge.provenance || "").toUpperCase() === "INFERRED";
    const rgb = mixRgb(parseGraphColor(nodeColor(pa.node)), parseGraphColor(nodeColor(pb.node)));
    const depthFade = Math.max(0.2, Math.min(1, 2.2 / ((a.depth + b.depth) / Math.max(camera.distance, 1))));
    let alpha = active ? 0.94 : inferred ? 0.13 * depthFade : 0.36 * depthFade;
    if (isolating && !active) alpha *= 0.12;
    ctx.beginPath();
    ctx.moveTo(a.sx, a.sy);
    ctx.lineTo(b.sx, b.sy);
    ctx.strokeStyle = rgba(rgb, alpha * 0.45);
    ctx.lineWidth = active ? 4.2 : inferred ? 1.1 : 2.1;
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(a.sx, a.sy);
    ctx.lineTo(b.sx, b.sy);
    ctx.strokeStyle = rgba(rgb, alpha);
    ctx.lineWidth = active ? 1.6 : inferred ? 0.6 : 0.9;
    ctx.stroke();
  }
  ctx.restore();

  const ordered = particles
    .map(particle => ({ particle, proj: projected.get(particle.id) }))
    .filter(item => item.proj && !item.proj.behind)
    .sort((a, b) => b.proj.depth - a.proj.depth);

  for (const { particle, proj } of ordered) {
    const dim = isolating && !linked.has(particle.id);
    const rgb = parseGraphColor(nodeColor(particle.node));
    const radius = galaxyNodeRadius(particle, nodeSize, proj.scale);
    const active = particle.id === selectedId;
    const hover = particle.id === hoverId;
    const alpha = dim ? 0.2 : 1;
    ctx.save();
    ctx.globalAlpha = alpha;
    if (!reducedMotion && !dim) {
      const bloom = ctx.createRadialGradient(proj.sx, proj.sy, 0, proj.sx, proj.sy, radius * (active || hover ? 3.6 : 2.6));
      bloom.addColorStop(0, rgba(rgb, active ? 0.55 : 0.32));
      bloom.addColorStop(1, rgba(rgb, 0));
      ctx.fillStyle = bloom;
      ctx.beginPath();
      ctx.arc(proj.sx, proj.sy, radius * (active || hover ? 3.6 : 2.6), 0, Math.PI * 2);
      ctx.fill();
    }
    const fill = ctx.createRadialGradient(proj.sx - radius * 0.28, proj.sy - radius * 0.32, radius * 0.1, proj.sx, proj.sy, radius);
    fill.addColorStop(0, rgba({ r: Math.min(255, rgb.r + 48), g: Math.min(255, rgb.g + 48), b: Math.min(255, rgb.b + 48) }, 1));
    fill.addColorStop(1, rgba(rgb, 1));
    ctx.beginPath();
    ctx.arc(proj.sx, proj.sy, radius + (hover ? 1.1 : 0), 0, Math.PI * 2);
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.lineWidth = active ? 1.8 : 0.9;
    ctx.strokeStyle = active ? "rgba(250,250,250,0.92)" : "rgba(250,250,250,0.28)";
    ctx.stroke();
    ctx.restore();
  }

  const fontPx = Math.max(11, 12 * Math.min(window.devicePixelRatio || 1, 2) * 0.7);
  ctx.save();
  ctx.font = `550 ${fontPx}px "Segoe UI", ui-sans-serif, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (const { particle, proj } of ordered) {
    const show = particle.id === selectedId
      || particle.id === hoverId
      || alwaysLabel(particle.node);
    if (!show) continue;
    if (isolating && !linked.has(particle.id) && particle.id !== hoverId) continue;
    const text = labelText(particle.node);
    if (!text) continue;
    const radius = galaxyNodeRadius(particle, nodeSize, proj.scale);
    const x = proj.sx;
    const y = proj.sy + radius + 5;
    ctx.lineWidth = 3.5;
    ctx.strokeStyle = "rgba(5,6,10,0.78)";
    ctx.fillStyle = "rgba(250,250,252,0.94)";
    ctx.strokeText(text, x, y);
    ctx.fillText(text, x, y);
  }
  ctx.restore();
}

export function canvasPointFromEvent(event, canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratioX = canvas.width / Math.max(rect.width, 1);
  const ratioY = canvas.height / Math.max(rect.height, 1);
  return {
    x: (event.clientX - rect.left) * ratioX,
    y: (event.clientY - rect.top) * ratioY,
  };
}
