/* Agent activity / tool-trace UI — extracted from app.js */
import { getMessageTextElement, providerLabel, setAgentActivityModel, setBubbleProvider } from "./chat.js";

const TOOL_ACTION_META = {
  memory_get: { icon: "🧠", verb: "Reading memory node" },
  boards_search: { icon: "🔎", verb: "Searching Azure Boards" },
  boards_my_work: { icon: "📋", verb: "Loading my work items" },
  boards_get_item: { icon: "📄", verb: "Opening work item" },
  boards_list_comments: { icon: "💬", verb: "Reading work item comments" },
  boards_query_wiql: { icon: "🧮", verb: "Running WIQL query" },
  granola_list_meetings: { icon: "📝", verb: "Listing Granola meetings" },
  granola_get_meetings: { icon: "🗒️", verb: "Reading Granola meeting notes" },
  granola_get_transcript: { icon: "🎙️", verb: "Fetching Granola transcript" },
  fs_read: { icon: "📁", verb: "Reading file" },
  fs_list: { icon: "🗂️", verb: "Listing directory" },
  fs_search: { icon: "🔍", verb: "Searching files" },
  fs_write: { icon: "✏️", verb: "Writing file" },
};
function friendlyToolVerb(name) {
  const raw = String(name || "").trim();
  if (!raw) return "Working";
  const words = raw.replace(/^(fs|boards|granola|memory)_/, "").replace(/_/g, " ").trim();
  if (!words) return "Working";
  return words.charAt(0).toUpperCase() + words.slice(1);
}
function formatActivityDuration(seconds) {
  const sec = Math.max(0, Math.floor(Number(seconds) || 0));
  if (sec < 60) return `${sec}s`;
  const minutes = Math.floor(sec / 60);
  const rem = sec % 60;
  if (minutes < 60) return rem ? `${minutes}m ${rem}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours}h ${mins}m` : `${hours}h`;
}
function humanizeToolAction(name, args) {
  const meta = TOOL_ACTION_META[name] || { icon: "⚙️", verb: friendlyToolVerb(name) };
  const a = args || {};
  let detail = "";
  if (name === "memory_get") detail = a.id || a.node_id || "";
  else if (name === "boards_search" || name === "fs_search") detail = a.query || a.text || "";
  else if (name === "boards_get_item" || name === "boards_list_comments") detail = a.id || a.work_item_id || a.item_id || "";
  else if (name === "fs_read" || name === "fs_write" || name === "fs_list") detail = a.path || a.file || "";
  else {
    const first = Object.values(a).find(v => typeof v === "string" && v);
    detail = first || "";
  }
  detail = String(detail).slice(0, 80);
  return { icon: meta.icon, label: detail ? `${meta.verb} ${detail}` : meta.verb };
}
function humanizeProgressEvent(event) {
  const toolName = String(event?.tool_name || "").trim();
  if (toolName) return humanizeToolAction(toolName, event.arguments);
  const phase = String(event?.phase || "");
  const status = String(event?.status || "").trim();
  if (phase === "provider") return { icon: "🤖", label: status || "Model selected" };
  if (phase === "context" || phase === "context_done") return { icon: "📚", label: status || "Searching memory" };
  if (phase === "thinking" || phase === "thinking_done") return { icon: "💭", label: status || "Thinking…" };
  if (status) return { icon: "⚙️", label: status };
  return { icon: "⚙️", label: phase || "Working…" };
}
function humanizeTraceItem(item) {
  if (item && item.kind === "agent") {
    const name = item.role_name || item.role || item.name || "Agent";
    return { icon: item.role === "synthesis" ? "🧩" : "🤖", label: `${name} agent` };
  }
  if (item && item.kind === "status") {
    return humanizeProgressEvent({ phase: item.phase, status: item.summary || item.status, tool_name: "" });
  }
  return humanizeToolAction(item && item.name, item && item.arguments);
}
function getAgentActivity(bubble) {
  if (!bubble) return null;
  let panel = bubble.querySelector(".agent-activity");
  if (panel) return panel;
  panel = document.createElement("div");
  panel.className = "agent-activity running";
  panel.innerHTML = `
    <button type="button" class="agent-activity-header" aria-expanded="true">
      <span class="agent-activity-spinner"></span>
      <span class="agent-activity-title">Working…</span>
      <span class="agent-activity-caret">▾</span>
    </button>
    <div class="agent-activity-steps"></div>`;
  panel.querySelector(".agent-activity-header").addEventListener("click", () => {
    const collapsed = panel.classList.toggle("collapsed");
    panel.querySelector(".agent-activity-header").setAttribute("aria-expanded", String(!collapsed));
  });
  const textNode = getMessageTextElement(bubble);
  bubble.insertBefore(panel, textNode);
  return panel;
}
function activityStepRow(panel, stepId, icon, label) {
  const steps = panel.querySelector(".agent-activity-steps");
  const existing = stepId ? steps.querySelector(`[data-step="${CSS.escape(stepId)}"]`) : null;
  if (existing) return existing;
  const row = document.createElement("div");
  row.className = "agent-activity-step";
  if (stepId) row.dataset.step = stepId;
  row.dataset.startedAt = String(Date.now());
  row.innerHTML = `
    <span class="step-icon"></span>
    <span class="step-body"><span class="step-label"></span><span class="step-detail"></span></span>
    <span class="step-time"></span>
    <span class="step-status"></span>`;
  row.querySelector(".step-icon").textContent = icon || "⚙️";
  setActivityStepLabel(row, label);
  steps.appendChild(row);
  return row;
}
function setActivityStepLabel(row, label) {
  const text = String(label || "").trim();
  if (!text) return;
  row.dataset.baseLabel = text;
  row.querySelector(".step-label").textContent = text;
}
function setActivityStepDetail(row, detail) {
  row.querySelector(".step-detail").textContent = String(detail || "").trim().slice(0, 180);
}
function finishActivityStep(row, options = {}) {
  const ok = options.ok !== false;
  row.dataset.state = ok ? "ok" : "error";
  row.querySelector(".step-status").textContent = ok ? "✓" : "✗";
  if (options.detail) setActivityStepDetail(row, options.detail);
  refreshActivityStepTime(row, true);
}
function refreshActivityStepTime(row, freeze = false) {
  if (row.dataset.timeFrozen === "1") return;
  const started = Number(row.dataset.startedAt || 0);
  const node = row.querySelector(".step-time");
  if (!started || !node) return;
  const elapsed = (Date.now() - started) / 1000;
  node.textContent = elapsed >= 1 ? formatActivityDuration(elapsed) : "";
  if (freeze) row.dataset.timeFrozen = "1";
}
function refreshAgentActivitySteps(panel) {
  panel.querySelectorAll('.agent-activity-step[data-state="running"]').forEach(row => refreshActivityStepTime(row));
}
function openRequestStep(panel) {
  // The first server event can be seconds away, so never leave the panel empty.
  if (panel.dataset.requestStep) return;
  panel.dataset.requestStep = "open";
  activityStepRow(panel, "prep:request", "📨", "Sending request").dataset.state = "running";
}
function closeRequestStep(panel) {
  if (panel.dataset.requestStep !== "open") return;
  panel.dataset.requestStep = "done";
  const row = panel.querySelector('[data-step="prep:request"]');
  if (row) finishActivityStep(row, { ok: true });
}
function refreshAgentActivityTitle(panel) {
  if (!panel) return;
  const title = panel.querySelector(".agent-activity-title");
  if (!title) return;
  const started = Number(panel.dataset.startedAt || 0);
  const elapsed = started ? formatActivityDuration((Date.now() - started) / 1000) : "";
  const model = String(panel.dataset.modelLabel || "").trim();
  if (panel.classList.contains("running")) {
    if (model && elapsed) title.textContent = `${model} · ${elapsed}`;
    else if (model) title.textContent = model;
    else title.textContent = elapsed ? `Working… ${elapsed}` : "Working…";
    return;
  }
  const count = panel.querySelectorAll(".agent-activity-step").length;
  const hasAgentTrace = panel.dataset.kind === "agent";
  const base = count
    ? hasAgentTrace
      ? `Ran ${count} agent step${count === 1 ? "" : "s"}`
      : `Used ${count} step${count === 1 ? "" : "s"}`
    : "Worked";
  title.textContent = [base, model, elapsed].filter(Boolean).join(" · ");
}
function beginAgentActivity(bubble) {
  const panel = getAgentActivity(bubble);
  if (!panel) return panel;
  if (!panel.dataset.startedAt) panel.dataset.startedAt = String(Date.now());
  panel.classList.add("running");
  panel.classList.remove("done", "collapsed");
  panel.querySelector(".agent-activity-header")?.setAttribute("aria-expanded", "true");
  openRequestStep(panel);
  if (panel._elapsedTimer) clearInterval(panel._elapsedTimer);
  refreshAgentActivityTitle(panel);
  panel._elapsedTimer = setInterval(() => {
    // Bubble removed from the DOM (chat switched mid-stream) — stop ticking.
    if (!panel.isConnected) {
      stopAgentActivityTimer(panel);
      return;
    }
    refreshAgentActivityTitle(panel);
    refreshAgentActivitySteps(panel);
  }, 1000);
  return panel;
}
function stopAgentActivityTimer(panel) {
  if (!panel) return;
  if (panel._elapsedTimer) {
    clearInterval(panel._elapsedTimer);
    panel._elapsedTimer = null;
  }
}
function updateAgentActivity(bubble, event) {
  if (!bubble || !event) return;
  const phase = event.phase || (String(event.status || "").startsWith("Finished") ? "tool_finish" : "tool_start");
  const stepId = event.step_id != null ? String(event.step_id) : "";
  const toolName = String(event.tool_name || "").trim();
  if (!toolName && !stepId && !event.status && !event.provider) return;
  const panel = beginAgentActivity(bubble);
  closeRequestStep(panel);
  if (event.provider) {
    setBubbleProvider(bubble, event.provider);
    setAgentActivityModel(bubble, event.provider);
  }
  if (phase === "provider" && !toolName) {
    // Provider announcement is reflected in the header/persona; keep a compact step too.
    const { icon, label } = humanizeProgressEvent({
      phase: "provider",
      status: event.status || `Using ${providerLabel(event.provider)}`,
    });
    const text = label || providerLabel(event.provider) || "Model selected";
    const row = activityStepRow(panel, stepId || "prep:provider", icon === "⚙️" ? "🤖" : icon, text);
    // Routing announces itself first and names the model later, so this label may sharpen.
    setActivityStepLabel(row, text);
    finishActivityStep(row, { ok: true });
    refreshAgentActivityTitle(panel);
    return;
  }
  if (!toolName && !stepId && !event.status) return;
  const { icon, label } = humanizeProgressEvent(event);
  const rowId = stepId || `step:${panel.querySelectorAll(".agent-activity-step").length}`;
  const row = activityStepRow(panel, rowId, icon, label);
  const baseLabel = String(row.dataset.baseLabel || "");
  const donePhase = phase === "tool_finish" || phase.endsWith("_done");
  if (donePhase) {
    const entry = Array.isArray(event.tool_trace)
      ? [...event.tool_trace].reverse().find(item => String(item.step_id || "") === stepId)
      : null;
    // Keep the action on the label and report the outcome underneath, so the panel reads as a log.
    let detail = "";
    if (entry) detail = entry.ok ? String(entry.summary || "") : String(entry.error || "failed");
    else if (label && label !== baseLabel) detail = label;
    else if (event.status && event.status !== baseLabel) detail = String(event.status);
    finishActivityStep(row, { ok: event.ok !== false, detail });
  } else {
    row.dataset.state = "running";
    row.querySelector(".step-status").textContent = "";
    if (event.status && event.status !== baseLabel) setActivityStepDetail(row, event.status);
    refreshActivityStepTime(row);
  }
  refreshAgentActivityTitle(panel);
  bubble.scrollIntoView?.({ block: "nearest" });
}
function updateCouncilActivity(bubble, event) {
  const agent = event && event.agent;
  if (!bubble || !agent) return;
  const role = String(agent.role || "model");
  const roleName = String(agent.role_name || role || "Model");
  const isJudge = role === "judge";
  const done = String(agent.status || "").toLowerCase() === "done";
  const panel = beginAgentActivity(bubble);
  closeRequestStep(panel);
  panel.dataset.kind = "agent";
  const row = activityStepRow(panel, `council:${role}`, isJudge ? "⚖️" : "🤖", isJudge ? "Judge" : roleName);
  const text = String(agent.text || "").trim();
  if (done) {
    finishActivityStep(row, { ok: true, detail: text ? text.slice(0, 140) : "Responded" });
  } else {
    row.dataset.state = "running";
    setActivityStepDetail(row, String(event.status || "Running").replace(/\.\.\.$/, ""));
    refreshActivityStepTime(row);
  }
  refreshAgentActivityTitle(panel);
  bubble.scrollIntoView?.({ block: "nearest" });
}
function finalizeAgentActivity(bubble, trace) {
  if (!bubble) return;
  let panel = bubble.querySelector(".agent-activity");
  if (!panel && Array.isArray(trace) && trace.length) {
    panel = getAgentActivity(bubble);
    trace.forEach((item, index) => {
      const { icon, label } = humanizeTraceItem(item);
      const row = activityStepRow(panel, String(item.step_id || `trace:${index}`), icon, label);
      row.removeAttribute("data-started-at");
      if (item.pending === true) {
        row.dataset.state = "running";
        return;
      }
      finishActivityStep(row, {
        ok: item.ok !== false,
        detail: item.ok === false ? String(item.error || "failed") : String(item.summary || item.status || ""),
      });
    });
  }
  if (!panel) return;
  closeRequestStep(panel);
  // A step still marked running when the turn ends would spin forever.
  panel.querySelectorAll('.agent-activity-step[data-state="running"]').forEach(row => finishActivityStep(row, { ok: true }));
  stopAgentActivityTimer(panel);
  panel.classList.remove("running");
  panel.classList.add("done", "collapsed");
  panel.querySelector(".agent-activity-header").setAttribute("aria-expanded", "false");
  refreshAgentActivityTitle(panel);
}

export {
  beginAgentActivity, finalizeAgentActivity, refreshAgentActivityTitle,
  stopAgentActivityTimer, updateAgentActivity, updateCouncilActivity,
};
