/* IntelliJ-style Settings: secondary nav tree + one content pane. */
const FORM_PAGES = new Set([
  "interface",
  "memory-lifecycle",
  "memory-sources",
  "chat-memory",
  "retention",
]);

const PAGE_META = {
  interface: {
    title: "Interface",
    subtitle: "Appearance only — theme, density, and language.",
  },
  "memory-lifecycle": {
    title: "Memory & lifecycle",
    subtitle: "Master switches for capturing knowledge and keeping it fresh.",
  },
  "memory-sources": {
    title: "Memory sources",
    subtitle: "Edit connections, schedules, and tests for AutoScan bindings.",
  },
  "chat-memory": {
    title: "Chat memory",
    subtitle: "How Ask conversations become memory candidates.",
  },
  retention: {
    title: "Retention",
    subtitle: "Decay, archive, delete, and promote rules for memories.",
  },
  embeddings: {
    title: "Embeddings",
    subtitle: "Retrieval tuning and the vector engine for Ask and Search.",
  },
  "export-import": {
    title: "Export / Import",
    subtitle: "Move your full ArchitectOS profile between machines.",
  },
  security: {
    title: "Security Preview",
    subtitle: "Dry-run the secret/risk scanner before sending text to a model.",
  },
};

let currentPage = "interface";

function pages() {
  return Array.from(document.querySelectorAll("#settings-pages [data-settings-page]"));
}

function navItems() {
  return Array.from(document.querySelectorAll("#settings-nav-tree [data-settings-nav]"));
}

function selectSettingsPage(pageId, { focusNav = false } = {}) {
  if (pageId === "connections") pageId = "memory-sources";
  if (!PAGE_META[pageId]) pageId = "interface";
  currentPage = pageId;

  pages().forEach(el => {
    const match = el.getAttribute("data-settings-page") === pageId;
    el.hidden = !match;
    el.classList.toggle("is-active", match);
  });

  navItems().forEach(btn => {
    const match = btn.getAttribute("data-settings-nav") === pageId;
    btn.classList.toggle("is-active", match);
    btn.setAttribute("aria-selected", match ? "true" : "false");
    if (match && focusNav) btn.focus();
  });

  const meta = PAGE_META[pageId];
  const title = document.querySelector("#settings-page-title");
  const subtitle = document.querySelector("#settings-page-subtitle");
  if (title) title.textContent = meta.title;
  if (subtitle) subtitle.textContent = meta.subtitle;

  const formActions = document.querySelector("[data-settings-form-actions]");
  if (formActions) formActions.hidden = !FORM_PAGES.has(pageId);

  const main = document.querySelector(".settings-main");
  if (main) main.scrollTop = 0;
}

function filterSettingsNav(query) {
  const q = String(query || "").trim().toLowerCase();
  const groups = Array.from(document.querySelectorAll("#settings-nav-tree .settings-nav-group"));
  const items = navItems();

  items.forEach(btn => {
    if (!q) {
      btn.hidden = false;
      return;
    }
    const label = (btn.textContent || "").toLowerCase();
    const keywords = (btn.getAttribute("data-settings-keywords") || "").toLowerCase();
    btn.hidden = !(label.includes(q) || keywords.includes(q));
  });

  groups.forEach(group => {
    let next = group.nextElementSibling;
    let anyVisible = false;
    while (next && !next.classList.contains("settings-nav-group")) {
      if (next.matches("[data-settings-nav]") && !next.hidden) anyVisible = true;
      next = next.nextElementSibling;
    }
    group.hidden = q ? !anyVisible : false;
  });
}

function bindSettingsNav() {
  const tree = document.querySelector("#settings-nav-tree");
  const filter = document.querySelector("#settings-nav-filter");
  if (!tree || tree.dataset.bound === "1") return;
  tree.dataset.bound = "1";

  tree.addEventListener("click", event => {
    const btn = event.target.closest("[data-settings-nav]");
    if (!btn || !tree.contains(btn)) return;
    selectSettingsPage(btn.getAttribute("data-settings-nav"));
  });

  tree.addEventListener("keydown", event => {
    const visible = navItems().filter(el => !el.hidden);
    const active = document.activeElement;
    const idx = visible.indexOf(active);
    if (idx < 0) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = event.key === "ArrowDown"
        ? visible[Math.min(idx + 1, visible.length - 1)]
        : visible[Math.max(idx - 1, 0)];
      next?.focus();
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const id = active.getAttribute("data-settings-nav");
      if (id) selectSettingsPage(id);
    } else if (event.key === "Home") {
      event.preventDefault();
      visible[0]?.focus();
    } else if (event.key === "End") {
      event.preventDefault();
      visible[visible.length - 1]?.focus();
    }
  });

  if (filter) {
    filter.addEventListener("input", () => filterSettingsNav(filter.value));
  }

  selectSettingsPage(currentPage);
}

bindSettingsNav();

export { selectSettingsPage, filterSettingsNav };
