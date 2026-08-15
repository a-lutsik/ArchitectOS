/* Windowed ("virtualized") list renderer — shared by file-tree.js and memory-panel.js.
 *
 * Renders only the rows intersecting the container's scroll viewport (plus
 * `overscan` extra rows on each side), using two spacer divs so the total
 * scroll height matches the full row model. Row heights are measured after
 * render (`estimatedRowHeight` is used until then), so variable-height rows
 * are supported. Framework-free, no dependencies, no top-level DOM access.
 *
 * Keyboard: ArrowUp/Down, Home/End, PageUp/PageDown move the active row;
 * Enter/Space activate it when `onActivate` is provided. The container gets
 * `role="listbox"` (or the caller's `role`) and `tabindex="0"`.
 *
 * Usage:
 *   const list = createVirtualList({ container, renderRow, estimatedRowHeight, onActivate });
 *   list.setRows(rows);
 *   list.refresh();
 *   list.destroy();
 *
 * The factory sets `data-vlist-index` on each rendered row root, so a delegated
 * event handler can map a DOM event back to the model: rows[Number(el.dataset.vlistIndex)].
 */

function createVirtualList(options) {
  const {
    container,
    renderRow,
    estimatedRowHeight = 32,
    overscan = 8,
    gap = 0,
    holderClass = "",
    holderStyle = "",
    role = "listbox",
    onActivate = null,
  } = options;

  const doc = container.ownerDocument || document;
  container.textContent = "";
  if (!container.getAttribute("role")) container.setAttribute("role", role);
  if (!container.hasAttribute("tabindex")) container.setAttribute("tabindex", "0");
  const topSpacer = doc.createElement("div");
  const holder = doc.createElement("div");
  const bottomSpacer = doc.createElement("div");
  topSpacer.style.cssText = "flex:none;";
  bottomSpacer.style.cssText = "flex:none;";
  topSpacer.setAttribute("aria-hidden", "true");
  bottomSpacer.setAttribute("aria-hidden", "true");
  holder.style.cssText = `flex:none;${holderStyle}`;
  if (holderClass) holder.className = holderClass;

  let rows = [];
  let heights = [];
  let offsets = [0];
  let totalHeight = 0;
  let estimate = estimatedRowHeight;
  let lastStart = -1;
  let lastEnd = -1;
  let dirty = true;
  let scheduled = false;
  let destroyed = false;
  let activeIndex = -1;

  function recomputeOffsets() {
    offsets = new Array(rows.length + 1);
    offsets[0] = 0;
    for (let i = 0; i < rows.length; i += 1) {
      offsets[i + 1] = offsets[i] + (heights[i] > 0 ? heights[i] : estimate);
    }
    totalHeight = offsets[rows.length] || 0;
  }

  function rowAt(y) {
    let lo = 0;
    let hi = rows.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (offsets[mid + 1] <= y) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  function ensureStructure() {
    if (topSpacer.parentNode !== container) container.appendChild(topSpacer);
    if (holder.parentNode !== container) container.appendChild(holder);
    if (bottomSpacer.parentNode !== container) container.appendChild(bottomSpacer);
  }

  function updateSpacers(start, end) {
    topSpacer.style.height = `${offsets[start] || 0}px`;
    bottomSpacer.style.height = `${Math.max(0, totalHeight - (offsets[end] || 0))}px`;
  }

  function scrollActiveIntoView() {
    if (activeIndex < 0 || activeIndex >= rows.length) return;
    const top = offsets[activeIndex] || 0;
    const bottom = offsets[activeIndex + 1] || (top + estimate);
    const viewTop = container.scrollTop;
    const viewBottom = viewTop + (container.clientHeight || 0);
    if (top < viewTop) container.scrollTop = top;
    else if (bottom > viewBottom) container.scrollTop = Math.max(0, bottom - (container.clientHeight || 0));
  }

  function setActiveIndex(next, { scroll = true } = {}) {
    if (rows.length === 0) {
      activeIndex = -1;
      container.removeAttribute("aria-activedescendant");
      dirty = true;
      schedule();
      return;
    }
    activeIndex = Math.max(0, Math.min(rows.length - 1, next));
    dirty = true;
    if (scroll) scrollActiveIntoView();
    schedule();
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
      el.id = el.id || `vlist-row-${i}`;
      if (!el.getAttribute("role")) el.setAttribute("role", role === "listbox" ? "option" : "listitem");
      el.setAttribute("aria-selected", i === activeIndex ? "true" : "false");
      if (i === activeIndex) {
        el.classList.add("vlist-active");
        container.setAttribute("aria-activedescendant", el.id);
      } else {
        el.classList.remove("vlist-active");
      }
      holder.appendChild(el);
    }
    if (activeIndex < 0) container.removeAttribute("aria-activedescendant");
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

  function onKeyDown(event) {
    if (destroyed || rows.length === 0) return;
    const page = Math.max(1, Math.floor((container.clientHeight || estimate) / estimate) - 1);
    let next = activeIndex;
    switch (event.key) {
      case "ArrowDown":
        next = activeIndex < 0 ? 0 : activeIndex + 1;
        break;
      case "ArrowUp":
        next = activeIndex < 0 ? 0 : activeIndex - 1;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = rows.length - 1;
        break;
      case "PageDown":
        next = (activeIndex < 0 ? 0 : activeIndex) + page;
        break;
      case "PageUp":
        next = (activeIndex < 0 ? 0 : activeIndex) - page;
        break;
      case "Enter":
      case " ":
        if (activeIndex >= 0 && typeof onActivate === "function") {
          event.preventDefault();
          onActivate(rows[activeIndex], activeIndex);
        }
        return;
      default:
        return;
    }
    event.preventDefault();
    setActiveIndex(next);
  }

  container.addEventListener("scroll", schedule, { passive: true });
  container.addEventListener("keydown", onKeyDown);
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
      if (activeIndex >= rows.length) activeIndex = rows.length ? rows.length - 1 : -1;
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
    getActiveIndex() {
      return activeIndex;
    },
    setActiveIndex(index) {
      setActiveIndex(index);
    },
    destroy() {
      destroyed = true;
      container.removeEventListener("scroll", schedule);
      container.removeEventListener("keydown", onKeyDown);
      if (resizeObserver) resizeObserver.disconnect();
      container.textContent = "";
    },
  };
}

export { createVirtualList };
