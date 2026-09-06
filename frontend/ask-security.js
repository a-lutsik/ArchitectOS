/* Ask security gate: same /api/security/preview policy as Settings → Security Preview. */
import { api } from "./api-client.js";
import { showAppConfirm } from "./app-dialog.js";
import { t } from "./state.js";

const STATUS_SELECTORS = ["#ask-security-status", "#workspace-ask-security-status"];

function findingsList(findings) {
  return (Array.isArray(findings) ? findings : [])
    .filter(item => item && item.kind)
    .map(item => `${item.kind} ×${item.count || 1}`);
}

function formatSecurityFindings(findings) {
  return findingsList(findings).join(" · ");
}

function securityParts(security) {
  if (!security || security.scanning) return [];
  const parts = [];
  if (security.clean && !security.redacted) {
    parts.push(t("ask.security.clean"));
    return parts;
  }
  if (!security.redacted && !security.message_redacted && !security.result_redacted && !security.context_redacted) {
    return parts;
  }
  parts.push(t("ask.security.redacted"));
  if (security.message_redacted) parts.push(t("ask.security.promptRedacted"));
  if (security.result_redacted) parts.push(t("ask.security.resultRedacted"));
  if (security.context_redacted && !security.message_redacted) parts.push(t("ask.security.contextRedacted"));
  const kinds = formatSecurityFindings(security.message_findings || security.findings);
  if (kinds) parts.push(kinds);
  return parts;
}

function formatAskSecurityLabel(security) {
  if (!security) return "";
  if (security.scanning) return t("ask.security.scanning");
  return securityParts(security).join(" · ");
}

function setAskSecurityStatus(security) {
  const label = formatAskSecurityLabel(security);
  const flagged = Boolean(security && (security.redacted || security.message_redacted || security.result_redacted));
  const scanning = Boolean(security && security.scanning);
  for (const selector of STATUS_SELECTORS) {
    const el = document.querySelector(selector);
    if (!el) continue;
    if (!label) {
      el.hidden = true;
      el.textContent = "";
      el.classList.remove("is-flagged", "is-scanning");
      continue;
    }
    el.hidden = false;
    el.textContent = label;
    el.classList.toggle("is-flagged", flagged);
    el.classList.toggle("is-scanning", scanning);
  }
}

function setBubbleSecurity(el, security) {
  if (!el) return;
  let line = el.querySelector(".message-security");
  const label = formatAskSecurityLabel(security);
  const flagged = Boolean(security && (security.redacted || security.message_redacted || security.result_redacted));
  if (!label || security?.scanning || security?.clean) {
    if (line) line.remove();
    return;
  }
  if (!line) {
    line = document.createElement("div");
    line.className = "message-security";
    const usage = el.querySelector(".message-usage");
    const actions = el.querySelector(".message-actions");
    if (usage) el.insertBefore(line, usage);
    else if (actions) el.insertBefore(line, actions);
    else el.appendChild(line);
  }
  line.textContent = label;
  line.classList.toggle("is-flagged", flagged);
  line.title = t("ask.security.statusTitle");
}

async function previewAskSecurity(text) {
  return api("/api/security/preview", { method: "POST", body: JSON.stringify({ text: String(text || "") }) });
}

async function confirmAskSend(text) {
  const value = String(text || "").trim();
  if (!value) return true;
  setAskSecurityStatus({ scanning: true });
  let preview;
  try {
    preview = await previewAskSecurity(value);
  } catch (_err) {
    setAskSecurityStatus(null);
    return true;
  }
  if (!preview || !preview.redacted) {
    setAskSecurityStatus(null);
    return true;
  }
  const kinds = formatSecurityFindings(preview.findings);
  const ok = await showAppConfirm({
    title: t("ask.security.warnTitle"),
    message: t("ask.security.warnMessage"),
    detail: kinds || undefined,
    confirmLabel: t("ask.security.warnSend"),
    danger: true,
  });
  if (!ok) {
    setAskSecurityStatus(preview);
    return false;
  }
  setAskSecurityStatus(preview);
  return true;
}

export {
  confirmAskSend, formatAskSecurityLabel, formatSecurityFindings,
  previewAskSecurity, setAskSecurityStatus, setBubbleSecurity,
};
