// New project wizard. Classic script loaded before app.js; shares global scope.
const projectWizard = {
  firstRun: false,
  projectData: {},

  init() {
    this.bindEvents();
    this.loadCurrentProject();
  },

  bindEvents() {
    // New Project button
    const newProjectBtn = document.getElementById('new-project-btn');
    if (newProjectBtn) {
      newProjectBtn.addEventListener('click', () => this.openWizard());
    }

    // Project Settings button
    const settingsBtn = document.getElementById('project-settings-btn');
    if (settingsBtn) {
      settingsBtn.addEventListener('click', () => this.openSettings());
    }

    const wizardSkip = document.getElementById('wizard-skip');
    const wizardCancel = document.getElementById('wizard-cancel');
    const wizardFinish = document.getElementById('wizard-finish');

    if (wizardSkip) wizardSkip.addEventListener('click', () => {
      if (this.firstRun) {
        markOnboardingComplete().catch(showError);
      }
      this.closeModal('new-project-wizard');
    });
    if (wizardCancel) wizardCancel.addEventListener('click', () => this.closeModal('new-project-wizard'));
    if (wizardFinish) wizardFinish.addEventListener('click', () => this.finishWizard());

    // Browse folder
    const browseFolderBtn = document.getElementById('wizard-browse-folder');
    if (browseFolderBtn) {
      browseFolderBtn.addEventListener('click', () => this.browseFolder());
    }

    // Auto-fill project name from folder path
    const folderPathInput = document.getElementById('wizard-folder-path');
    const projectNameInput = document.getElementById('wizard-project-name');
    const syncProjectName = () => {
      const path = folderPathInput?.value?.trim();
      if (!path || !projectNameInput) return;
      const folderName = path.split(/[/\\]/).filter(Boolean).pop();
      if (folderName) projectNameInput.value = folderName;
    };
    if (folderPathInput) {
      folderPathInput.addEventListener('change', syncProjectName);
      folderPathInput.addEventListener('blur', syncProjectName);
    }

    // Modal close handlers
    document.querySelectorAll('[data-close-modal]').forEach(el => {
      el.addEventListener('click', (e) => {
        const modal = e.target.closest('.modal');
        if (modal) this.closeModal(modal.id);
      });
    });

    // Settings actions
    this.bindSettingsActions();
  },

  bindSettingsActions() {
    const initBtn = document.getElementById('settings-init-project');
    const scanBtn = document.getElementById('settings-scan-project');
    const reindexBtn = document.getElementById('settings-reindex-project');
    const buildContextBtn = document.getElementById('settings-build-context');
    const saveBtn = document.getElementById('settings-save');
    const browsePathBtn = document.getElementById('settings-browse-path');

    if (initBtn) initBtn.addEventListener('click', () => this.initProject());
    if (scanBtn) scanBtn.addEventListener('click', () => this.scanProject());
    if (reindexBtn) reindexBtn.addEventListener('click', () => this.reindexProject());
    if (buildContextBtn) buildContextBtn.addEventListener('click', () => this.buildContext());
    if (saveBtn) saveBtn.addEventListener('click', () => this.saveSettings());
    if (browsePathBtn) browsePathBtn.addEventListener('click', () => this.browseSettingsPath());
  },

  async loadCurrentProject() {
    try {
      // Load from state or API
      if (state.projectId && state.projectId !== 'architectos') {
        const projectNameEl = document.getElementById('current-project-name');
        if (projectNameEl) {
          projectNameEl.textContent = state.projectId;
        }
      }
    } catch (error) {
      console.error('Failed to load current project:', error);
    }
  },

  openWizard(options = {}) {
    this.firstRun = Boolean(options.firstRun);
    this.projectData = {};
    const title = document.getElementById('wizard-title');
    const lead = document.getElementById('wizard-lead');
    const skipBtn = document.getElementById('wizard-skip');
    const cancelBtn = document.getElementById('wizard-cancel');
    const finishBtn = document.getElementById('wizard-finish');
    const advanced = document.querySelector('#new-project-wizard .wizard-advanced');

    if (title) title.textContent = this.firstRun ? t('wizard.welcomeTitle') : t('wizard.title');
    if (lead) lead.textContent = this.firstRun ? t('wizard.welcomeLead') : t('wizard.lead');
    if (skipBtn) skipBtn.hidden = !this.firstRun;
    if (cancelBtn) cancelBtn.hidden = this.firstRun;
    if (finishBtn) finishBtn.textContent = this.firstRun ? t('wizard.getStarted') : t('wizard.create');
    if (advanced) advanced.open = false;

    const folderPathInput = document.getElementById('wizard-folder-path');
    const projectNameInput = document.getElementById('wizard-project-name');
    if (folderPathInput) folderPathInput.value = '';
    if (projectNameInput) projectNameInput.value = '';
    setWizardFolderStatus('');

    this.openModal('new-project-wizard');
  },

  openSettings() {
    // Load current project settings
    const project = currentProject();
    const projectName = document.getElementById('settings-project-name');
    const projectPath = document.getElementById('settings-project-path');

    if (projectName) {
      projectName.value = displayProjectName(project);
    }
    if (projectPath) {
      projectPath.value = project?.root_path || "";
    }

    this.openModal('project-settings-modal');
  },

  openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
      modal.removeAttribute('hidden');
      document.body.style.overflow = 'hidden';
    }
  },

  closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
      modal.setAttribute('hidden', '');
      document.body.style.overflow = '';
    }
  },

  async browseFolder() {
    const browseBtn = document.getElementById('wizard-browse-folder');
    const folderPathInput = document.getElementById('wizard-folder-path');
    if (browseBtn) browseBtn.disabled = true;
    try {
      setWizardFolderStatus(t("wizard.pickerWaiting"));
      const path = await pickProjectFolder({
        initialPath: folderPathInput?.value || '',
        onStatus: setWizardFolderStatus,
      });
      if (!path || !folderPathInput) return;
      folderPathInput.value = path;
      folderPathInput.dispatchEvent(new Event('change'));
    } catch (error) {
      setWizardFolderStatus(error.message || t('wizard.pickerUnavailable'), 'error');
    } finally {
      if (browseBtn) browseBtn.disabled = false;
    }
  },

  async browseSettingsPath() {
    try {
      const pathInput = document.getElementById('settings-project-path');
      const path = await pickProjectFolder({ initialPath: pathInput?.value || '' });
      if (path && pathInput) pathInput.value = path;
    } catch (error) {
      showError(error);
    }
  },

  async finishWizard() {
    try {
      // Gather all wizard data
      const folderPath = document.getElementById('wizard-folder-path')?.value;
      const projectName = document.getElementById('wizard-project-name')?.value;
      const codeStyle = document.getElementById('wizard-code-style')?.value;
      const ignorePatterns = document.getElementById('wizard-ignore-patterns')?.value;
      const autoIndex = this.firstRun ? true : (document.getElementById('wizard-auto-index')?.checked ?? true);
      const autoMemory = document.getElementById('wizard-auto-memory')?.checked;
      const memoryScope = document.getElementById('wizard-memory-scope')?.value;

      if (!folderPath || !projectName) {
        showSnackbar(t('wizard.validation'), 'error');
        return;
      }

      // Create project via API
      const response = await api('/api/projects', {
        method: 'POST',
        body: JSON.stringify({
          name: projectName,
          root_path: folderPath,
          config: {
            code_style: codeStyle,
            ignore_patterns: ignorePatterns?.split('\n').filter(p => p.trim()),
            auto_index: autoIndex,
            auto_memory: autoMemory,
            memory_scope: memoryScope
          }
        })
      });

      // Update UI
      const actualProjectId = response.project_id || response.id || (response.project && response.project.id) || projectName;
      state.projectId = actualProjectId;
      await saveWorkspaceSettings({ current_project_id: state.projectId });
      state.selectedFile = "";

      const projectNameEl = document.getElementById('current-project-name');
      if (projectNameEl) {
        projectNameEl.textContent = projectName;
      }

      await markOnboardingComplete();

      if (autoIndex) {
        try {
          await api('/api/project/scan', {
            method: 'POST',
            body: JSON.stringify({ project_id: state.projectId, root_path: folderPath, name: projectName, limit: 80, reindex: true, rebuild_links: true })
          });
        } catch (scanError) {
          console.warn('Auto-index after wizard failed:', scanError);
        }
      }

      // Close wizard
      this.closeModal('new-project-wizard');

      // Refresh workspace
      await loadProjects();
      await loadProjectFiles();

      // Show success message
      console.log(`Project created: ${projectName} (ID: ${actualProjectId})`);
      showSnackbar(response.message || `Project ${projectName} was created successfully.`, response.created === false ? 'info' : 'success');

    } catch (error) {
      showError(error);
    }
  },

  async initProject() {
    try {
      const projectName = document.getElementById('settings-project-name')?.value;
      if (!projectName) {
        showSnackbar('Please enter a project name', 'error');
        return;
      }

      await api('/api/project/scan', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          init: true
        })
      });

      showSnackbar('Project initialized successfully.', 'success');
      await loadProjectFiles();
    } catch (error) {
      showError(error);
    }
  },

  async scanProject() {
    try {
      await api('/api/project/scan', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId
        })
      });

      showSnackbar('Project scanned successfully.', 'success');
      await loadProjectFiles();
    } catch (error) {
      showError(error);
    }
  },

  async reindexProject() {
    try {
      await api('/api/project/scan', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          reindex: true
        })
      });

      showSnackbar('Project reindexed successfully.', 'success');
      await loadProjectFiles();
    } catch (error) {
      showError(error);
    }
  },

  async buildContext() {
    try {
      const query = document.getElementById('settings-context-query')?.value;
      if (!query) {
        showSnackbar('Please enter a context query', 'error');
        return;
      }

      const response = await api('/api/project/context', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          query: query
        })
      });

      console.log('Context built:', response.context);
      showSnackbar('Context built successfully.', 'success');
    } catch (error) {
      showError(error);
    }
  },

  async saveSettings() {
    try {
      const projectName = document.getElementById('settings-project-name')?.value;
      const projectPath = document.getElementById('settings-project-path')?.value;

      if (!projectName || !projectPath) {
        showSnackbar('Please select a project folder and enter a project name', 'error');
        return;
      }

      const project = await api('/api/projects', {
        method: 'POST',
        body: JSON.stringify({ name: projectName, root_path: projectPath })
      });
      state.projectId = project.project_id || project.id || (project.project && project.project.id);
      if (!state.projectId) throw new Error("Project was created but no project id was returned.");
      await saveWorkspaceSettings({ current_project_id: state.projectId });
      state.selectedFile = "";
      await loadProjects();
      await loadProjectFiles();

      this.closeModal('project-settings-modal');
      showSnackbar(project.message || 'Project settings saved successfully.', project.created === false ? 'info' : 'success');
    } catch (error) {
      showError(error);
    }
  }
};
