// MCP Master-Detail Layout JavaScript

let mcpServers = [];
let selectedServerId = null;
let mcpEventsBound = false;

async function loadMcpMasterDetail() {
  try {
    const payload = await api("/api/mcp/servers");
    mcpServers = payload.servers || [];

    renderMcpServerList();

    // Auto-select first server if none selected
    if (mcpServers.length > 0 && !selectedServerId) {
      selectMcpServer(mcpServers[0].id);
    }
  } catch (error) {
    console.error("Failed to load MCP servers:", error);
  }
}

function renderMcpServerList() {
  const container = document.querySelector("#mcp-server-items");
  if (!container) return;

  container.innerHTML = "";

  if (mcpServers.length === 0) {
    container.innerHTML = '<div class="mcp-empty-state">No MCP servers configured.</div>';
    return;
  }

  mcpServers.forEach(server => {
    const item = document.createElement("div");
    item.className = `mcp-server-item status-${server.status || 'planned'}`;
    if (server.id === selectedServerId) {
      item.classList.add("active");
    }

    const icon = getServerIcon(server.label);

    item.innerHTML = `
      <div class="mcp-server-icon">${icon}</div>
      <div class="mcp-server-info">
        <div class="mcp-server-item-name">${escapeHtml(server.label)}</div>
        <div class="mcp-server-item-status">${escapeHtml(server.transport || 'stdio')} • ${escapeHtml(server.status || 'planned')}</div>
      </div>
    `;

    item.addEventListener("click", () => selectMcpServer(server.id));
    container.appendChild(item);
  });
}

function getServerIcon(label) {
  const icons = {
    'filesystem': '📁',
    'github': '🐙',
    'azure': '☁️',
    'jira': '📋',
    'slack': '💬',
    'confluence': '📄',
    'granola': '🎥',
    'gitlab': '🦊',
    'notion': '📝',
    'google': '🌐'
  };

  const key = label.toLowerCase();
  for (const [name, icon] of Object.entries(icons)) {
    if (key.includes(name)) return icon;
  }

  return '🔌';
}

function selectMcpServer(serverId) {
  selectedServerId = serverId;
  renderMcpServerList();
  renderMcpServerDetail();
}

function renderMcpServerDetail() {
  const emptyState = document.querySelector("#mcp-detail-empty");
  const content = document.querySelector("#mcp-detail-content");

  if (!selectedServerId) {
    if (emptyState) emptyState.style.display = "flex";
    if (content) content.style.display = "none";
    return;
  }

  const server = mcpServers.find(s => s.id === selectedServerId);
  if (!server) {
    if (emptyState) emptyState.style.display = "flex";
    if (content) content.style.display = "none";
    return;
  }

  if (emptyState) emptyState.style.display = "none";
  if (content) content.style.display = "flex";

  // Update header
  const nameEl = document.querySelector("#mcp-detail-name");
  const statusEl = document.querySelector("#mcp-detail-status");
  const descEl = document.querySelector("#mcp-detail-description");

  if (nameEl) nameEl.textContent = server.label;
  if (statusEl) {
    statusEl.textContent = server.status || 'planned';
    statusEl.className = `status-badge ${server.status || 'planned'}`;
  }
  if (descEl) descEl.textContent = server.notes || 'No description available.';

  // Update fields
  const enabledEl = document.querySelector("#mcp-detail-enabled");
  const transportEl = document.querySelector("#mcp-detail-transport");
  const remoteUrlEl = document.querySelector("#mcp-detail-remote-url");
  const commandEl = document.querySelector("#mcp-detail-command");
  const envEl = document.querySelector("#mcp-detail-env");
  const headersEl = document.querySelector("#mcp-detail-headers");
  const authorizeBtn = document.querySelector("#mcp-authorize-btn");

  if (enabledEl) enabledEl.checked = server.enabled || false;
  if (transportEl) transportEl.value = server.transport || 'stdio';
  if (remoteUrlEl) remoteUrlEl.value = server.url || '';
  if (commandEl) {
    const command = Array.isArray(server.command) ? server.command.join(' ') : (server.command || '');
    commandEl.value = command;
  }
  if (envEl) {
    envEl.value = server.env ? JSON.stringify(server.env, null, 2) : '{}';
  }
  if (headersEl) {
    const headers = server.headers ? JSON.stringify(server.headers, null, 2) : '{}';
    headersEl.value = headers;
  }

  // Show/hide fields based on transport
  updateFieldVisibility(server.transport || 'stdio');
  if (authorizeBtn) {
    const isRemote = ['http', 'remote', 'streamable-http', 'remote http'].includes(server.transport || 'stdio');
    authorizeBtn.style.display = isRemote ? '' : 'none';
  }
  // Clear tools list (will be populated when user clicks Test)
  const toolsList = document.querySelector("#mcp-tools-list");
  if (toolsList) {
    toolsList.innerHTML = '<p class="mcp-empty-state">No tools available. Click Test to load tools.</p>';
  }

  // Update tools count
  const toolsCount = document.querySelector("#mcp-tools-count");
  if (toolsCount) toolsCount.textContent = '0 tools';
}

