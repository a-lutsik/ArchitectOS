// Workspace Sidebar Resizer — self-contained ES module (side effects only, no exports).

(function() {
  const MIN_WIDTH = 180;
  const MAX_WIDTH = 800;
  const DEFAULT_WIDTH = 280;

  let isResizing = false;
  let startX = 0;
  let startWidth = 0;

  function applySidebarWidth(sidebar, width) {
    const px = `${width}px`;
    sidebar.style.width = px;
    sidebar.style.flexBasis = px;
    sidebar.style.flexGrow = "0";
    sidebar.style.flexShrink = "0";
  }

  function initWorkspaceResizer() {
    const resizer = document.getElementById('workspace-resizer');
    const sidebar = document.querySelector('.workspace-sidebar');

    if (!resizer || !sidebar) {
      console.warn('Workspace resizer or sidebar not found');
      return;
    }

    let width = DEFAULT_WIDTH;
    const savedWidth = localStorage.getItem('workspace-sidebar-width');
    if (savedWidth) {
      const parsed = parseInt(savedWidth, 10);
      if (parsed >= MIN_WIDTH && parsed <= MAX_WIDTH) width = parsed;
    }
    applySidebarWidth(sidebar, width);

    if (resizer.dataset.bound === "1") return;
    resizer.dataset.bound = "1";

    resizer.addEventListener('mousedown', handleMouseDown);

    function handleMouseDown(e) {
      isResizing = true;
      startX = e.clientX;
      startWidth = sidebar.offsetWidth;

      resizer.classList.add('resizing');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);

      e.preventDefault();
    }

    function handleMouseMove(e) {
      if (!isResizing) return;

      const deltaX = e.clientX - startX;
      let newWidth = startWidth + deltaX;

      // Constrain width
      if (newWidth < MIN_WIDTH) newWidth = MIN_WIDTH;
      if (newWidth > MAX_WIDTH) newWidth = MAX_WIDTH;

      applySidebarWidth(sidebar, newWidth);
    }

    function handleMouseUp() {
      if (!isResizing) return;

      isResizing = false;
      resizer.classList.remove('resizing');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';

      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);

      // Save width to localStorage
      const currentWidth = sidebar.offsetWidth;
      localStorage.setItem('workspace-sidebar-width', currentWidth.toString());
    }

    // Double-click to reset to default width
    resizer.addEventListener('dblclick', () => {
      applySidebarWidth(sidebar, DEFAULT_WIDTH);
      localStorage.setItem('workspace-sidebar-width', DEFAULT_WIDTH.toString());
    });
  }

  // Initialize when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initWorkspaceResizer);
  } else {
    initWorkspaceResizer();
  }

  // Re-initialize when workspace view becomes active
  document.addEventListener('viewchange', (e) => {
    if (e.detail && e.detail.view === 'workspace') {
      setTimeout(initWorkspaceResizer, 100);
    }
  });
})();
