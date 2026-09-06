/* Projects, onboarding, workspace files, search/context, tasks — extracted from app.js */
import { api, ensureServerOnline } from "./api-client.js";
import { escapeHtml, setElementValue, setTextContent, showSnackbar } from "./dom-utils.js";
import { fileEditor } from "./file-editor.js";
import { fileFind } from "./file-find.js";
import { dropTreeView, renderFileTree, renderFileTreeSkeleton } from "./file-tree.js";
import { graphState, loadGraph, resizeGraphCanvas, resetGraphAutoLayoutFlags, applyGraphAutoLayout, wakeGraphAnimation } from "./graph.js";
import { projectWizard } from "./project-wizard.js";
import {
  currentProject, displayProjectName, isSystemWorkspace, projectParam,
  state, syncProjectSwitcherLabel, syncProjectTerminology, t,
} from "./state.js";
import { renderResults, saveUiSettings, saveWorkspaceSettings, showError } from "./ui.js";

async function loadProjects() {
  const payload = await api("/api/projects");
  state.projects = Array.isArray(payload.projects) ? payload.projects : [];
  const realProject = state.projects.find(project => project.id !== "architectos" && project.root_path);
  const savedProject = state.projects.find(project => project.id === state.projectId);
  if (!savedProject && state.projects.length) {
    state.projectId = (realProject || state.projects[0]).id;
    saveWorkspaceSettings({ current_project_id: state.projectId }).catch(showError);
  }
  const select = document.querySelector("#project-select");
  if (!select) {
    syncProjectFields();
    return;
  }
  select.innerHTML = "";

  // Add placeholder option
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select project...";
  placeholder.disabled = true;
  placeholder.selected = !state.projectId;
  select.appendChild(placeholder);

  for (const project of state.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = displayProjectName(project);
    select.appendChild(option);
  }
  select.value = state.projectId;
  if (!select.value && state.projects.length) {
    state.projectId = (realProject || state.projects[0]).id;
    select.value = state.projectId;
  }
  syncProjectSwitcherLabel();
  syncProjectTerminology();
  syncProjectFields();
}
function syncProjectFields() {
  const project = currentProject();
  const name = document.querySelector("#project-name");
  const root = document.querySelector("#project-root-path");
  const summaryName = document.querySelector("#project-folder-summary-name");
  const summaryPath = document.querySelector("#project-folder-summary-path");
  const currentProjectName = document.querySelector("#current-project-name");

  if (name) name.value = displayProjectName(project);
  if (root) root.value = project ? project.root_path || "" : "";
  if (summaryName) summaryName.textContent = displayProjectName(project) || "No workspace";
  if (summaryPath) summaryPath.textContent = project && project.root_path ? project.root_path : "No folder selected";

  // Update workspace tree project name
  if (currentProjectName) {
    currentProjectName.textContent = displayProjectName(project) || "No project selected";
  }
}
let folderSubmitting = false;