function updateFieldVisibility(transport) {
  const remoteField = document.querySelector("#mcp-field-remote-url");
  const commandField = document.querySelector("#mcp-field-command");
  const envField = document.querySelector("#mcp-field-env");
  const headersField = document.querySelector("#mcp-field-headers");

  const isRemote = ['http', 'remote', 'streamable-http', 'remote http'].includes(transport);

  if (remoteField) remoteField.style.display = isRemote ? 'block' : 'none';
  if (headersField) headersField.style.display = isRemote ? 'block' : 'none';
  if (commandField) commandField.style.display = isRemote ? 'none' : 'block';
  if (envField) envField.style.display = isRemote ? 'none' : 'block';
}

function mcpSlugify(value) {
  return String(value || "")
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}

function setAddMcpError(message) {
  const errorEl = document.querySelector("#add-mcp-error");
  if (!errorEl) return;
  if (message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
  } else {
    errorEl.textContent = "";
    errorEl.hidden = true;
  }
}

function updateAddMcpVisibility() {
  const transport = document.querySelector("#add-mcp-transport")?.value || "stdio";
  const isRemote = ["http", "remote", "streamable-http", "remote http"].includes(transport);
  const commandGroup = document.querySelector("#add-mcp-command-group");
  const urlGroup = document.querySelector("#add-mcp-url-group");
  const envGroup = document.querySelector("#add-mcp-env-group");
  const headersGroup = document.querySelector("#add-mcp-headers-group");
  if (commandGroup) commandGroup.style.display = isRemote ? "none" : "block";
  if (envGroup) envGroup.style.display = isRemote ? "none" : "block";
  if (urlGroup) urlGroup.style.display = isRemote ? "block" : "none";
  if (headersGroup) headersGroup.style.display = isRemote ? "block" : "none";
}

function openAddMcpModal() {
  const modal = document.querySelector("#add-mcp-modal");
  if (!modal) return;
  setAddMcpError("");
  const fields = {
    "#add-mcp-label": "",
    "#add-mcp-id": "",
    "#add-mcp-command": "",
    "#add-mcp-url": "",
    "#add-mcp-env": "",
    "#add-mcp-headers": "",
  };
  Object.entries(fields).forEach(([selector, value]) => {
    const el = document.querySelector(selector);
    if (el) el.value = value;
  });
  const transportEl = document.querySelector("#add-mcp-transport");
  if (transportEl) transportEl.value = "stdio";
  const enabledEl = document.querySelector("#add-mcp-enabled");
  if (enabledEl) enabledEl.checked = true;
  const idEl = document.querySelector("#add-mcp-id");
  if (idEl) delete idEl.dataset.touched;
  updateAddMcpVisibility();
  modal.removeAttribute("hidden");
  document.body.style.overflow = "hidden";
  setTimeout(() => document.querySelector("#add-mcp-label")?.focus(), 60);
}

function closeAddMcpModal() {
  const modal = document.querySelector("#add-mcp-modal");
  if (!modal) return;
  modal.setAttribute("hidden", "");
  document.body.style.overflow = "";
}

