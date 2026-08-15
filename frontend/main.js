/* ArchitectOS frontend entry point (native ES module).
 *
 * Replaces the old ordered list of classic <script> tags in index.html.
 * The imports below run in the same order the classic scripts used to load
 * (module evaluation follows the import graph depth-first; every cross-module
 * call happens at event time, so only top-level side effects care about order —
 * those live in app.js, which is why it stays last).
 *
 * index.html loads exactly this one file:
 *   <script type="module" src="/main.js"></script>
 * Module scripts are deferred by default, so the DOM (including the
 * <meta name="architectos-token"> tag the server injects) is fully parsed
 * before any module evaluates — same guarantee the end-of-body classic
 * scripts relied on. CSP script-src 'self' covers modules from this origin.
 */
import "./i18n.js";
import "./state.js";
import "./dom-utils.js";
import "./api-client.js";
import "./ui.js";
import "./mcp_master_detail.js";
import "./workspace_resizer.js";
import "./rich-response.js";
import "./file-find.js";
import "./search-palette.js";
import "./file-editor.js";
import "./project-wizard.js";
import "./workspace-chat.js";
import "./voice-memory.js";
import "./terminal.js";
import "./code-intel.js";
import "./memory-panel.js";
import "./memory-ingest.js";
import "./providers.js";
import "./settings.js";
import "./agent-activity.js";
import "./chat.js";
import "./graph.js";
import "./projects.js";
import "./ask-ui.js";
import "./file-tree.js";

import { fileEditor } from "./file-editor.js";
import { projectWizard } from "./project-wizard.js";
import { workspaceChat } from "./workspace-chat.js";
import "./app.js";

// Deliberate window bridge: the only globals this app still publishes.
// Kept for DevTools/console debugging and external poking (the pre-module
// code exposed these same three from app.js). Nothing inside the app reads
// them back — modules import each other directly. Do not add more.
window.fileEditor = fileEditor;
window.projectWizard = projectWizard;
window.workspaceChat = workspaceChat;
