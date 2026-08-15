// File find (name/content) modal. ES module.
import { api } from "./api-client.js";
import { escapeHtml, on } from "./dom-utils.js";
import { fileEditor } from "./file-editor.js";
import { openProjectFile, runSearch } from "./projects.js";
import { projectParam } from "./state.js";
import { showError } from "./ui.js";

const fileFind = {
  isOpen: false,
  mode: "name",
  hits: [],
  selectedIndex: 0,
  timer: 0,
  requestSeq: 0,

  el() {
    return document.querySelector("#fileFindModal");
  },

  queryEl() {
    return document.querySelector("#file-find-query");
  },

  maskEl() {
    return document.querySelector("#file-find-mask");
  },

  open(mode = "name") {
    const modal = this.el();
    if (!modal) return;
    this.mode = mode === "content" ? "content" : "name";
    this.syncModeUi();
    modal.classList.add("active");
    modal.setAttribute("aria-hidden", "false");
    this.isOpen = true;
    const input = this.queryEl();
    if (input) {
      input.focus();
      input.select();
    }
    this.scheduleSearch(true);
  },

  close() {
    const modal = this.el();
    if (!modal) return;
    modal.classList.remove("active");
    modal.setAttribute("aria-hidden", "true");
    this.isOpen = false;
    clearTimeout(this.timer);
  },

  toggle(mode = "name") {
    if (this.isOpen && this.mode === mode) {
      this.close();
      return;
    }
    this.open(mode);
  },

  setMode(mode) {
    this.mode = mode === "content" ? "content" : "name";
    this.syncModeUi();
    this.scheduleSearch(true);
    this.queryEl()?.focus();
  },

  syncModeUi() {
    const title = document.querySelector("#file-find-title");
    const label = document.querySelector("#file-find-query-label");
    const input = this.queryEl();
    document.querySelectorAll("[data-file-find-mode]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.fileFindMode === this.mode);
    });
    if (title) title.textContent = this.mode === "content" ? "Find in Files" : "Find File";
    if (label) label.textContent = this.mode === "content" ? "Text to find" : "Name";
    if (input) {
      input.placeholder = this.mode === "content" ? "Enter text to find…" : "Enter file name…";
    }
  },

  scheduleSearch(immediate = false) {
    clearTimeout(this.timer);
    const run = () => this.runSearch().catch(err => {
      const meta = document.querySelector("#file-find-meta");
      if (meta) meta.textContent = err.message || String(err);
    });
    if (immediate) {
      run();
      return;
    }
    this.timer = window.setTimeout(run, this.mode === "content" ? 280 : 160);
  },

  async runSearch() {
    const query = (this.queryEl()?.value || "").trim();
    const mask = (this.maskEl()?.value || "").trim();
    const results = document.querySelector("#file-find-results");
    const meta = document.querySelector("#file-find-meta");
    if (!results) return;

    if (!query && !(this.mode === "name" && mask)) {
      this.hits = [];
      this.selectedIndex = 0;
      results.innerHTML = `<div class="file-find-empty">${this.mode === "content" ? "Enter text to search in project files." : "Enter a file name or a file mask."}</div>`;
      if (meta) meta.textContent = "Type to search. Use ↑↓ and Enter.";
      return;
    }

    const seq = ++this.requestSeq;
    if (meta) meta.textContent = this.mode === "content" ? "Searching contents…" : "Searching files…";
    const payload = await api(
      `/api/project/search?project_id=${projectParam()}&q=${encodeURIComponent(query)}&mode=${encodeURIComponent(this.mode)}&mask=${encodeURIComponent(mask)}&limit=80`
    );
    if (seq !== this.requestSeq || !this.isOpen) return;

    this.hits = Array.isArray(payload.hits) ? payload.hits : [];
    this.selectedIndex = 0;
    if (meta) {
      const maskHint = mask ? ` · mask ${mask}` : "";
      meta.textContent = this.hits.length
        ? `${this.hits.length} result${this.hits.length === 1 ? "" : "s"}${maskHint}`
        : `No matches${maskHint}`;
    }
    this.renderHits();
  },

  renderHits() {
    const results = document.querySelector("#file-find-results");
    if (!results) return;
    results.innerHTML = "";
    if (!this.hits.length) {
      results.innerHTML = `<div class="file-find-empty">No matches found.</div>`;
      return;
    }
    this.hits.forEach((hit, index) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `file-find-hit${index === this.selectedIndex ? " selected" : ""}`;
      btn.setAttribute("role", "option");
      btn.dataset.index = String(index);
      const lineHint = hit.line ? `:${hit.line}` : "";
      btn.innerHTML = `
        <span class="file-find-hit-name">${escapeHtml(hit.name || hit.path || "")}${escapeHtml(lineHint)}</span>
        <span class="file-find-hit-path">${escapeHtml(hit.path || "")}</span>
        ${this.mode === "content" && hit.snippet ? `<span class="file-find-hit-snippet">${escapeHtml(hit.snippet)}</span>` : ""}
      `;
      btn.addEventListener("mouseenter", () => {
        this.selectedIndex = index;
        this.syncSelection();
      });
      btn.addEventListener("click", () => this.openHit(hit));
      results.appendChild(btn);
    });
    this.ensureSelectedVisible();
  },

  syncSelection() {
    const results = document.querySelector("#file-find-results");
    if (!results) return;
    results.querySelectorAll(".file-find-hit").forEach((el, index) => {
      el.classList.toggle("selected", index === this.selectedIndex);
    });
    this.ensureSelectedVisible();
  },

  ensureSelectedVisible() {
    const results = document.querySelector("#file-find-results");
    const selected = results?.querySelector(".file-find-hit.selected");
    selected?.scrollIntoView({ block: "nearest" });
  },

  navigate(direction) {
    if (!this.hits.length) return;
    const delta = direction === "down" ? 1 : -1;
    this.selectedIndex = (this.selectedIndex + delta + this.hits.length) % this.hits.length;
    this.syncSelection();
  },

  openSelected() {
    const hit = this.hits[this.selectedIndex];
    if (hit) this.openHit(hit);
  },

  openHit(hit) {
    if (!hit?.path) return;
    this.close();
    openProjectFile(hit.path).catch(showError);
    fileEditor.openFile(hit.path);
  },

  bindEvents() {
    on("#open-find-file", "click", () => this.open("name"));
    on("#open-find-in-files", "click", () => this.open("content"));
    document.querySelectorAll("[data-close-file-find]").forEach(el => {
      el.addEventListener("click", () => this.close());
    });
    document.querySelectorAll("[data-file-find-mode]").forEach(btn => {
      btn.addEventListener("click", () => this.setMode(btn.dataset.fileFindMode || "name"));
    });
    on("#file-find-query", "input", () => this.scheduleSearch());
    on("#file-find-mask", "input", () => this.scheduleSearch());
    document.addEventListener("keydown", event => {
      const isMod = event.metaKey || event.ctrlKey;
      const key = String(event.key || "").toLowerCase();

      if (isMod && event.shiftKey && (key === "o" || key === "n")) {
        event.preventDefault();
        this.toggle("name");
        return;
      }
      if (isMod && event.shiftKey && key === "f") {
        event.preventDefault();
        this.toggle("content");
        return;
      }
      if (!this.isOpen) return;

      if (event.key === "Escape") {
        event.preventDefault();
        this.close();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        this.navigate("down");
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        this.navigate("up");
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        this.openSelected();
      }
    });
  }
};

export { fileFind };