function projectFolderModal() {
  return document.querySelector("#projectFolderModal");
}
function folderModalIsOpen() {
  const modal = projectFolderModal();
  return Boolean(modal && modal.classList.contains("active"));
}
function setFolderSubmitError(message) {
  const errorEl = document.getElementById("project-folder-submit-error");
  if (!errorEl) return;
  if (message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
  } else {
    errorEl.textContent = "";
    errorEl.hidden = true;
  }
}
function setFolderSubmitting(submitting) {
  folderSubmitting = Boolean(submitting);
  const saveBtn = document.querySelector("#save-project");
  const initBtn = document.querySelector("#init-project");
  const browseBtn = document.querySelector("#browse-project-folder");
  if (saveBtn) {
    saveBtn.disabled = folderSubmitting;
    saveBtn.setAttribute("aria-busy", folderSubmitting ? "true" : "false");
    saveBtn.textContent = folderSubmitting ? t("folder.connecting") : t("action.connectFolder");
  }
  if (initBtn) {
    initBtn.disabled = folderSubmitting;
    initBtn.setAttribute("aria-busy", folderSubmitting ? "true" : "false");
    initBtn.textContent = folderSubmitting ? t("folder.connecting") : t("action.connectAndIndex");
  }
  if (browseBtn) browseBtn.disabled = folderSubmitting;
}
function openProjectFolderModal() {
  const modal = projectFolderModal();
  if (!modal) return;
  syncProjectFields();
  setFolderSubmitError("");
  setFolderSubmitting(false);
  setProjectFolderStatus("");
  modal.classList.add("active");
  modal.setAttribute("aria-hidden", "false");
  document.querySelector("#project-root-path")?.focus();
}
function closeProjectFolderModal() {
  const modal = projectFolderModal();
  if (!modal) return;
  modal.classList.remove("active");
  modal.setAttribute("aria-hidden", "true");
  setFolderSubmitting(false);
}
function setProjectFolderStatus(message, tone = "") {
  const status = document.querySelector("#project-folder-picker-status");
  if (!status) return;
  status.className = `provider-test ${tone}`.trim();
  status.textContent = message;
}
function setWizardFolderStatus(message, tone = "") {
  const status = document.querySelector("#wizard-folder-status");
  if (!status) return;
  status.className = `wizard-inline-status ${tone}`.trim();
  status.textContent = message;
}
async function pickProjectFolder({ initialPath = "", onStatus } = {}) {
  if (!(await ensureServerOnline())) {
    const message = t("error.serverUnreachable");
    if (onStatus) onStatus(message, "error");
    throw new Error(message);
  }
  if (onStatus) onStatus(t("wizard.pickerOpening"));
  let payload;
  try {
    payload = await api("/api/system/folder-picker", {
      method: "POST",
      body: JSON.stringify({ initial_path: initialPath }),
    });
  } catch (error) {
    if (onStatus) onStatus(error.message || t("wizard.pickerUnavailable"), "error");
    throw error;
  }
  if (!payload.supported) {
    const message = payload.message || t("wizard.pickerUnavailable");
    if (onStatus) onStatus(message, "error");
    return null;
  }
  if (payload.cancelled || !payload.path) {
    if (onStatus) onStatus(t("wizard.pickerCancelled"));
    return null;
  }
  if (onStatus) onStatus(t("wizard.pickerSelected"), "ok");
  return payload.path;
}
async function browseProjectFolder() {
  const root = document.querySelector("#project-root-path");
  const name = document.querySelector("#project-name");
  const path = await pickProjectFolder({
    initialPath: root?.value || "",
    onStatus: setProjectFolderStatus,
  });
  if (!path) return;
  if (root) root.value = path;
  const folderName = path.split(/[\\/]/).filter(Boolean).pop() || "Project";
  if (name && (!name.value.trim() || name.value.trim() === "System Workspace")) name.value = folderName;
}
async function markOnboardingComplete() {
  state.onboardingComplete = true;
  await saveUiSettings({ onboarding_complete: true });
}
function needsProjectOnboarding() {
  if (state.projectFilesStatus === "no_root" || state.projectFilesStatus === "missing_root") return true;
  const project = currentProject();
  if (!project) return true;
  return isSystemWorkspace(project) && !project.root_path;
}
async function maybeShowOnboarding() {
  if (state.onboardingComplete) return;
  if (!needsProjectOnboarding()) {
    await markOnboardingComplete();
    return;
  }
  if (typeof projectWizard !== "undefined" && projectWizard.openWizard) {
    projectWizard.openWizard({ firstRun: true });
    return;
  }
  openProjectFolderModal();
}
function resetGraphFilters() {
  document.querySelectorAll("#graph-task-filter,#graph-provider-filter").forEach(filter => { if (filter) delete filter.dataset.ready; });
  graphState.searchQuery = "";
  graphState.neighborhoodId = "";
  graphState.groupFilter = "";
  resetGraphAutoLayoutFlags();
  setElementValue("#graph-search", "");
  setElementValue("#graph-group-filter", "");
  // Restore auto defaults for the current stage once nodes load; seed sensible placeholders now.
  applyGraphAutoLayout();
}
async function connectProjectFromFolder({ index = false } = {}) {
  if (folderSubmitting) return;
  const current = currentProject();
  const root = (document.querySelector("#project-root-path")?.value || current?.root_path || "").trim();
  const name = (document.querySelector("#project-name")?.value || current?.name || "").trim() || root.split(/[\\/]/).filter(Boolean).pop() || "Project";
  if (!root) {
    setFolderSubmitError(t("folder.validation"));
    return;
  }

  setFolderSubmitError("");
  setFolderSubmitting(true);

  try {
    const project = await api("/api/projects", { method: "POST", body: JSON.stringify({ name, root_path: root }) });
    state.projectId = project.project_id || project.id || (project.project && project.project.id);
    if (!state.projectId) throw new Error(t("error.invalidResponse"));
    state.selectedFile = "";
    const projectNameEl = document.getElementById("current-project-name");
    if (projectNameEl) projectNameEl.textContent = name;

    closeProjectFolderModal();

    const successMessage = index
      ? t("folder.connectedIndexing").replace("{name}", name)
      : (project.message || t("folder.connected").replace("{name}", name));
    showSnackbar(successMessage, project.created === false ? "info" : "success");

    const followUp = async () => {
      await saveWorkspaceSettings({ current_project_id: state.projectId });
      await markOnboardingComplete();
      await loadProjects();
      await refreshWorkspace();
      await loadProjectFiles();
      if (!index) return;
      try {
        await runProjectIndex({ reindex: true, limit: 80, label: "Initial index" });
      } catch (scanError) {
        console.warn("Auto-index after connect failed:", scanError);
        showSnackbar(t("folder.indexFailed"), "error");
      }
    };
    followUp().catch(showError);
    return project;
  } catch (error) {
    setFolderSubmitting(false);
    const message = error && error.message ? error.message : t("error.unexpected");
    if (folderModalIsOpen()) setFolderSubmitError(message);
    else showError(error);
  }
}
async function saveProjectFromFolder() {
  return connectProjectFromFolder({ index: false });
}
async function initProjectFromFolder() {
  return connectProjectFromFolder({ index: true });
}
async function scanSelectedProject() {
  return runProjectIndex({ reindex: false, limit: 40, label: "Analyze" });
}
async function reindexSelectedProject() {
  return runProjectIndex({ reindex: true, limit: 120, label: "Reindex" });
}
async function runProjectIndex({ reindex = false, limit = 40, label = "Analyze" } = {}) {
  const current = currentProject();
  const root = (document.querySelector("#project-root-path")?.value || current?.root_path || "").trim();
  const name = (document.querySelector("#project-name")?.value || current?.name || "").trim();
  const payload = { project_id: state.projectId, root_path: root, name, limit, reindex, rebuild_links: reindex };
  setTextContent("#context-output", `${label} running...`);
  const result = await api("/api/project/scan", { method: "POST", body: JSON.stringify(payload) });
  state.projectId = result.project_id || state.projectId;
  state.selectedFile = "";
  resetGraphFilters();
  await loadProjects();
  const linked = result.links ? `, ${result.links.created || 0} new link(s)` : "";
  const archived = result.archived ? `, ${result.archived} stale item(s) archived` : "";
  setTextContent("#context-output", `${label} complete: ${result.count} file memory item(s) from ${result.root}${archived}${linked}.`);
  await refreshWorkspace();
  await loadProjectFiles();
  await loadGraph();
  return result;
}
async function buildContext(query) {
  const payload = await api(`/api/context?query=${encodeURIComponent(query)}&project_id=${projectParam()}&limit=8`);
  setTextContent("#context-output", payload.context);
}
async function runSearch(query, targetId = "search-results") {
  const payload = await api(`/api/memory/search?query=${encodeURIComponent(query)}&project_id=${projectParam()}&limit=10&refresh=0`);
  renderResults(document.querySelector(`#${targetId}`), payload.hits);
}

