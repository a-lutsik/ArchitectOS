// Workspace Sidebar Resizer

(function() {
  const MIN_WIDTH = 180;
  const MAX_WIDTH = 800;
  const DEFAULT_WIDTH = 280;

  let isResizing = false;
  let startX = 0;
  let startWidth = 0;

  function initWorkspaceResizer() {
    const resizer = document.getElementById('workspace-resizer');
    const sidebar = document.querySelector('.workspace-sidebar');

    if (!resizer || !sidebar) {
      console.warn('Workspace resizer or sidebar not found');
      return;
    }

    // Load saved width from localStorage
    const savedWidth = localStorage.getItem('workspace-sidebar-width');
    if (savedWidth) {
      const width = parseInt(savedWidth, 10);
      if (width >= MIN_WIDTH && width <= MAX_WIDTH) {
        sidebar.style.width = `${width}px`;
      }
    }

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

      sidebar.style.width = `${newWidth}px`;
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
      sidebar.style.width = `${DEFAULT_WIDTH}px`;
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
