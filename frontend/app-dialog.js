// In-app prompt/confirm. Replaces native browser dialogs so the host page
// chrome ("127.0.0.1:8766 says") never appears. Markup and classes match
// the existing Create project / terminal-confirm modals.
import { trapFocus } from "./dom-utils.js";
import { t } from "./state.js";

let releaseFocus = null;
let activeSettle = null;

function els() {
  const modal = document.querySelector("#app-dialog-modal");
  return {
    modal,
    title: modal?.querySelector("#app-dialog-title"),
    message: modal?.querySelector("#app-dialog-message"),
    detail: modal?.querySelector("#app-dialog-detail"),
    field: modal?.querySelector("#app-dialog-field"),
    label: modal?.querySelector("#app-dialog-label"),
    input: modal?.querySelector("#app-dialog-input"),
    cancel: modal?.querySelector("#app-dialog-cancel"),
    confirm: modal?.querySelector("#app-dialog-confirm"),
    alt: modal?.querySelector("#app-dialog-alt"),
    close: modal?.querySelector("[data-app-dialog-cancel]"),
  };
}

function closeDialog() {
  const { modal } = els();
  if (modal) modal.setAttribute("hidden", "");
  document.body.style.overflow = "";
  if (releaseFocus) {
    releaseFocus();
    releaseFocus = null;
  }
}

function cancelActive() {
  if (typeof activeSettle === "function") activeSettle({ kind: "cancel" });
}

function openDialog({ mode, title, message, detail, label, value, placeholder, confirmLabel, altLabel, danger }) {
  const ui = els();
  if (!ui.modal || !ui.title || !ui.cancel || !ui.confirm) {
    return Promise.resolve(mode === "prompt" || mode === "permission" ? null : false);
  }
  cancelActive();

  const isPermission = mode === "permission";
  ui.title.textContent = title || "";
  ui.cancel.textContent = isPermission ? t("ask.permission.deny") : t("dialog.cancel");
  ui.confirm.textContent = confirmLabel || t("dialog.ok");
  ui.confirm.className = danger ? "btn-danger" : "btn-primary";
  if (ui.alt) {
    const showAlt = Boolean(altLabel);
    ui.alt.hidden = !showAlt;
    if (showAlt) ui.alt.textContent = altLabel;
  }

  const hasMessage = Boolean(message);
  ui.message.hidden = !hasMessage;
  ui.message.textContent = message || "";
  const dialog = ui.modal.querySelector("[role='dialog']");
  if (dialog) {
    if (hasMessage) dialog.setAttribute("aria-describedby", "app-dialog-message");
    else dialog.removeAttribute("aria-describedby");
  }
  const closeBtn = ui.modal.querySelector(".modal-close");
  if (closeBtn) closeBtn.setAttribute("aria-label", t("dialog.cancel"));

  const hasDetail = Boolean(detail);
  ui.detail.hidden = !hasDetail;
  const detailCode = ui.detail.querySelector("code");
  if (detailCode) detailCode.textContent = detail || "";
  else ui.detail.textContent = detail || "";

  const isPrompt = mode === "prompt";
  ui.field.hidden = !isPrompt;
  if (isPrompt) {
    ui.label.textContent = label || "";
    ui.input.value = value || "";
    ui.input.placeholder = placeholder || "";
  } else {
    ui.input.value = "";
  }

  ui.modal.removeAttribute("hidden");
  document.body.style.overflow = "hidden";
  releaseFocus = trapFocus(ui.modal);

  if (isPrompt) {
    ui.input.focus();
    ui.input.select();
  } else {
    ui.confirm.focus();
  }

  return new Promise(resolve => {
    const cancelTargets = Array.from(ui.modal.querySelectorAll("[data-app-dialog-cancel]"));
    const settle = result => {
      if (activeSettle !== settle) return;
      activeSettle = null;
      ui.input.removeEventListener("keydown", onEnter);
      ui.confirm.removeEventListener("click", onConfirm);
      if (ui.alt) ui.alt.removeEventListener("click", onAlt);
      document.removeEventListener("keydown", onEscape);
      cancelTargets.forEach(target => target.removeEventListener("click", onCancel));
      closeDialog();
      if (result.kind === "alt") {
        resolve(isPermission ? "session" : true);
      } else if (result.kind === "confirm") {
        if (isPermission) resolve("once");
        else resolve(mode === "prompt" ? result.value : true);
      } else if (mode === "prompt" || isPermission) {
        resolve(null);
      } else {
        resolve(false);
      }
    };
    const onConfirm = () => {
      if (mode === "prompt") {
        const next = String(ui.input.value || "").trim();
        if (!next) {
          settle({ kind: "cancel" });
          return;
        }
        settle({ kind: "confirm", value: next });
        return;
      }
      settle({ kind: "confirm" });
    };
    const onCancel = () => settle({ kind: "cancel" });
    const onEnter = event => {
      if (event.key === "Enter") {
        event.preventDefault();
        onConfirm();
      }
    };
    const onEscape = event => {
      if (event.key === "Escape") {
        event.preventDefault();
        settle({ kind: "cancel" });
      }
    };
    const onAlt = () => settle({ kind: "alt" });
    activeSettle = settle;
    ui.input.addEventListener("keydown", onEnter);
    ui.confirm.addEventListener("click", onConfirm);
    if (ui.alt && altLabel) ui.alt.addEventListener("click", onAlt);
    document.addEventListener("keydown", onEscape);
    cancelTargets.forEach(target => target.addEventListener("click", onCancel));
  });
}

function showAppConfirm({ title, message, detail, confirmLabel, danger = false } = {}) {
  return openDialog({ mode: "confirm", title, message, detail, confirmLabel, danger });
}

function showAppPrompt({ title, label, value, placeholder, confirmLabel, message } = {}) {
  return openDialog({ mode: "prompt", title, message, label, value, placeholder, confirmLabel });
}

function showAppPermission({ title, message, detail, danger = false } = {}) {
  return openDialog({
    mode: "permission",
    title,
    message,
    detail,
    confirmLabel: t("ask.permission.allowOnce"),
    altLabel: t("ask.permission.allowChat"),
    danger,
  });
}

export { showAppConfirm, showAppPermission, showAppPrompt };