function scheduleGraphLoad() {
  const memoryView = document.querySelector("#memory-view");
  if (!memoryView || !memoryView.classList.contains("active")) return;
  requestAnimationFrame(() => {
    resizeGraphCanvas();
    wakeGraphAnimation();
    requestAnimationFrame(() => loadGraph().catch(showError));
  });
}

function renderFileTreeEmptyState(list, payload) {
  dropTreeView();
  const status = payload.status || "";
  const configuredRoot = (payload.configured_root || "").trim();

  if (status === "missing_root" || status === "no_root") {
    const isMissing = status === "missing_root";
    const title = isMissing ? "Folder not available here" : "No folder connected";
    const message = isMissing
      ? "This project's folder can't be found on this computer. Update its path or connect another folder to load files and index memory."
      : "Connect a project folder to load its files and start building memory.";
    const primaryLabel = isMissing ? "Update folder" : "Connect folder";
    const pathLine = isMissing && configuredRoot
      ? `<code class="file-tree-empty-path">${escapeHtml(configuredRoot)}</code>`
      : "";

    list.innerHTML = `
      <div class="file-tree-empty file-tree-onboard">
        <div class="file-tree-onboard-icon">📁</div>
        <strong class="file-tree-onboard-title">${escapeHtml(title)}</strong>
        <p class="file-tree-onboard-text">${escapeHtml(message)}</p>
        ${pathLine}
        <div class="file-tree-onboard-actions">
          <button type="button" class="btn btn-primary" data-onboard-action="folder">${escapeHtml(primaryLabel)}</button>
          <button type="button" class="btn btn-secondary" data-onboard-action="new-project">New project</button>
        </div>
      </div>`;

    const folderBtn = list.querySelector('[data-onboard-action="folder"]');
    if (folderBtn) folderBtn.addEventListener("click", () => openProjectFolderModal());
    const newBtn = list.querySelector('[data-onboard-action="new-project"]');
    if (newBtn) newBtn.addEventListener("click", () => {
      if (typeof projectWizard !== "undefined" && projectWizard.openWizard) projectWizard.openWizard();
      else openProjectFolderModal();
    });
    return;
  }

  const message = payload.message || "No files found here yet. Use Refresh or connect a folder.";
  list.innerHTML = `<div class="file-tree-empty">${escapeHtml(message)}</div>`;
}

