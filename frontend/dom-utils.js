// ArchitectOS DOM & formatting helpers: escapeHtml, element setters,
// event binding, and the snackbar. ES module.
import { t } from "./state.js";

function escapeHtml(value) { return String(value || "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char])); }
function setText(selector, key) { const el = document.querySelector(selector); if (el) el.textContent = t(key); }
function setPlaceholder(selector, key) { const el = document.querySelector(selector); if (el) el.placeholder = t(key); }
function setButton(selector, key) { setText(selector, key); }
function setTextContent(selector, value) {
  const el = typeof selector === "string" ? document.querySelector(selector) : selector;
  if (el) el.textContent = value;
  return el;
}
function setElementValue(selector, value) {
  const el = typeof selector === "string" ? document.querySelector(selector) : selector;
  if (el) el.value = value;
  return el;
}
function setElementDisabled(selector, disabled) {
  const el = typeof selector === "string" ? document.querySelector(selector) : selector;
  if (el) el.disabled = disabled;
  return el;
}
function on(selector, eventName, handler, options) {
  const el = typeof selector === "string" ? document.querySelector(selector) : selector;
  if (el) el.addEventListener(eventName, handler, options);
  return el;
}
function onAll(selector, eventName, handler, options) {
  document.querySelectorAll(selector).forEach(el => el.addEventListener(eventName, handler, options));
}
function showSnackbar(message, tone = "info") {
  let stack = document.querySelector("#snackbar-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.id = "snackbar-stack";
    stack.className = "snackbar-stack";
    document.body.appendChild(stack);
  }
  const item = document.createElement("div");
  item.className = `snackbar ${tone}`.trim();
  item.setAttribute("role", tone === "error" ? "alert" : "status");
  item.innerHTML = `<span>${escapeHtml(message)}</span><button type="button" aria-label="Dismiss">&times;</button>`;
  stack.appendChild(item);
  const close = () => {
    item.classList.add("leaving");
    window.setTimeout(() => item.remove(), 180);
  };
  on(item.querySelector("button"), "click", close);
  window.setTimeout(close, tone === "error" ? 7000 : 4200);
}
function labelPrefix(inputSelector, key) {
  const input = document.querySelector(inputSelector);
  const label = input ? input.closest("label") : null;
  if (!label) return;
  const control = label.querySelector("input,select,textarea");
  for (const node of Array.from(label.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE) node.remove();
  }
  label.insertBefore(document.createTextNode(t(key)), control || label.firstChild);
}

const FOCUSABLE_SELECTOR = 'a[href], button, input, select, textarea, [tabindex]';

// Modal focus trap: keeps Tab / Shift+Tab cycling among the focusable elements
// inside `container`. Returns a release function that removes the trap and
// restores focus to `restoreTo` (defaults to whatever was focused when the
// trap was installed — i.e. the element that opened the modal).
function trapFocus(container, restoreTo = document.activeElement) {
  if (!container || typeof container.addEventListener !== "function") return () => {};
  const focusableItems = () => Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR))
    .filter(el => !el.disabled && el.tabIndex >= 0 && el.getAttribute("aria-hidden") !== "true");
  const onKeydown = event => {
    if (event.key !== "Tab") return;
    const items = focusableItems();
    if (!items.length) { event.preventDefault(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !container.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !container.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };
  container.addEventListener("keydown", onKeydown);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    container.removeEventListener("keydown", onKeydown);
    if (restoreTo && typeof restoreTo.focus === "function" && restoreTo.isConnected !== false) restoreTo.focus();
  };
}

export {
  escapeHtml, setText, setPlaceholder, setButton, setTextContent,
  setElementValue, setElementDisabled, on, onAll, showSnackbar, labelPrefix,
  trapFocus,
};
