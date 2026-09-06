/* Zoom/pan lightbox for Ask diagrams and message images. */
import { showSnackbar, trapFocus } from "./dom-utils.js";
import { t } from "./state.js";

const MIN_SCALE = 0.25;
const MAX_SCALE = 8;

let releaseFocus = null;
let view = { scale: 1, x: 0, y: 0 };
let drag = null;
let mermaidSrc = "";

const ICONS = {
  mermaid: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="16 18 22 12 16 6"></polyline><polyline points="8 6 2 12 8 18"></polyline></svg>',
  png: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>',
  expand: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 3 21 3 21 9"></polyline><polyline points="9 21 3 21 3 15"></polyline><line x1="21" y1="3" x2="14" y2="10"></line><line x1="3" y1="21" x2="10" y2="14"></line></svg>',
};

function els() {
  const root = document.querySelector("#media-lightbox");
  return {
    root,
    title: root?.querySelector("#media-lightbox-title"),
    zoom: root?.querySelector("#media-lightbox-zoom"),
    viewport: root?.querySelector("#media-lightbox-viewport"),
    canvas: root?.querySelector("#media-lightbox-canvas"),
  };
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function applyView() {
  const { canvas, zoom } = els();
  if (!canvas) return;
  canvas.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  if (zoom) zoom.textContent = `${Math.round(view.scale * 100)}%`;
}

function contentSize(canvas) {
  const child = canvas?.firstElementChild;
  if (!child) return { w: 0, h: 0 };
  if (child.tagName === "IMG") {
    return { w: child.naturalWidth || child.clientWidth, h: child.naturalHeight || child.clientHeight };
  }
  if (child.tagName === "SVG") {
    const vb = child.viewBox?.baseVal;
    if (vb && vb.width && vb.height) return { w: vb.width, h: vb.height };
    try {
      const box = child.getBBox();
      if (box.width && box.height) return { w: box.width, h: box.height };
    } catch (_err) { /* SVG not in DOM yet */ }
    const rect = child.getBoundingClientRect();
    return { w: rect.width / (view.scale || 1), h: rect.height / (view.scale || 1) };
  }
  return { w: child.scrollWidth, h: child.scrollHeight };
}

function fitView() {
  const { viewport, canvas } = els();
  if (!viewport || !canvas) return;
  const size = contentSize(canvas);
  const pad = 48;
  const vw = Math.max(1, viewport.clientWidth - pad);
  const vh = Math.max(1, viewport.clientHeight - pad);
  const scale = clamp(Math.min(vw / Math.max(1, size.w), vh / Math.max(1, size.h)), MIN_SCALE, MAX_SCALE);
  view.scale = Number.isFinite(scale) ? scale : 1;
  view.x = (viewport.clientWidth - size.w * view.scale) / 2;
  view.y = (viewport.clientHeight - size.h * view.scale) / 2;
  applyView();
}

function zoomAt(nextScale, cx, cy) {
  const { viewport } = els();
  if (!viewport) return;
  const rect = viewport.getBoundingClientRect();
  const px = cx - rect.left;
  const py = cy - rect.top;
  const scale = clamp(nextScale, MIN_SCALE, MAX_SCALE);
  view.x = px - ((px - view.x) * scale) / view.scale;
  view.y = py - ((py - view.y) * scale) / view.scale;
  view.scale = scale;
  applyView();
}

function zoomBy(factor) {
  const { viewport } = els();
  if (!viewport) return;
  const rect = viewport.getBoundingClientRect();
  zoomAt(view.scale * factor, rect.left + rect.width / 2, rect.top + rect.height / 2);
}

function closeLightbox() {
  const { root, canvas } = els();
  if (!root || root.hasAttribute("hidden")) return;
  root.setAttribute("hidden", "");
  document.body.style.overflow = "";
  mermaidSrc = "";
  setDiagramCopyTools(false);
  if (canvas) canvas.replaceChildren();
  if (releaseFocus) {
    releaseFocus();
    releaseFocus = null;
  }
  drag = null;
}

function setDiagramCopyTools(show) {
  const { root } = els();
  if (!root) return;
  root.querySelectorAll("[data-media-copy]").forEach((btn) => {
    btn.hidden = !show;
  });
}

function openLightbox({ title, node }) {
  const ui = els();
  if (!ui.root || !ui.canvas || !node) return;
  ui.title.textContent = title || t("ask.media.title");
  const hint = ui.root.querySelector("#media-lightbox-hint");
  if (hint) hint.textContent = t("ask.media.hint");
  const fit = ui.root.querySelector("[data-media-zoom='fit']");
  if (fit) fit.textContent = t("ask.media.fit");
  const close = ui.root.querySelector("[data-media-lightbox-close].modal-close");
  if (close) close.setAttribute("aria-label", t("ask.media.close"));
  ui.canvas.replaceChildren(node);
  ui.root.removeAttribute("hidden");
  document.body.style.overflow = "hidden";
  if (releaseFocus) releaseFocus();
  releaseFocus = trapFocus(ui.root);
  view = { scale: 1, x: 0, y: 0 };
  applyView();
  requestAnimationFrame(() => {
    fitView();
    ui.root.querySelector("[data-media-zoom='in']")?.focus();
  });
}

function cloneSvg(svg) {
  const clone = svg.cloneNode(true);
  clone.removeAttribute("width");
  clone.removeAttribute("height");
  clone.style.width = "";
  clone.style.height = "";
  clone.style.maxWidth = "none";
  clone.style.maxHeight = "none";
  clone.style.display = "block";
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
  const size = liveSvgSize(svg);
  const pad = 8;
  clone.setAttribute("viewBox", `${size.x - pad} ${size.y - pad} ${size.w + pad * 2} ${size.h + pad * 2}`);
  clone.setAttribute("width", String(Math.ceil(size.w + pad * 2)));
  clone.setAttribute("height", String(Math.ceil(size.h + pad * 2)));
  return clone;
}

function mermaidSourceFrom(box) {
  const raw = String(box?.querySelector?.(".rich-mermaid-src")?.textContent || mermaidSrc || "").trim();
  if (!raw) return "";
  if (/^```/.test(raw)) return raw;
  return "```mermaid\n" + raw + "\n```";
}

function diagramBackground() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "#fbfaf7" : "#18181b";
}

function liveSvgSize(svg) {
  try {
    const box = svg.getBBox();
    if (box.width && box.height) return { w: box.width, h: box.height, x: box.x, y: box.y };
  } catch (_err) { /* detached or not rendered */ }
  const vb = svg.viewBox?.baseVal;
  if (vb && vb.width && vb.height) return { w: vb.width, h: vb.height, x: vb.x || 0, y: vb.y || 0 };
  const rect = svg.getBoundingClientRect?.();
  if (rect && rect.width && rect.height) return { w: rect.width, h: rect.height, x: 0, y: 0 };
  const width = Number.parseFloat(svg.getAttribute("width") || "");
  const height = Number.parseFloat(svg.getAttribute("height") || "");
  if (width && height && Number.isFinite(width) && Number.isFinite(height)) {
    return { w: width, h: height, x: 0, y: 0 };
  }
  return { w: 960, h: 540, x: 0, y: 0 };
}

function serializeSvg(svg) {
  const clone = cloneSvg(svg);
  let xml = new XMLSerializer().serializeToString(clone);
  if (!/\sxmlns=/.test(xml)) xml = xml.replace(/<svg\b/, '<svg xmlns="http://www.w3.org/2000/svg"');
  return { xml, width: Number.parseFloat(clone.getAttribute("width") || "1"), height: Number.parseFloat(clone.getAttribute("height") || "1") };
}

function loadSvgImage(xml) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const dataUri = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
    let blobUrl = "";
    const failOverToBlob = () => {
      blobUrl = URL.createObjectURL(new Blob([xml], { type: "image/svg+xml" }));
      img.onload = () => {
        URL.revokeObjectURL(blobUrl);
        resolve(img);
      };
      img.onerror = () => {
        URL.revokeObjectURL(blobUrl);
        reject(new Error("svg"));
      };
      img.src = blobUrl;
    };
    img.onload = () => resolve(img);
    img.onerror = failOverToBlob;
    img.src = dataUri;
  });
}

function svgToPngBlob(svg) {
  const { xml, width, height } = serializeSvg(svg);
  const scale = 2;
  const pad = 24;
  const maxEdge = 8192;
  let drawW = Math.max(1, Math.ceil(width * scale));
  let drawH = Math.max(1, Math.ceil(height * scale));
  const edge = Math.max(drawW, drawH);
  if (edge > maxEdge) {
    const shrink = maxEdge / edge;
    drawW = Math.max(1, Math.floor(drawW * shrink));
    drawH = Math.max(1, Math.floor(drawH * shrink));
  }
  return loadSvgImage(xml).then((img) => new Promise((resolve, reject) => {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = drawW + pad * 2;
      canvas.height = drawH + pad * 2;
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        reject(new Error("canvas"));
        return;
      }
      ctx.fillStyle = diagramBackground();
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, pad, pad, drawW, drawH);
      canvas.toBlob((blob) => {
        if (blob) resolve(blob);
        else reject(new Error("png"));
      }, "image/png");
    } catch (err) {
      reject(err);
    }
  }));
}

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function canWriteClipboardImage() {
  return Boolean(navigator.clipboard?.write && typeof ClipboardItem === "function");
}

async function writePngToClipboard(pngPromise) {
  if (!canWriteClipboardImage()) throw new Error("clipboard");
  // Pass the Promise into ClipboardItem during the click turn. Awaiting the
  // rasterizer first drops user activation and Chrome/WebKit reject the write.
  try {
    await navigator.clipboard.write([new ClipboardItem({ "image/png": pngPromise })]);
    return;
  } catch (_err) {
    const blob = await pngPromise;
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
  }
}

function flashCopied(btn, key) {
  if (!btn) return;
  const label = t(key);
  btn.setAttribute("data-tooltip", label);
  btn.setAttribute("aria-label", label);
  btn.classList.add("is-copied");
  window.setTimeout(() => {
    const restore = t(btn.dataset.i18nTitle || key);
    btn.setAttribute("data-tooltip", restore);
    btn.setAttribute("aria-label", restore);
    btn.classList.remove("is-copied");
  }, 1400);
}

async function copyMermaidSource(source, btn) {
  const text = String(source || "").trim();
  if (!text) {
    showSnackbar(t("ask.media.copyFailed"), "error");
    return;
  }
  try {
    await copyText(text);
    flashCopied(btn, "ask.media.copiedMermaid");
    showSnackbar(t("ask.media.copiedMermaid"), "success");
  } catch (_err) {
    showSnackbar(t("ask.media.copyFailed"), "error");
  }
}

async function copyMermaidPng(svg, btn) {
  if (!svg) {
    showSnackbar(t("ask.media.copyFailed"), "error");
    return;
  }
  const pngPromise = svgToPngBlob(svg);
  try {
    await writePngToClipboard(pngPromise);
    flashCopied(btn, "ask.media.copiedPng");
    showSnackbar(t("ask.media.copiedPng"), "success");
    return;
  } catch (_err) {
    /* WebView/http origins often block image clipboard; save a PNG instead. */
  }
  try {
    const blob = await pngPromise;
    downloadBlob(blob, "diagram.png");
    flashCopied(btn, "ask.media.downloadedPng");
    showSnackbar(t("ask.media.downloadedPng"), "success");
  } catch (_err) {
    showSnackbar(t("ask.media.copyFailed"), "error");
  }
}

function openMermaid(box) {
  const svg = box.querySelector("svg");
  if (!svg) return;
  mermaidSrc = String(box.querySelector(".rich-mermaid-src")?.textContent || "").trim();
  setDiagramCopyTools(true);
  openLightbox({ title: t("ask.media.diagram"), node: cloneSvg(svg) });
}

function openImage(img) {
  mermaidSrc = "";
  setDiagramCopyTools(false);
  const node = document.createElement("img");
  node.src = img.currentSrc || img.src;
  node.alt = img.alt || "";
  node.addEventListener("load", () => fitView(), { once: true });
  openLightbox({ title: t("ask.media.image"), node });
}

function mermaidActionButton(action, labelKey, icon) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "rich-mermaid-action";
  btn.dataset.mermaidAction = action;
  btn.dataset.i18nTitle = labelKey;
  btn.setAttribute("data-tooltip", t(labelKey));
  btn.setAttribute("aria-label", t(labelKey));
  btn.innerHTML = icon;
  return btn;
}

function decorateAskMedia(root) {
  const scope = root && root.querySelectorAll ? root : document;
  scope.querySelectorAll?.(".rich-mermaid.rendered")?.forEach((box) => {
    if (box.querySelector(".rich-mermaid-actions") || !box.querySelector("svg")) return;
    const bar = document.createElement("div");
    bar.className = "rich-mermaid-actions";
    bar.append(
      mermaidActionButton("mermaid", "ask.media.copyMermaid", ICONS.mermaid),
      mermaidActionButton("png", "ask.media.copyPng", ICONS.png),
      mermaidActionButton("expand", "ask.media.expand", ICONS.expand),
    );
    box.appendChild(bar);
  });
}

function bindLightbox() {
  if (document.documentElement.dataset.mediaLightboxBound === "1") return;
  document.documentElement.dataset.mediaLightboxBound = "1";

  document.addEventListener("click", (event) => {
    const actionBtn = event.target.closest?.("[data-mermaid-action]");
    if (actionBtn) {
      event.preventDefault();
      event.stopPropagation();
      const box = actionBtn.closest(".rich-mermaid");
      const action = actionBtn.dataset.mermaidAction;
      if (action === "expand") openMermaid(box);
      else if (action === "mermaid") copyMermaidSource(mermaidSourceFrom(box), actionBtn);
      else if (action === "png") copyMermaidPng(box?.querySelector("svg"), actionBtn);
      return;
    }
    const mermaid = event.target.closest?.(".rich-mermaid.rendered");
    if (mermaid && mermaid.querySelector("svg") && !event.target.closest("a") && !event.target.closest(".rich-mermaid-actions")) {
      event.preventDefault();
      openMermaid(mermaid);
      return;
    }
    const img = event.target.closest?.(".message img, .attachment-chip img");
    if (img && img.tagName === "IMG") {
      event.preventDefault();
      openImage(img);
    }
  });

  const ui = els();
  if (!ui.root) return;

  ui.root.addEventListener("click", (event) => {
    if (event.target.closest("[data-media-lightbox-close]")) {
      event.preventDefault();
      closeLightbox();
    }
    const copyKind = event.target.closest("[data-media-copy]")?.dataset.mediaCopy;
    if (copyKind === "mermaid") {
      event.preventDefault();
      copyMermaidSource(mermaidSourceFrom(), event.target.closest("[data-media-copy]"));
      return;
    }
    if (copyKind === "png") {
      event.preventDefault();
      copyMermaidPng(ui.canvas?.querySelector("svg"), event.target.closest("[data-media-copy]"));
      return;
    }
    const zoom = event.target.closest("[data-media-zoom]")?.dataset.mediaZoom;
    if (zoom === "in") zoomBy(1.25);
    if (zoom === "out") zoomBy(0.8);
    if (zoom === "fit") fitView();
  });

  document.addEventListener("keydown", (event) => {
    if (ui.root.hasAttribute("hidden")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeLightbox();
      return;
    }
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomBy(1.25);
    }
    if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomBy(0.8);
    }
    if (event.key === "0") {
      event.preventDefault();
      fitView();
    }
  });

  ui.viewport?.addEventListener("wheel", (event) => {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
    zoomAt(view.scale * factor, event.clientX, event.clientY);
  }, { passive: false });

  ui.viewport?.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, ox: view.x, oy: view.y };
    ui.viewport.classList.add("is-panning");
    ui.viewport.setPointerCapture(event.pointerId);
  });
  ui.viewport?.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    view.x = drag.ox + (event.clientX - drag.x);
    view.y = drag.oy + (event.clientY - drag.y);
    applyView();
  });
  const endDrag = (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    drag = null;
    ui.viewport?.classList.remove("is-panning");
  };
  ui.viewport?.addEventListener("pointerup", endDrag);
  ui.viewport?.addEventListener("pointercancel", endDrag);
  ui.viewport?.addEventListener("dblclick", (event) => {
    event.preventDefault();
    fitView();
  });
}

bindLightbox();

export { decorateAskMedia, closeLightbox };