async function loadProjectFiles() {
  const list = document.querySelector("#file-list");
  if (!list) return;

  console.log('Loading files for project:', state.projectId);
  renderFileTreeSkeleton(list);

  let payload;
  try {
    payload = await api(`/api/project/files?project_id=${projectParam()}&limit=5000`);
  } catch (error) {
    dropTreeView();
    list.innerHTML = `<div class="file-tree-empty"><strong>Could not load files</strong><p>${escapeHtml(error.message || String(error))}</p></div>`;
    throw error;
  }

  const files = Array.isArray(payload.files) ? payload.files : [];
  state.projectFilesStatus = payload.status || (files.length ? "ok" : "");
  console.log('Loaded files:', files.length, 'files');

  if (!files.length) {
    renderFileTreeEmptyState(list, payload);
    return;
  }

  // Use file tree rendering
  renderFileTree(files, list, (file) => {
    openProjectFile(file.path).catch(showError);
    fileEditor.openFile(file.path);
  });
}


fileFind.bindEvents();
async function openProjectFile(path) {
  const payload = await api(`/api/project/file?project_id=${projectParam()}&path=${encodeURIComponent(path)}`);
  state.selectedFile = payload.path;
  setElementValue("#selected-file-path", payload.path);
  setElementValue("#file-preview", payload.readable === false ? (payload.message || "Preview unavailable for this file.") : payload.text);
  if (payload.readable === false) showSnackbar(payload.message || "Preview unavailable for this file.", "info");
}
async function buildSelectedFileContext() {
  if (!state.selectedFile) throw new Error("Select a project file first.");
  const query = document.querySelector("#context-query")?.value || "selected file context";
  const payload = await api("/api/project/context", { method: "POST", body: JSON.stringify({ project_id: state.projectId, path: state.selectedFile, query }) });
  setTextContent("#context-output", payload.context);
}
async function loadGitDiff() {
  const path = state.selectedFile || "";
  const payload = await api(`/api/project/git-diff?project_id=${projectParam()}${path ? `&path=${encodeURIComponent(path)}` : ""}`);
  setTextContent("#context-output", payload.diff || payload.message || "No git diff.");
}
async function refreshWorkspace() {
  const search = await api(`/api/memory/search?query=${encodeURIComponent("favorite memory decision")}&project_id=${projectParam()}&limit=12`);
  renderResults(document.querySelector("#workspace-favorites"), search.hits.filter(hit => hit.node.metadata && hit.node.metadata.favorite).slice(0, 5));
}

