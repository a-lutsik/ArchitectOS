/* Agent activity / tool-trace UI — extracted from app.js */
import { getMessageTextElement, setAgentActivityModel, setBubbleProvider } from "./chat.js";

const TOOL_ACTION_META = {
  memory_get: { verb: "Reading memory" },
  boards_search: { verb: "Searching Azure Boards" },
  boards_my_work: { verb: "Loading my work items" },
  boards_get_item: { verb: "Opening work item" },
  boards_list_comments: { verb: "Reading work item comments" },
  boards_query_wiql: { verb: "Running WIQL query" },
  granola_list_meetings: { verb: "Listing Granola meetings" },
  granola_get_meetings: { verb: "Reading Granola notes" },
  granola_get_transcript: { verb: "Fetching transcript" },
  fs_read: { verb: "Reading file" },
  fs_list: { verb: "Listing directory" },
  fs_search: { verb: "Searching files" },
  fs_write: { verb: "Writing file" },
};
const PLUMBING_PHASES = new Set(["provider", "thinking", "thinking_done", "request"]);
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
  const meta = TOOL_ACTION_META[name] || { verb: friendlyToolVerb(name) };
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
  return { label: detail ? `${meta.verb} ${detail}` : meta.verb };
}
function humanizeProgressEvent(event) {
  const toolName = String(event?.tool_name || "").trim();
  if (toolName) return humanizeToolAction(toolName, event.arguments);
  const phase = String(event?.phase || "");
  const status = String(event?.status || "").trim();
  if (phase === "context" || phase === "context_done") return { label: status || "Searching project memory" };
  if (status) return { label: status };
  return { label: phase || "Thinking…" };
}
function isPlumbingEvent(event) {
  const phase = String(event?.phase || "");
  if (PLUMBING_PHASES.has(phase)) return true;
  if (String(event?.tool_name || "").trim()) return false;
  const status = String(event?.status || "").toLowerCase();
  return /sending request|routing to|thinking/.test(status);
}
function isPlumbingTraceItem(item) {
  if (!item) return true;
  if (item.kind === "status") return PLUMBING_PHASES.has(String(item.phase || "")) || /sending request|routing to|thinking/.test(String(item.summary || item.status || "").toLowerCase());
  return false;
}
function humanizeTraceItem(item) {
  if (item && item.kind === "agent") {
    const name = item.role_name || item.role || item.name || "Agent";
    return { label: item.role === "synthesis" ? "Synthesizing" : name };
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
      <span class="agent-activity-spinner" aria-hidden="true"></span>
      <span class="agent-activity-title">Thinking…</span>
      <span class="agent-activity-caret" aria-hidden="true"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg></span>
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
function activityStepRow(panel, stepId, label) {
  const steps = panel.querySelector(".agent-activity-steps");
  const existing = stepId ? steps.querySelector(`[data-step="${CSS.escape(stepId)}"]`) : null;
  if (existing) return existing;
  const row = document.createElement("div");
  row.className = "agent-activity-step";
  if (stepId) row.dataset.step = stepId;
  row.dataset.startedAt = String(Date.now());
  row.innerHTML = `
    <span class="step-mark" aria-hidden="true"></span>
    <span class="step-body"><span class="step-label"></span><span class="step-detail"></span></span>
    <span class="step-time"></span>`;
  setActivityStepLabel(row, label);
  steps.appendChild(row);
  panel.classList.toggle("has-steps", steps.children.length > 0);
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
function runningStepLabel(panel) {
  const running = [...panel.querySelectorAll('.agent-activity-step[data-state="running"]')].at(-1);
  return String(running?.dataset.baseLabel || "").trim();
}
function refreshAgentActivityTitle(panel) {
  if (!panel) return;
  const title = panel.querySelector(".agent-activity-title");
  if (!title) return;
  const started = Number(panel.dataset.startedAt || 0);
  const elapsed = started ? formatActivityDuration((Date.now() - started) / 1000) : "";
  if (panel.classList.contains("running")) {
    title.textContent = runningStepLabel(panel) || "Thinking…";
    return;
  }
  title.textContent = elapsed ? `Thought for ${elapsed}` : "Thought";
}
function beginAgentActivity(bubble) {
  const panel = getAgentActivity(bubble);
  if (!panel) return panel;
  if (!panel.dataset.startedAt) panel.dataset.startedAt = String(Date.now());
  panel.classList.add("running");
  panel.classList.remove("done", "collapsed");
  panel.querySelector(".agent-activity-header")?.setAttribute("aria-expanded", "true");
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
  if (event.provider) {
    setBubbleProvider(bubble, event.provider);
    setAgentActivityModel(bubble, event.provider);
  }
  if (isPlumbingEvent(event) && !toolName) {
    refreshAgentActivityTitle(panel);
    return;
  }
  if (!toolName && !stepId && !event.status) return;
  const { label } = humanizeProgressEvent(event);
  const rowId = stepId || `step:${panel.querySelectorAll(".agent-activity-step").length}`;
  const row = activityStepRow(panel, rowId, label);
  const baseLabel = String(row.dataset.baseLabel || "");
  const donePhase = phase === "tool_finish" || phase.endsWith("_done");
  if (donePhase) {
    const entry = Array.isArray(event.tool_trace)
      ? [...event.tool_trace].reverse().find(item => String(item.step_id || "") === stepId)
      : null;
    let detail = "";
    if (entry) detail = entry.ok ? String(entry.summary || "") : String(entry.error || "failed");
    else if (label && label !== baseLabel) detail = label;
    else if (event.status && event.status !== baseLabel) detail = String(event.status);
    finishActivityStep(row, { ok: event.ok !== false, detail });
  } else {
    row.dataset.state = "running";
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
  panel.dataset.kind = "agent";
  const row = activityStepRow(panel, `council:${role}`, isJudge ? "Judge" : roleName);
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
      if (isPlumbingTraceItem(item)) return;
      const { label } = humanizeTraceItem(item);
      const row = activityStepRow(panel, String(item.step_id || `trace:${index}`), label);
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
  panel.querySelectorAll('.agent-activity-step[data-state="running"]').forEach(row => finishActivityStep(row, { ok: true }));
  stopAgentActivityTimer(panel);
  panel.classList.remove("running");
  panel.classList.add("done", "collapsed");
  panel.classList.toggle("has-steps", panel.querySelectorAll(".agent-activity-step").length > 0);
  panel.querySelector(".agent-activity-header").setAttribute("aria-expanded", "false");
  refreshAgentActivityTitle(panel);
}

export {
  beginAgentActivity, finalizeAgentActivity, refreshAgentActivityTitle,
  stopAgentActivityTimer, updateAgentActivity, updateCouncilActivity,
};
