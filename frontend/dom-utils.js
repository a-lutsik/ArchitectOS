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

export {
  escapeHtml, setText, setPlaceholder, setButton, setTextContent,
  setElementValue, setElementDisabled, on, onAll, showSnackbar, labelPrefix,
};
