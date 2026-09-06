/* Shared app state + core project/i18n helpers — extracted from app.js */
import { translations } from "./i18n.js";

const state = { projectId: "architectos", projects: [], chatId: "", activeRunId: "", selectedFile: "", language: "en", theme: "system", density: "comfortable", attachments: [], memoryFiles: [], terminalHistory: [], askMode: "quick", askMemoryAdvice: true, onboardingComplete: false, projectFilesStatus: "", lastFailedMessage: "", embeddingCatalog: [], showDotfiles: false, apiProviderReady: false };
const titleByView = { workspace: "view.workspace", chat: "view.chat", memory: "view.memory", tasks: "view.tasks", providers: "view.providers", mcp: "view.mcp", hooks: "view.hooks", code: "view.code", analytics: "view.analytics", settings: "view.settings" };
const SETUP_VIEWS = new Set(["providers", "mcp", "hooks", "code", "settings"]);
const ASK_MODES = ["quick", "council", "memory", "memory-mcp"];

function projectParam() { return encodeURIComponent(state.projectId || "architectos"); }
function currentProject() { return state.projects.find(project => project.id === state.projectId) || null; }
function isSystemWorkspace(project) { return Boolean(project && project.id === "architectos"); }
function displayProjectName(project) { return isSystemWorkspace(project) ? "System Workspace" : (project ? project.name : ""); }
function syncProjectSwitcherLabel() {
  const label = document.querySelector("#project-select-label");
  if (!label) return;
  label.textContent = isSystemWorkspace(currentProject()) ? "Workspace" : "Project";
}
function syncProjectTerminology() {
  const system = isSystemWorkspace(currentProject());
  const filesTitle = document.querySelector("#workspace-files-title");
  const scanButton = document.querySelector("#scan-project");
  if (filesTitle) filesTitle.textContent = system ? "Workspace Files" : t("workspace.projectFiles");
  if (scanButton) scanButton.textContent = system ? "Scan Workspace" : t("action.scanProject");
}
function t(key) { return (translations[state.language] && translations[state.language][key]) || translations.en[key] || key; }

export {
  state, titleByView, SETUP_VIEWS, ASK_MODES,
  projectParam, currentProject, isSystemWorkspace, displayProjectName,
  syncProjectSwitcherLabel, syncProjectTerminology, t,
};
