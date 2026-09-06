/* Obsidian graph physics — the same d3-force model Obsidian used:
   Verlet, many-body charge, Hooke springs on real links, collide, center.
   No packed clouds, no home positions, no hub trees. Clusters form only
   because linked notes pull together and unlinked notes push apart. */

export const FORCE_ALPHA_MIN = 0.001;
const ALPHA_DECAY = 1 - Math.pow(FORCE_ALPHA_MIN, 1 / 300);
const VELOCITY_DECAY = 0.6;
const DIST_MIN2 = 1;

function jiggle() {
  return (Math.random() - 0.5) * 1e-6;
}

export function forceHash01(id) {
  const text = String(id || "");
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return ((hash >>> 0) % 10000) / 10000;
}

export function isGraphForceSettled(alpha, alphaTarget) {
  return !(alphaTarget > 0) && alpha < FORCE_ALPHA_MIN;
}

export function stepGraphForceAlpha(alpha, alphaTarget) {
  return alpha + (alphaTarget - alpha) * ALPHA_DECAY;
}

export function graphForceParams({ nodeSpread = 1, nodeCount = 40 } = {}) {
  const spread = Math.max(1, Math.min(20, Number(nodeSpread) || 1));
  const n = Math.max(8, Number(nodeCount) || 40);
  const scale = Math.sqrt(n / 48);
  return {
    // Obsidian "Link distance" / "Link force" / "Repel force" / "Center force".
    linkDistance: 36 + spread * 16,
    linkStrength: 1,
    inferredStrength: 0.18,
    charge: -(58 + spread * 14) * Math.min(2.4, 0.7 + scale),
    chargeDistanceMax: 560 + spread * 28,
    centerStrength: 0.032 + 0.008 * Math.min(spread, 8),
    collidePad: 0.15,
  };
}

export function seedForcePositions(particles, { width = 800, height = 600, force = false } = {}) {
  const list = Array.isArray(particles) ? particles : [];
  if (!list.length) return;
  list.forEach(particle => {
    if (!force && Number.isFinite(particle.x) && Number.isFinite(particle.y)) return;
    const u = forceHash01(particle.id);
    const v = forceHash01(`${particle.id}:y`);
    particle.x = width * (0.12 + 0.76 * u);
    particle.y = height * (0.12 + 0.76 * v);
    particle.vx = 0;
    particle.vy = 0;
    particle.fx = undefined;
    particle.fy = undefined;
  });
}

function inferredEdge(edge) {
  return String(edge?.provenance || "").toUpperCase() === "INFERRED";
}

