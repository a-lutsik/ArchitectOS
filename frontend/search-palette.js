// Search command palette. ES module.
import { api } from "./api-client.js";
import { switchView } from "./ask-ui.js";
import { escapeHtml, showSnackbar } from "./dom-utils.js";
import { graphState, openGraphNodeModal } from "./graph.js";
import { projectParam, state, t } from "./state.js";
import { renderResults, showError } from "./ui.js";

const searchPalette = {
  isOpen: false,
  highlightedIndex: -1,
  currentResults: [],

  getRecentSearches() {
    try {
      return JSON.parse(localStorage.getItem('architectos_recent_searches') || '[]').slice(0, 5);
    } catch {
      return [];
    }
  },

  saveRecentSearch(query) {
    if (!query.trim()) return;
    try {
      let recent = this.getRecentSearches();
      recent = [query, ...recent.filter(q => q !== query)].slice(0, 5);
      localStorage.setItem('architectos_recent_searches', JSON.stringify(recent));
    } catch (error) {
      console.warn('Could not save recent search:', error);
    }
  },

  open() {
    this.isOpen = true;
    const palette = document.getElementById('searchPalette');
    palette.classList.add('active');
    palette.classList.remove('closing');

    const input = document.getElementById('paletteSearchInput');
    setTimeout(() => {
      input.focus();
      input.select();
    }, 100);

    document.body.style.overflow = 'hidden';
    this.renderRecentSearches();
  },

  close() {
    this.isOpen = false;
    const palette = document.getElementById('searchPalette');
    palette.classList.add('closing');

    setTimeout(() => {
      palette.classList.remove('active', 'closing');
      document.body.style.overflow = '';
      const input = document.getElementById('paletteSearchInput');
      input.value = '';
      this.showRecentSection();
    }, 200);
  },

  toggle() {
    if (this.isOpen) {
      this.close();
    } else {
      this.open();
    }
  },

  showRecentSection() {
    document.getElementById('paletteRecentSearches').style.display = 'block';
    document.getElementById('paletteResults').style.display = 'none';
  },

  showResultsSection() {
    document.getElementById('paletteRecentSearches').style.display = 'none';
    document.getElementById('paletteResults').style.display = 'block';
  },

  renderRecentSearches() {
    const recent = this.getRecentSearches();
    const container = document.getElementById('recentSearchesList');

    if (!recent.length) {
      container.innerHTML = '<div class="recent-item" style="cursor: default; opacity: 0.6;">No recent searches</div>';
      return;
    }

    container.innerHTML = recent.map(query => `
      <div class="recent-item" data-recent-query="${escapeHtml(query)}">
        <span class="recent-icon">🕐</span>
        <span>${escapeHtml(query)}</span>
      </div>
    `).join('');

    container.querySelectorAll('.recent-item[data-recent-query]').forEach(item => {
      item.addEventListener('click', () => {
        const query = item.dataset.recentQuery;
        const input = document.getElementById('paletteSearchInput');
        input.value = query;
        this.performSearch(query);
      });
    });
  },

  async performSearch(query) {
    if (!query.trim()) {
      this.showRecentSection();
      return;
    }

    this.showResultsSection();
    this.renderLoading(query);
    this.currentQuery = query;

    const activeFilters = Array.from(document.querySelectorAll('.filter-pill.active'))
      .map(pill => pill.dataset.filterType);

    const requestId = (this._searchRequestId = (this._searchRequestId || 0) + 1);
    try {
      const payload = await api(
        `/api/memory/search?query=${encodeURIComponent(query)}&project_id=${projectParam()}&limit=12&refresh=0`
      );
      if (requestId !== this._searchRequestId) return;

      let results = payload.hits || [];

      // Apply filters
      if (activeFilters.length > 0) {
        results = results.filter(hit => {
          if (activeFilters.includes('favorite')) {
            return hit.node.metadata && hit.node.metadata.favorite;
          }
          return activeFilters.includes(hit.node.type);
        });
      }

      this.currentResults = results;
      this.renderResults(results);
      this.saveRecentSearch(query);
    } catch (error) {
      if (requestId !== this._searchRequestId) return;
      console.error('Search failed:', error);
      this.renderError(error.message);
    }
  },

  renderLoading(query) {
    const container = document.getElementById('paletteResultList');
    const countSpan = document.getElementById('paletteResultCount');
    if (countSpan) countSpan.textContent = '';
    if (!container) return;
    container.innerHTML = `
      <div class="no-palette-results">
        <div class="no-palette-results-icon">⏳</div>
        <div>Searching memory for “${escapeHtml(query.trim())}”…</div>
      </div>
    `;
    this.highlightedIndex = -1;
  },

  getTypeIcon(type) {
    const icons = {
      'Decision': '🎯',
      'Lesson': '💡',
      'Constraint': '⚠️',
      'Feature': '✨',
      'Doc': '📚',
      'Artifact': '📦',
      'Task': '✅',
      'Concept': '💭',
      'Project': '🏗️',
      'Provider': '🔌',
      'Requirement': '📋',
      'Meeting': '🤝'
    };
    return icons[type] || '📝';
  },

  groupFor(hit) {
    const node = hit.node || {};
    const meta = node.metadata || {};
    const source = String(meta.source || meta.source_type || '').toLowerCase();
    if (meta.work_item_id || source.includes('boards')) return 'boards';
    if (node.type === 'Artifact' || ['code', 'project_scan', 'azure-git'].some(key => source.includes(key))) return 'code';
    return 'knowledge';
  },

  renderResults(results) {
    const container = document.getElementById('paletteResultList');
    const countSpan = document.getElementById('paletteResultCount');
    if (!container || !countSpan) return;

    countSpan.textContent = `(${results.length})`;

    if (!results.length) {
      container.innerHTML = `
        <div class="no-palette-results">
          <div class="no-palette-results-icon">🔍</div>
          <div>No results found</div>
        </div>
      `;
      this.highlightedIndex = -1;
      return;
    }

    const groups = [
      ['knowledge', t('palette.group.knowledge') || 'Knowledge'],
      ['boards', t('palette.group.boards') || 'Boards'],
      ['code', t('palette.group.code') || 'Code'],
    ];
    const grouped = new Map(groups.map(([key]) => [key, []]));
    for (const hit of results) {
      grouped.get(this.groupFor(hit)).push(hit);
    }

    let index = 0;
    const parts = [];
    for (const [key, label] of groups) {
      const items = grouped.get(key);
      if (!items.length) continue;
      parts.push(`<div class="palette-group-header">${escapeHtml(label)} <span class="muted">(${items.length})</span></div>`);
      for (const hit of items) {
        const node = hit.node;
        const icon = this.getTypeIcon(node.type);
        const snippet = (node.text || '').substring(0, 140);
        parts.push(`
          <div class="palette-result-item ${index === 0 ? 'highlighted' : ''}" data-index="${index}" data-node-id="${escapeHtml(node.id)}">
            <div class="palette-result-icon">${icon}</div>
            <div class="palette-result-content">
              <div class="palette-result-title">${escapeHtml(node.label)}</div>
              <div class="palette-result-meta">
                <span class="badge">${escapeHtml(node.type)}</span>
                <span class="badge">${escapeHtml(node.scope)}</span>
              </div>
              <div class="palette-result-snippet">${escapeHtml(snippet)}${snippet.length >= 140 ? '...' : ''}</div>
            </div>
            <div class="palette-result-actions">
              <button type="button" class="palette-feedback-btn" data-palette-feedback="1" data-node-id="${escapeHtml(node.id)}" title="Relevant result">👍</button>
              <button type="button" class="palette-feedback-btn" data-palette-feedback="-1" data-node-id="${escapeHtml(node.id)}" title="Not relevant">👎</button>
              <kbd>↵</kbd>
            </div>
          </div>
        `);
        index += 1;
      }
    }
    container.innerHTML = parts.join('');

    this.highlightedIndex = 0;

    // Bind click events
    container.querySelectorAll('.palette-result-item').forEach(item => {
      item.addEventListener('click', event => {
        const feedback = event.target.closest('[data-palette-feedback]');
        if (feedback) {
          event.stopPropagation();
          api('/api/memory/feedback', {
            method: 'POST',
            body: JSON.stringify({
              project_id: state.projectId,
              query: this.currentQuery || '',
              rating: Number(feedback.dataset.paletteFeedback || 0),
              hit_ids: [feedback.dataset.nodeId],
            }),
          }).then(() => {
            feedback.classList.add('active');
            showSnackbar(t('palette.feedbackSaved') || 'Feedback saved', 'info');
          }).catch(showError);
          return;
        }
        this.selectResult(item.dataset.nodeId);
      });
    });
  },

  renderError(message) {
    const container = document.getElementById('paletteResultList');
    container.innerHTML = `
      <div class="no-palette-results">
        <div class="no-palette-results-icon">⚠️</div>
        <div>Search failed: ${escapeHtml(message)}</div>
      </div>
    `;
  },

  selectResult(nodeId) {
    this.close();

    // Switch to memory view
    switchView('memory');

    // Try to focus the node in graph if it exists
    setTimeout(() => {
      if (graphState.nodes.find(n => n.id === nodeId)) {
        const node = graphState.nodes.find(n => n.id === nodeId);
        if (node) {
          openGraphNodeModal(node);
          // Center the graph on this node if possible
          const particle = graphState.particles.find(p => p.id === nodeId);
          if (particle) {
            const canvas = document.querySelector('#graph-canvas');
            if (canvas) {
              graphState.offsetX = canvas.width / 2 - particle.x;
              graphState.offsetY = canvas.height / 2 - particle.y;
            }
          }
        }
      }
    }, 300);
  },

  navigateResults(direction) {
    const items = document.querySelectorAll('.palette-result-item');
    if (!items.length) return;

    const current = document.querySelector('.palette-result-item.highlighted');

    if (current) {
      current.classList.remove('highlighted');
      this.highlightedIndex = parseInt(current.dataset.index);
    }

    if (direction === 'down') {
      this.highlightedIndex = Math.min(this.highlightedIndex + 1, items.length - 1);
    } else {
      this.highlightedIndex = Math.max(this.highlightedIndex - 1, 0);
    }

    items[this.highlightedIndex]?.classList.add('highlighted');
    items[this.highlightedIndex]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  },

  selectHighlighted() {
    const highlighted = document.querySelector('.palette-result-item.highlighted');
    if (highlighted) {
      this.selectResult(highlighted.dataset.nodeId);
    }
  },

  bindEvents() {
    // Open button
    document.getElementById('open-search-palette')?.addEventListener('click', () => this.open());

    // Close on backdrop click
    document.querySelectorAll('[data-close-palette]').forEach(el => {
      el.addEventListener('click', () => this.close());
    });

    // Search input
    const input = document.getElementById('paletteSearchInput');
    let searchTimeout;
    input.addEventListener('input', (e) => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => {
        this.performSearch(e.target.value);
      }, 300);
    });

    // Filter pills
    document.querySelectorAll('.filter-pill').forEach(pill => {
      pill.addEventListener('click', () => {
        pill.classList.toggle('active');
        const input = document.getElementById('paletteSearchInput');
        this.performSearch(input.value);
      });
    });

    // Global keyboard shortcuts
    document.addEventListener('keydown', (e) => {
      // Ctrl/Cmd + K to toggle
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        this.toggle();
        return;
      }

      // Only handle these when palette is open
      if (!this.isOpen) return;

      // Escape to close
      if (e.key === 'Escape') {
        e.preventDefault();
        this.close();
        return;
      }

      // Arrow navigation
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        this.navigateResults(e.key === 'ArrowDown' ? 'down' : 'up');
        return;
      }

      // Enter to select
      if (e.key === 'Enter') {
        e.preventDefault();
        this.selectHighlighted();
        return;
      }
    });
  }
};

export { searchPalette };