async function submitAddMcpServer() {
  const label = (document.querySelector("#add-mcp-label")?.value || "").trim();
  let id = (document.querySelector("#add-mcp-id")?.value || "").trim();
  if (!id) id = mcpSlugify(label);
  else id = mcpSlugify(id);
  const transport = document.querySelector("#add-mcp-transport")?.value || "stdio";
  const isRemote = ["http", "remote", "streamable-http", "remote http"].includes(transport);
  const commandRaw = (document.querySelector("#add-mcp-command")?.value || "").trim();
  const url = (document.querySelector("#add-mcp-url")?.value || "").trim();
  const envRaw = (document.querySelector("#add-mcp-env")?.value || "").trim();
  const headersRaw = (document.querySelector("#add-mcp-headers")?.value || "").trim();
  const enabled = Boolean(document.querySelector("#add-mcp-enabled")?.checked);

  if (!label) return setAddMcpError("Name is required.");
  if (!id) return setAddMcpError("Server id is required.");
  if (mcpServers.some(server => server.id === id)) {
    return setAddMcpError(`A server with id "${id}" already exists.`);
  }
  if (isRemote && !url) return setAddMcpError("Remote URL is required for http transport.");
  if (!isRemote && !commandRaw) return setAddMcpError("Command is required for stdio transport.");

  const payload = {
    id,
    label,
    transport: isRemote ? "http" : "stdio",
    enabled,
    status: "planned",
  };
  if (isRemote) {
    payload.url = url;
    if (headersRaw) {
      try {
        payload.headers = JSON.parse(headersRaw);
      } catch (_err) {
        return setAddMcpError("Headers must be valid JSON.");
      }
    }
  } else {
    payload.command = commandRaw.split(/\s+/).filter(Boolean);
    if (envRaw) {
      try {
        payload.env = JSON.parse(envRaw);
      } catch (_err) {
        return setAddMcpError("Env must be valid JSON.");
      }
    }
  }

  const createBtn = document.querySelector("#add-mcp-create");
  if (createBtn) createBtn.disabled = true;
  try {
    const result = await api("/api/mcp/servers", { method: "PATCH", body: JSON.stringify(payload) });
    closeAddMcpModal();
    await loadMcpMasterDetail();
    const newId = (result && result.server && result.server.id) || id;
    selectMcpServer(newId);
    if (typeof showSnackbar === "function") showSnackbar(`MCP server "${label}" added.`, "success");
  } catch (error) {
    setAddMcpError(error.message || "Failed to add server.");
  } finally {
    if (createBtn) createBtn.disabled = false;
  }
}

