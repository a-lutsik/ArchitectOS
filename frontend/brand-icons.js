/* Official brand marks for source / MCP tiles. */

const ICONS = {
  /* Simple Icons — GitHub Octocat */
  github: `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path fill="currentColor" d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"/></svg>`,
  /* Simple Icons — GitLab tanuki (#FC6D26) */
  gitlab: `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path fill="#FC6D26" d="m23.6004 9.5927-.0337-.0862L20.3.9814a.851.851 0 0 0-.3362-.405.8748.8748 0 0 0-.9997.0539.8748.8748 0 0 0-.29.4399l-2.2055 6.748H7.5375l-2.2057-6.748a.8573.8573 0 0 0-.29-.4412.8748.8748 0 0 0-.9997-.0537.8585.8585 0 0 0-.3362.4049L.4332 9.5015l-.0325.0862a6.0657 6.0657 0 0 0 2.0119 7.0105l.0113.0087.03.0213 4.976 3.7264 2.462 1.8633 1.4995 1.1321a1.0085 1.0085 0 0 0 1.2197 0l1.4995-1.1321 2.4619-1.8633 5.006-3.7489.0125-.01a6.0682 6.0682 0 0 0 2.0094-7.003z"/></svg>`,
  /* Simple Icons — Azure DevOps (#0078D4) */
  azure: `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path fill="#0078D4" d="M0 8.877L2.247 5.91l8.405-3.416V.022l7.37 5.393L2.966 8.338v8.225L0 15.707zm24-4.45v14.651l-5.753 4.9-9.303-3.057v3.056l-5.978-7.416 15.057 1.798V5.415z"/></svg>`,
  /* Official Granola app icon (favicon) */
  granola: `<img src="/brand/granola-icon.png" alt="" width="18" height="18" draggable="false" />`,
};

function brandKeyFromText(...parts) {
  const text = parts.map(part => String(part || "").toLowerCase()).join(" ");
  if (!text.trim()) return "";
  if (text.includes("granola")) return "granola";
  if (text.includes("github")) return "github";
  if (text.includes("gitlab")) return "gitlab";
  if (text.includes("azure") || text.includes("ado") || text.includes("devops")) return "azure";
  return "";
}

function brandIconHtml(key, fallback = "🔌") {
  const mark = ICONS[key];
  if (!mark) return fallback;
  return `<span class="brand-icon brand-icon-${key}" aria-hidden="true">${mark}</span>`;
}

function sourceBrandIcon(source, fallback = "📌") {
  const cfg = source?.config || {};
  const key = brandKeyFromText(cfg.adapter, cfg.mcp_server_id, source?.name, source?.kind);
  return brandIconHtml(key, fallback);
}

export { ICONS, brandIconHtml, brandKeyFromText, sourceBrandIcon };
