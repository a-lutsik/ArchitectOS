/* Windowed ("virtualized") list renderer — shared by file-tree.js and memory-panel.js.
 *
 * Renders only the rows intersecting the container's scroll viewport (plus
 * `overscan` extra rows on each side), using two spacer divs so the total
 * scroll height matches the full row model. Row heights are measured after
 * render (`estimatedRowHeight` is used until then), so variable-height rows
 * are supported. Framework-free, no dependencies, no top-level DOM access.
 *
 * Usage:
 *   const list = createVirtualList({ container, renderRow, estimatedRowHeight });
 *   list.setRows(rows);  // replace the row model (scroll position preserved)
 *   list.refresh();      // force re-render of the current window
 *   list.destroy();      // detach listeners and clear the container
 *
 * The factory sets `data-vlist-index` on each rendered row root, so a delegated
 * event handler can map a DOM event back to the model: rows[Number(el.dataset.vlistIndex)].
 */

function createVirtualList(options) {
  const {
    container,
    renderRow,
    estimatedRowHeight = 32, // must include `gap`
    overscan = 8,
    gap = 0,                 // visual gap between rows (CSS gap/margin), in px
    holderClass = "",
    holderStyle = "",
  } = options;

  const doc = container.ownerDocument || document;
  container.textContent = ""; // replace any prior content (loading/empty states)
  const topSpacer = doc.createElement("div");
  const holder = doc.createElement("div");
  const bottomSpacer = doc.createElement("div");
  topSpacer.style.cssText = "flex:none;";
  bottomSpacer.style.cssText = "flex:none;";
  holder.style.cssText = `flex:none;${holderStyle}`;
  if (holderClass) holder.className = holderClass;

  let rows = [];
  let heights = []; // measured offsetHeight + gap per row; 0/hole = not measured yet
  let offsets = [0]; // prefix sums over heights, length rows.length + 1
  let totalHeight = 0;
  let estimate = estimatedRowHeight; // updated to the last measured height
  let lastStart = -1;
  let lastEnd = -1;
  let dirty = true;
  let scheduled = false;
  let destroyed = false;

  function recomputeOffsets() {
    offsets = new Array(rows.length + 1);
    offsets[0] = 0;
    for (let i = 0; i < rows.length; i += 1) {
      offsets[i + 1] = offsets[i] + (heights[i] > 0 ? heights[i] : estimate);
    }
    totalHeight = offsets[rows.length] || 0;
  }

  // Index of the row containing offset y (first row whose end edge is > y).
  function rowAt(y) {
    let lo = 0;
    let hi = rows.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (offsets[mid + 1] <= y) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  // The host page may clear the container between renders (e.g. a "Loading..."
  // state written via innerHTML); re-attach our skeleton when that happened.
  function ensureStructure() {
    if (topSpacer.parentNode !== container) container.appendChild(topSpacer);
    if (holder.parentNode !== container) container.appendChild(holder);
    if (bottomSpacer.parentNode !== container) container.appendChild(bottomSpacer);
  }

  function updateSpacers(start, end) {
    topSpacer.style.height = `${offsets[start] || 0}px`;
    bottomSpacer.style.height = `${Math.max(0, totalHeight - (offsets[end] || 0))}px`;
  }

  function render() {
    scheduled = false;
    if (destroyed) return;
    ensureStructure();
    recomputeOffsets();
    const scrollTop = Math.max(0, Number(container.scrollTop) || 0);
    const clientHeight = Number(container.clientHeight) || 0;
    const viewport = clientHeight > 0 ? clientHeight : 600;
    let start = 0;
    let end = 0;
    if (rows.length > 0) {
      start = Math.max(0, rowAt(scrollTop) - overscan);
      end = Math.min(rows.length, rowAt(scrollTop + viewport) + 1 + overscan);
      if (end <= start) end = Math.min(rows.length, start + 1);
    }
    if (!dirty && start === lastStart && end === lastEnd) {
      updateSpacers(start, end);
      return;
    }
    dirty = false;
    lastStart = start;
    lastEnd = end;
    holder.textContent = "";
    for (let i = start; i < end; i += 1) {
      const el = renderRow(rows[i], i);
      if (!el) continue;
      el.dataset.vlistIndex = String(i);
      holder.appendChild(el);
    }
    // Measure what was just rendered and correct the row model if needed.
    let measured = false;
    const rendered = holder.children || [];
    for (let k = 0; k < rendered.length; k += 1) {
      const h = Number(rendered[k].offsetHeight) || 0;
      if (h > 0) {
        const withGap = h + gap;
        if (heights[start + k] !== withGap) {
          heights[start + k] = withGap;
          estimate = withGap;
          measured = true;
        }
      }
    }
    if (measured) recomputeOffsets();
    updateSpacers(start, end);
  }

  function schedule() {
    if (scheduled || destroyed) return;
    scheduled = true;
    const raf = typeof requestAnimationFrame === "function"
      ? requestAnimationFrame
      : (fn) => setTimeout(fn, 16);
    raf(render);
  }

  container.addEventListener("scroll", schedule, { passive: true });
  let resizeObserver = null;
  if (typeof ResizeObserver === "function") {
    resizeObserver = new ResizeObserver(schedule);
    resizeObserver.observe(container);
  }

  return {
    setRows(nextRows) {
      rows = Array.isArray(nextRows) ? nextRows : [];
      heights = new Array(rows.length);
      estimate = estimatedRowHeight;
      dirty = true;
      render();
    },
    refresh() {
      dirty = true;
      render();
    },
    getRowCount() {
      return rows.length;
    },
    destroy() {
      destroyed = true;
      container.removeEventListener("scroll", schedule);
      if (resizeObserver) resizeObserver.disconnect();
      container.textContent = "";
    },
  };
}

export { createVirtualList };