function applyCharge(particles, alpha, charge, distanceMax, skipId) {
  const n = particles.length;
  const max2 = distanceMax * distanceMax;
  if (n <= 160) {
    for (let i = 0; i < n; i += 1) {
      const a = particles[i];
      for (let j = i + 1; j < n; j += 1) {
        const b = particles[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let dist2 = dx * dx + dy * dy;
        if (dist2 > max2) continue;
        if (dist2 < DIST_MIN2) {
          dx = jiggle();
          dy = jiggle();
          dist2 = dx * dx + dy * dy + DIST_MIN2;
        }
        const w = charge * alpha / dist2;
        if (a.id !== skipId) {
          a.vx += dx * w;
          a.vy += dy * w;
        }
        if (b.id !== skipId) {
          b.vx -= dx * w;
          b.vy -= dy * w;
        }
      }
    }
    return;
  }
  const cell = Math.max(28, distanceMax / 4);
  const buckets = new Map();
  for (let i = 0; i < n; i += 1) {
    const p = particles[i];
    const key = `${Math.floor(p.x / cell)},${Math.floor(p.y / cell)}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(p);
  }
  for (const particle of particles) {
    if (particle.id === skipId) continue;
    const cx = Math.floor(particle.x / cell);
    const cy = Math.floor(particle.y / cell);
    for (let ox = -2; ox <= 2; ox += 1) {
      for (let oy = -2; oy <= 2; oy += 1) {
        const others = buckets.get(`${cx + ox},${cy + oy}`);
        if (!others) continue;
        for (const other of others) {
          if (other.id === particle.id) continue;
          let dx = other.x - particle.x;
          let dy = other.y - particle.y;
          let dist2 = dx * dx + dy * dy;
          if (dist2 > max2) continue;
          if (dist2 < DIST_MIN2) {
            dx = jiggle();
            dy = jiggle();
            dist2 = dx * dx + dy * dy + DIST_MIN2;
          }
          const w = charge * alpha / dist2;
          particle.vx += dx * w;
          particle.vy += dy * w;
        }
      }
    }
  }
}

function degreeCounts(particles, edges) {
  const count = new Map();
  for (const particle of particles) count.set(particle.id, 0);
  for (const edge of edges) {
    if (!count.has(edge.source) || !count.has(edge.target) || edge.source === edge.target) continue;
    count.set(edge.source, count.get(edge.source) + 1);
    count.set(edge.target, count.get(edge.target) + 1);
  }
  return count;
}

function applyLinks(particles, edges, alpha, params, skipId) {
  const byId = new Map(particles.map(item => [item.id, item]));
  const counts = degreeCounts(particles, edges);
  const rest = params.linkDistance;
  for (const edge of edges) {
    const a = byId.get(edge.source);
    const b = byId.get(edge.target);
    if (!a || !b || a === b) continue;
    const ca = Math.max(1, counts.get(a.id) || 1);
    const cb = Math.max(1, counts.get(b.id) || 1);
    const base = inferredEdge(edge) ? params.inferredStrength : params.linkStrength;
    const strength = base / Math.min(ca, cb);
    if (!(strength > 0)) continue;
    let dx = (b.x + b.vx) - (a.x + a.vx);
    let dy = (b.y + b.vy) - (a.y + a.vy);
    let dist = Math.hypot(dx, dy);
    if (dist < 1e-6) {
      dx = jiggle();
      dy = jiggle();
      dist = Math.hypot(dx, dy);
    }
    const l = ((dist - rest) / dist) * strength * alpha;
    const bias = ca / (ca + cb);
    if (b.id !== skipId) {
      b.vx -= dx * l * bias;
      b.vy -= dy * l * bias;
    }
    if (a.id !== skipId) {
      a.vx += dx * l * (1 - bias);
      a.vy += dy * l * (1 - bias);
    }
  }
}

function applyCenter(particles, alpha, strength, cx, cy, skipId) {
  if (!(strength > 0)) return;
  const k = strength * alpha;
  for (const particle of particles) {
    if (particle.id === skipId) continue;
    particle.vx += (cx - particle.x) * k;
    particle.vy += (cy - particle.y) * k;
  }
}

function applyCollide(particles, alpha, pad, radiusOf, skipId) {
  const n = particles.length;
  if (n < 2) return;
  let maxR = pad;
  const radii = new Map();
  for (const particle of particles) {
    const r = radiusOf(particle) + pad;
    radii.set(particle.id, r);
    if (r > maxR) maxR = r;
  }
  const cell = Math.max(8, maxR * 2);
  const buckets = new Map();
  for (const particle of particles) {
    const key = `${Math.floor(particle.x / cell)},${Math.floor(particle.y / cell)}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(particle);
  }
  const k = alpha;
  for (const particle of particles) {
    if (particle.id === skipId) continue;
    const ra = radii.get(particle.id);
    const cx = Math.floor(particle.x / cell);
    const cy = Math.floor(particle.y / cell);
    for (let ox = -1; ox <= 1; ox += 1) {
      for (let oy = -1; oy <= 1; oy += 1) {
        const others = buckets.get(`${cx + ox},${cy + oy}`);
        if (!others) continue;
        for (const other of others) {
          if (other.id <= particle.id) continue;
          let dx = other.x - particle.x;
          let dy = other.y - particle.y;
          let dist = Math.hypot(dx, dy);
          const need = ra + radii.get(other.id);
          if (dist >= need) continue;
          if (dist < 1e-6) {
            dx = 1;
            dy = 0;
            dist = 1;
          }
          const push = (need - dist) * 0.35 * k;
          const ux = dx / dist;
          const uy = dy / dist;
          particle.x -= ux * push;
          particle.y -= uy * push;
          if (other.id !== skipId) {
            other.x += ux * push;
            other.y += uy * push;
          }
        }
      }
    }
  }
}

function recenterBarycenter(particles, cx, cy) {
  const n = particles.length;
  if (!n) return;
  let sx = 0;
  let sy = 0;
  for (const particle of particles) {
    sx += particle.x;
    sy += particle.y;
  }
  const dx = sx / n - cx;
  const dy = sy / n - cy;
  if (!dx && !dy) return;
  for (const particle of particles) {
    particle.x -= dx;
    particle.y -= dy;
  }
}

export function stepForceSimulation(particles, edges, options = {}) {
  const list = particles || [];
  const n = list.length;
  if (!n) return { alpha: 0, settled: true };
  const {
    alpha = 1,
    alphaTarget = 0,
    draggingId = "",
    centerX = 0,
    centerY = 0,
    params = graphForceParams({ nodeCount: n }),
    radiusOf = () => 4,
  } = options;
  const next = stepGraphForceAlpha(alpha, alphaTarget);
  applyCharge(list, next, params.charge, params.chargeDistanceMax, draggingId);
  applyLinks(list, edges || [], next, params, draggingId);
  applyCenter(list, next, params.centerStrength, centerX, centerY, draggingId);
  for (const particle of list) {
    if (particle.id === draggingId) {
      if (Number.isFinite(particle.fx)) particle.x = particle.fx;
      if (Number.isFinite(particle.fy)) particle.y = particle.fy;
      particle.vx = 0;
      particle.vy = 0;
      continue;
    }
    particle.vx *= VELOCITY_DECAY;
    particle.vy *= VELOCITY_DECAY;
    particle.x += particle.vx;
    particle.y += particle.vy;
  }
  applyCollide(list, next, params.collidePad, radiusOf, draggingId);
  recenterBarycenter(list, centerX, centerY);
  if (draggingId) {
    const pinned = list.find(item => item.id === draggingId);
    if (pinned) {
      if (Number.isFinite(pinned.fx)) pinned.x = pinned.fx;
      if (Number.isFinite(pinned.fy)) pinned.y = pinned.fy;
      pinned.vx = 0;
      pinned.vy = 0;
    }
  }
  return { alpha: next, settled: isGraphForceSettled(next, alphaTarget) };
}