async function loadTasks() {
  const payload = await api(`/api/tasks?project_id=${projectParam()}`);
  const board = document.querySelector("#task-board");
  if (!board) return;
  board.innerHTML = "";
  for (const status of ["todo", "doing", "blocked", "done"]) {
    const column = document.createElement("section");
    column.className = "task-column";
    column.innerHTML = `<h4>${status}</h4><div class="task-column-cards" data-status="${status}"></div>`;
    const cards = column.querySelector(".task-column-cards");
    for (const task of payload.tasks.filter(item => item.status === status)) {
      const el = document.createElement("article");
      el.className = "task-card";
      el.innerHTML = `<strong>${escapeHtml(task.title)}</strong><p>${escapeHtml(task.detail)}</p><select data-task="${escapeHtml(task.id)}"><option>todo</option><option>doing</option><option>blocked</option><option>done</option></select>`;
      el.querySelector("select").value = task.status;
      cards.appendChild(el);
    }
    if (!cards.children.length) {
      const empty = document.createElement("div");
      empty.className = "task-column-empty";
      empty.textContent = status === "done" ? "Nothing completed yet" : "No tasks";
      cards.appendChild(empty);
    }
    if (status === "todo") {
      const composer = document.createElement("form");
      composer.className = "task-inline-composer";
      composer.innerHTML = `
        <input class="task-inline-title" type="text" placeholder="+ Add a task" aria-label="New task title" required>
        <div class="task-inline-extra" hidden>
          <select class="task-inline-priority" aria-label="Priority">
            <option value="high">high</option>
            <option value="medium" selected>medium</option>
            <option value="low">low</option>
          </select>
          <textarea class="task-inline-detail" rows="2" placeholder="Optional detail"></textarea>
          <div class="task-inline-actions">
            <button type="submit" class="btn btn-primary btn-sm">Add</button>
            <button type="button" class="btn btn-secondary btn-sm" data-cancel-task>Cancel</button>
          </div>
        </div>`;
      const title = composer.querySelector(".task-inline-title");
      const extra = composer.querySelector(".task-inline-extra");
      title.addEventListener("focus", () => { extra.hidden = false; });
      composer.querySelector("[data-cancel-task]").addEventListener("click", () => {
        composer.reset();
        extra.hidden = true;
      });
      composer.addEventListener("submit", async (event) => {
        event.preventDefault();
        const value = title.value.trim();
        if (!value) return;
        await api("/api/tasks", {
          method: "POST",
          body: JSON.stringify({
            project_id: state.projectId,
            title: value,
            priority: composer.querySelector(".task-inline-priority").value || "medium",
            status: "todo",
            detail: composer.querySelector(".task-inline-detail").value || "",
          }),
        });
        await loadTasks();
        await refreshWorkspace();
      });
      column.appendChild(composer);
    }
    board.appendChild(column);
  }
  board.querySelectorAll("[data-task]").forEach(select => select.addEventListener("change", async () => { await api(`/api/tasks/${select.dataset.task}`, { method: "PATCH", body: JSON.stringify({ status: select.value }) }); await loadTasks(); await refreshWorkspace(); }));
}

export {
  browseProjectFolder, buildContext, buildSelectedFileContext, closeProjectFolderModal,
  initProjectFromFolder, loadGitDiff, loadProjectFiles, loadProjects, loadTasks,
  markOnboardingComplete, maybeShowOnboarding, needsProjectOnboarding, openProjectFile,
  openProjectFolderModal, pickProjectFolder, projectFolderModal, refreshWorkspace,
  resetGraphFilters, runSearch, saveProjectFromFolder, scheduleGraphLoad,
  setProjectFolderStatus, setWizardFolderStatus, syncProjectFields,
};
