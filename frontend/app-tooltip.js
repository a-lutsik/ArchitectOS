// Hover/focus tooltips for icon-only controls. Lives on document.body so
// overflow:hidden sidebars cannot clip the label.

const SHOW_DELAY_MS = 280;
const HIDE_DELAY_MS = 50;
const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");

let showTimer = 0;
let hideTimer = 0;
let active = null;

function tipEl() {
  let el = document.getElementById("app-tooltip");
  if (!el) {
    el = document.createElement("div");
    el.id = "app-tooltip";
    el.className = "app-tooltip";
    el.setAttribute("role", "tooltip");
    el.hidden = true;
    document.body.appendChild(el);
  }
  return el;
}

function tooltipText(el) {
  return (el.getAttribute("data-tooltip") || el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
}

function formatShortcut(raw) {
  if (!raw) return "";
  if (MAC) return raw;
  return raw.replace(/⌘/g, "Ctrl+").replace(/⇧/g, "Shift+").replace(/⌥/g, "Alt+");
}

function position(anchor, tip) {
  const rect = anchor.getBoundingClientRect();
  const size = tip.getBoundingClientRect();
  const gap = 8;
  let top = rect.bottom + gap;
  if (top + size.height > window.innerHeight - 8) top = rect.top - size.height - gap;
  let left = rect.left + (rect.width - size.width) / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - size.width - 8));
  tip.style.top = `${Math.round(Math.max(8, top))}px`;
  tip.style.left = `${Math.round(left)}px`;
}

function hideTip() {
  const tip = document.getElementById("app-tooltip");
  if (active) active.removeAttribute("aria-describedby");
  active = null;
  if (!tip) return;
  tip.classList.remove("is-visible");
  window.setTimeout(() => {
    if (!tip.classList.contains("is-visible")) tip.hidden = true;
  }, 140);
}

function showFor(el) {
  const text = tooltipText(el);
  if (!text) return;
  const tip = tipEl();
  tip.replaceChildren();
  const label = document.createElement("span");
  label.className = "app-tooltip-label";
  label.textContent = text;
  tip.appendChild(label);
  const shortcut = formatShortcut(el.getAttribute("data-tooltip-shortcut") || "");
  if (shortcut) {
    const kbd = document.createElement("kbd");
    kbd.textContent = shortcut;
    tip.appendChild(kbd);
  }
  if (active && active !== el) active.removeAttribute("aria-describedby");
  el.setAttribute("aria-describedby", "app-tooltip");
  active = el;
  el.removeAttribute("title");
  tip.hidden = false;
  position(el, tip);
  requestAnimationFrame(() => {
    position(el, tip);
    tip.classList.add("is-visible");
  });
}

function bindAppTooltips() {
  document.addEventListener("pointerover", event => {
    const el = event.target.closest?.("[data-tooltip]");
    if (!el) return;
    window.clearTimeout(hideTimer);
    if (el === active) return;
    window.clearTimeout(showTimer);
    if (active) showFor(el);
    else showTimer = window.setTimeout(() => showFor(el), SHOW_DELAY_MS);
  });
  document.addEventListener("pointerout", event => {
    const el = event.target.closest?.("[data-tooltip]");
    if (!el) return;
    const next = event.relatedTarget;
    if (next && (el.contains(next) || next.closest?.("[data-tooltip]") === el)) return;
    window.clearTimeout(showTimer);
    hideTimer = window.setTimeout(hideTip, HIDE_DELAY_MS);
  });
  document.addEventListener("focusin", event => {
    const el = event.target.closest?.("[data-tooltip]");
    if (el) {
      window.clearTimeout(hideTimer);
      showFor(el);
    }
  });
  document.addEventListener("focusout", event => {
    const el = event.target.closest?.("[data-tooltip]");
    if (!el) return;
    window.clearTimeout(showTimer);
    hideTimer = window.setTimeout(hideTip, HIDE_DELAY_MS);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") hideTip();
  });
  window.addEventListener("scroll", hideTip, true);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bindAppTooltips);
} else {
  bindAppTooltips();
}
