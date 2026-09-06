// File editor module. ES module.
import { api } from "./api-client.js";
import { showAppConfirm, showAppPrompt } from "./app-dialog.js";
import { on } from "./dom-utils.js";
import { getFileExtension } from "./file-tree.js";
import { loadProjectFiles } from "./projects.js";
import { projectParam, state, t } from "./state.js";
import { showError } from "./ui.js";
import { workspaceChat } from "./workspace-chat.js";

const fileEditor = {
  currentFile: null,
  originalContent: '',
  isModified: false,
  openFiles: new Map(), // path -> { content, modified, element }

  init() {
    this.bindEvents();
    this.setupContextMenu();
    this.setupKeyboardShortcuts();
  },

  bindEvents() {
    // File open is handled on single click in renderFileTree onSelect

    // Editor textarea change
    const editor = document.getElementById('file-editor');
    if (editor) {
      editor.addEventListener('input', () => {
        this.markAsModified();
      });
    }

    // Save button
    const saveBtn = document.getElementById('editor-save');
    if (saveBtn) {
      saveBtn.addEventListener('click', () => this.saveFile());
    }

    // Close button
    const closeBtn = document.getElementById('editor-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', () => this.closeFile());
    }

    // New file button
    const newFileBtn = document.getElementById('new-file-btn');
    if (newFileBtn) {
      newFileBtn.addEventListener('click', () => this.createNewFile());
    }

    // New folder button
    const newFolderBtn = document.getElementById('new-folder-btn');
    if (newFolderBtn) {
      newFolderBtn.addEventListener('click', () => this.createNewFolder());
    }

    // Toggle context builder
    const toggleBtn = document.getElementById('toggle-context-builder');
    const contextSection = document.querySelector('.context-builder-section');
    if (toggleBtn && contextSection) {
      toggleBtn.addEventListener('click', () => {
        contextSection.classList.toggle('collapsed');
      });
    }
  },

  setupKeyboardShortcuts() {
    document.addEventListener('keydown', (e) => {
      // Ctrl+S / Cmd+S - Save
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        if (this.currentFile) {
          this.saveFile();
        }
      }

      // Ctrl+W / Cmd+W - Close file
      if ((e.ctrlKey || e.metaKey) && e.key === 'w') {
        e.preventDefault();
        if (this.currentFile) {
          this.closeFile();
        }
      }
    });
  },

  async openFile(path) {
    try {
      const editor = document.getElementById('file-editor');
      const pathEl = document.getElementById('editor-file-path');
      const statusEl = document.getElementById('editor-file-status');
      const saveBtn = document.getElementById('editor-save');
      const closeBtn = document.getElementById('editor-close');

      if (!editor) return;

      // Load file content
      editor.dataset.loading = 'true';
      const payload = await api(`/api/project/file?project_id=${projectParam()}&path=${encodeURIComponent(path)}`);

      this.currentFile = path;
      this.originalContent = payload.text || '';
      this.isModified = false;

      editor.value = this.originalContent;
      editor.dataset.loading = 'false';
      editor.disabled = false;

      if (pathEl) pathEl.textContent = path;
      if (statusEl) {
        statusEl.textContent = '';
        statusEl.className = 'editor-status';
      }

      if (saveBtn) saveBtn.disabled = false;
      if (closeBtn) closeBtn.disabled = false;

      // Set language for potential syntax highlighting
      const ext = this.getFileExtension(path);
      editor.dataset.language = this.getLanguageFromExtension(ext);

      // Add to open files
      this.addTab(path);

      // Update UI
      this.updateEditorUI();
      state.selectedFile = path;
      if (typeof workspaceChat !== "undefined") {
        workspaceChat.syncFileChip?.();
        if (workspaceChat.surface === "ask") workspaceChat.applySurface("split");
      }

    } catch (error) {
      showError(error);
    }
  },

  async saveFile() {
    if (!this.currentFile) return;

    try {
      const editor = document.getElementById('file-editor');
      const statusEl = document.getElementById('editor-file-status');

      if (!editor) return;

      const content = editor.value;

      // Call API to save file
      await api('/api/project/file/save', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: this.currentFile,
          text: content
        })
      });

      this.originalContent = content;
      this.isModified = false;

      if (statusEl) {
        statusEl.textContent = 'Saved';
        statusEl.className = 'editor-status saved';
        setTimeout(() => {
          statusEl.textContent = '';
          statusEl.className = 'editor-status';
        }, 2000);
      }

      this.updateEditorUI();

      // Refresh file tree
      await loadProjectFiles();

    } catch (error) {
      showError(error);
    }
  },

  async closeFile({ force = false } = {}) {
    if (this.isModified && !force) {
      const ok = await showAppConfirm({
        title: t("files.unsavedTitle"),
        message: t("files.unsavedClose"),
        confirmLabel: t("files.closeAnyway"),
        danger: true,
      });
      if (!ok) return false;
    }

    const editor = document.getElementById('file-editor');
    const pathEl = document.getElementById('editor-file-path');
    const statusEl = document.getElementById('editor-file-status');
    const saveBtn = document.getElementById('editor-save');
    const closeBtn = document.getElementById('editor-close');
    const closedPath = this.currentFile;

    this.currentFile = null;
    this.originalContent = '';
    this.isModified = false;

    if (editor) {
      editor.value = '';
      editor.disabled = true;
      delete editor.dataset.loading;
      delete editor.dataset.language;
    }

    if (pathEl) pathEl.textContent = 'Select or create a file';
    if (statusEl) {
      statusEl.textContent = '';
      statusEl.className = 'editor-status';
    }

    if (saveBtn) saveBtn.disabled = true;
    if (closeBtn) closeBtn.disabled = true;

    this.removeTab(closedPath);
    this.updateEditorUI();
    return true;
  },

  markAsModified() {
    const editor = document.getElementById('file-editor');
    const statusEl = document.getElementById('editor-file-status');

    if (!editor || !this.currentFile) return;

    const currentContent = editor.value;
    this.isModified = currentContent !== this.originalContent;

    if (statusEl) {
      if (this.isModified) {
        statusEl.textContent = 'Modified';
        statusEl.className = 'editor-status modified';
      } else {
        statusEl.textContent = '';
        statusEl.className = 'editor-status';
      }
    }

    this.updateEditorUI();
  },

  async createNewFile() {
    const fileName = await showAppPrompt({
      title: t("files.newFileTitle"),
      label: t("files.newFileLabel"),
      placeholder: t("files.newFilePlaceholder"),
      confirmLabel: t("files.createFile"),
    });
    if (!fileName) return;

    try {
      // Create empty file
      await api('/api/project/file/save', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: fileName,
          text: ''
        })
      });

      // Refresh file tree and open the new file
      await loadProjectFiles();
      await this.openFile(fileName);

    } catch (error) {
      showError(error);
    }
  },

  async createNewFolder() {
    const folderName = await showAppPrompt({
      title: t("files.newFolderTitle"),
      label: t("files.newFolderLabel"),
      placeholder: t("files.newFolderPlaceholder"),
      confirmLabel: t("files.createFolder"),
    });
    if (!folderName) return;

    try {
      // Create folder by creating a .gitkeep file inside
      await api('/api/project/file/save', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: `${folderName}/.gitkeep`,
          text: ''
        })
      });

      // Refresh file tree
      await loadProjectFiles();

    } catch (error) {
      showError(error);
    }
  },

  async deleteFile(path) {
    const ok = await showAppConfirm({
      title: t("files.deleteTitle"),
      message: t("files.deleteConfirm").replace("{path}", path),
      confirmLabel: t("files.delete"),
      danger: true,
    });
    if (!ok) return;

    try {
      await api('/api/project/file/delete', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: path
        })
      });

      if (this.currentFile === path) {
        await this.closeFile({ force: true });
      }

      await loadProjectFiles();

    } catch (error) {
      showError(error);
    }
  },

  async renameFile(oldPath) {
    const newPath = await showAppPrompt({
      title: t("files.renameTitle"),
      label: t("files.renameLabel"),
      value: oldPath,
      confirmLabel: t("files.rename"),
    });
    if (!newPath || newPath === oldPath) return;

    try {
      // Read current content
      const payload = await api(`/api/project/file?project_id=${projectParam()}&path=${encodeURIComponent(oldPath)}`);

      // Create new file with same content
      await api('/api/project/file/save', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: newPath,
          text: payload.text
        })
      });

      // Delete old file
      await api('/api/project/file/delete', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          path: oldPath
        })
      });

      if (this.currentFile === oldPath) {
        this.closeFile();
        await this.openFile(newPath);
      }

      await loadProjectFiles();

    } catch (error) {
      showError(error);
    }
  },

  setupContextMenu() {
    const contextMenu = document.getElementById('file-context-menu');
    if (!contextMenu) return;

    let targetPath = null;

    // Right-click on file tree item
    document.addEventListener('contextmenu', (e) => {
      const fileItem = e.target.closest('.file-tree-item');
      if (fileItem && fileItem.dataset.path) {
        e.preventDefault();
        targetPath = fileItem.dataset.path;

        // Position context menu
        contextMenu.style.left = `${e.pageX}px`;
        contextMenu.style.top = `${e.pageY}px`;
        contextMenu.dataset.visible = 'true';

        // Highlight target
        document.querySelectorAll('.file-tree-item').forEach(el => el.classList.remove('context-target'));
        fileItem.classList.add('context-target');
      }
    });

    // Click outside to close
    document.addEventListener('click', () => {
      contextMenu.dataset.visible = 'false';
      document.querySelectorAll('.file-tree-item').forEach(el => el.classList.remove('context-target'));
    });

    // Context menu actions
    contextMenu.addEventListener('click', async (e) => {
      const action = e.target.closest('[data-action]')?.dataset.action;
      if (!action || !targetPath) return;

      contextMenu.dataset.visible = 'false';

      switch (action) {
        case 'open':
          await this.openFile(targetPath);
          break;
        case 'rename':
          await this.renameFile(targetPath);
          break;
        case 'delete':
          await this.deleteFile(targetPath);
          break;
        case 'copy-path':
          await navigator.clipboard.writeText(targetPath);
          break;
      }
    });
  },

  addTab(path) {
    const tabsContainer = document.getElementById('editor-tabs');
    if (!tabsContainer) return;

    // Remove placeholder
    const placeholder = tabsContainer.querySelector('.editor-tab-placeholder');
    if (placeholder) placeholder.remove();

    // Check if tab already exists
    if (tabsContainer.querySelector(`[data-tab-path="${path}"]`)) return;

    const tab = document.createElement('div');
    tab.className = 'editor-tab active';
    tab.dataset.tabPath = path;

    const ext = this.getFileExtension(path);
    const icon = this.getFileIcon(ext);

    tab.innerHTML = `
      <span class="editor-tab-icon">${icon}</span>
      <span class="editor-tab-name">${path.split('/').pop()}</span>
      <button class="editor-tab-close" aria-label="Close">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    `;

    // Tab click to switch
    tab.addEventListener('click', (e) => {
      if (!e.target.closest('.editor-tab-close')) {
        this.openFile(path);
      }
    });

    // Close button
    on(tab.querySelector('.editor-tab-close'), 'click', (e) => {
      e.stopPropagation();
      if (this.currentFile === path) {
        this.closeFile();
      } else {
        tab.remove();
      }
    });

    tabsContainer.appendChild(tab);
  },

  removeTab(path) {
    const tab = document.querySelector(`[data-tab-path="${path}"]`);
    if (tab) tab.remove();

    const tabsContainer = document.getElementById('editor-tabs');
    if (tabsContainer && tabsContainer.children.length === 0) {
      tabsContainer.innerHTML = '<div class="editor-tab-placeholder"><span>No files open</span></div>';
    }
  },

  updateEditorUI() {
    // Update tab modified state
    const tabs = document.querySelectorAll('.editor-tab');
    tabs.forEach(tab => {
      if (tab.dataset.tabPath === this.currentFile) {
        tab.classList.add('active');
        if (this.isModified) {
          tab.classList.add('modified');
        } else {
          tab.classList.remove('modified');
        }
      } else {
        tab.classList.remove('active');
      }
    });
  },

  getFileExtension(path) {
    const match = path.match(/\.([^.]+)$/);
    return match ? match[1].toLowerCase() : '';
  },

  getLanguageFromExtension(ext) {
    const langMap = {
      'py': 'python',
      'js': 'javascript',
      'ts': 'typescript',
      'jsx': 'javascript',
      'tsx': 'typescript',
      'json': 'json',
      'html': 'html',
      'css': 'css',
      'scss': 'scss',
      'md': 'markdown',
      'sql': 'sql',
      'sh': 'shell',
      'yml': 'yaml',
      'yaml': 'yaml'
    };
    return langMap[ext] || 'text';
  },

  getFileIcon(ext) {
    const iconMap = {
      'py': '🐍',
      'js': '📜',
      'ts': '📜',
      'jsx': '⚛️',
      'tsx': '⚛️',
      'json': '📦',
      'md': '📝',
      'html': '🌐',
      'css': '🎨',
      'scss': '🎨',
      'png': '🖼️',
      'jpg': '🖼️',
      'svg': '🖼️',
      'pdf': '📄',
      'txt': '📄'
    };
    return iconMap[ext] || '📄';
  }
};

export { fileEditor };