// Event listeners
function initMcpMasterDetail() {
  if (mcpEventsBound) return;
  mcpEventsBound = true;

  // Add server button + modal
  const addBtn = document.querySelector("#add-mcp-server");
  if (addBtn) addBtn.addEventListener("click", openAddMcpModal);
  document.querySelectorAll("[data-close-add-mcp]").forEach(el => el.addEventListener("click", closeAddMcpModal));
  const addTransportEl = document.querySelector("#add-mcp-transport");
  if (addTransportEl) addTransportEl.addEventListener("change", updateAddMcpVisibility);
  const addLabelEl = document.querySelector("#add-mcp-label");
  const addIdEl = document.querySelector("#add-mcp-id");
  if (addLabelEl && addIdEl) {
    addLabelEl.addEventListener("input", () => {
      if (!addIdEl.dataset.touched) addIdEl.value = mcpSlugify(addLabelEl.value);
    });
    addIdEl.addEventListener("input", () => { addIdEl.dataset.touched = "1"; });
  }
  const createBtn = document.querySelector("#add-mcp-create");
  if (createBtn) createBtn.addEventListener("click", () => { submitAddMcpServer().catch(console.error); });

  // Transport change
  const transportEl = document.querySelector("#mcp-detail-transport");
  if (transportEl) {
    transportEl.addEventListener("change", async () => {
      updateFieldVisibility(transportEl.value);

      try {
        await api("/api/mcp/servers", {
          method: "PATCH",
          body: JSON.stringify({
            id: selectedServerId,
            transport: transportEl.value
          })
        });
        await loadMcpMasterDetail();
      } catch (error) {
        console.error("Failed to update transport:", error);
      }
    });
  }

  // Authorize button
  const authorizeBtn = document.querySelector("#mcp-authorize-btn");
  if (authorizeBtn) {
    authorizeBtn.addEventListener("click", async () => {
      const resultsEl = document.querySelector("#mcp-test-results");
      if (!resultsEl) return;

      resultsEl.innerHTML = '<p>Starting authorization...</p>';

      try {
        const popup = window.open("about:blank", "architectos-mcp-auth");
        const payload = await api(`/api/mcp/servers/${selectedServerId}/auth/start`, {
          method: "POST",
          body: JSON.stringify({ base_url: window.location.origin })
        });

        const authUrl = payload.auth_url || "";
        resultsEl.innerHTML = `
          <p class="success">${escapeHtml(payload.message || "Complete auth in browser")}</p>
          ${authUrl ? `<p><a href="${escapeHtml(authUrl)}" target="architectos-mcp-auth" rel="noopener noreferrer">Open Granola sign-in</a></p>` : ""}
        `;

        if (popup && authUrl) {
          popup.location.href = authUrl;
        } else if (authUrl) {
          window.open(authUrl, "architectos-mcp-auth", "noopener,noreferrer");
        }

        await loadMcpMasterDetail();
      } catch (error) {
        resultsEl.innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
      }
    });
  }

  // Test button
  const testBtn = document.querySelector("#mcp-test-btn");
  if (testBtn) {
    testBtn.addEventListener("click", async () => {
      const resultsEl = document.querySelector("#mcp-test-results");
      const toolsList = document.querySelector("#mcp-tools-list");
      const toolsCount = document.querySelector("#mcp-tools-count");

      if (resultsEl) resultsEl.innerHTML = '<p>Launching server...</p>';

      try {
        const payload = await api(`/api/mcp/servers/${selectedServerId}/test`, {
          method: "POST",
          body: "{}"
        });

        if (resultsEl) {
          const ok = Boolean(payload.ready);
          resultsEl.innerHTML = `<p class="${ok ? "success" : "error"}">${escapeHtml(payload.message || (ok ? "Test completed" : "Test failed"))}</p>`;
        }

        // Display tools
        if (toolsList && payload.tools) {
          const tools = payload.tools || [];
          if (tools.length === 0) {
            toolsList.innerHTML = '<p class="mcp-empty-state">No tools found.</p>';
          } else {
            toolsList.innerHTML = tools.map(tool => `
              <div class="mcp-tool-card">
                <div class="mcp-tool-card-header">
                  <div class="mcp-tool-name">${escapeHtml(tool.name || '')}</div>
                </div>
                <div class="mcp-tool-description">${escapeHtml(tool.description || 'No description')}</div>
              </div>
            `).join('');
          }

          if (toolsCount) toolsCount.textContent = `${tools.length} tool${tools.length !== 1 ? 's' : ''}`;
        }

        await loadMcpMasterDetail();
      } catch (error) {
        if (resultsEl) {
          resultsEl.innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
        }
      }
    });
  }

  // Disable button
  const disableBtn = document.querySelector("#mcp-disable-btn");
  if (disableBtn) {
    disableBtn.addEventListener("click", async () => {
      try {
        await api("/api/mcp/servers", {
          method: "PATCH",
          body: JSON.stringify({
            id: selectedServerId,
            enabled: false
          })
        });
        await loadMcpMasterDetail();
      } catch (error) {
        console.error("Failed to disable server:", error);
      }
    });
  }

  // Save button
  const saveBtn = document.querySelector("#mcp-save-btn");
  if (saveBtn) {
    saveBtn.addEventListener("click", async () => {
      const enabledEl = document.querySelector("#mcp-detail-enabled");
      const transportEl = document.querySelector("#mcp-detail-transport");
      const remoteUrlEl = document.querySelector("#mcp-detail-remote-url");
      const commandEl = document.querySelector("#mcp-detail-command");
      const envEl = document.querySelector("#mcp-detail-env");
      const headersEl = document.querySelector("#mcp-detail-headers");

      try {
        const updates = {
          id: selectedServerId,
          enabled: enabledEl ? enabledEl.checked : false,
          transport: transportEl ? transportEl.value : 'stdio',
          url: remoteUrlEl ? remoteUrlEl.value : '',
          command: commandEl ? commandEl.value.split(' ').filter(Boolean) : []
        };

        if (envEl && envEl.value.trim()) {
          updates.env = JSON.parse(envEl.value);
        }

        if (headersEl && headersEl.value.trim()) {
          updates.headers = JSON.parse(headersEl.value);
        }

        await api("/api/mcp/servers", {
          method: "PATCH",
          body: JSON.stringify(updates)
        });

        await loadMcpMasterDetail();

        const resultsEl = document.querySelector("#mcp-test-results");
        if (resultsEl) {
          resultsEl.innerHTML = '<p class="success">Settings saved successfully!</p>';
        }
      } catch (error) {
        const resultsEl = document.querySelector("#mcp-test-results");
        if (resultsEl) {
          resultsEl.innerHTML = `<p class="error">Failed to save: ${error.message}</p>`;
        }
      }
    });
  }

  // Reset button
  const resetBtn = document.querySelector("#mcp-reset-btn");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      renderMcpServerDetail();
    });
  }

  window.addEventListener("message", async (event) => {
    const data = event.data || {};
    if (data.type !== "mcp-auth-complete") return;
    if (data.id) selectedServerId = data.id;
    const resultsEl = document.querySelector("#mcp-test-results");
    if (resultsEl) {
      resultsEl.innerHTML = '<p class="success">Authorization completed. Running Test…</p>';
    }
    try {
      await loadMcpMasterDetail();
      if (selectedServerId) {
        document.querySelector("#mcp-test-btn")?.click();
      }
    } catch (error) {
      if (resultsEl) resultsEl.innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
    }
  });
}
