/* Workspace file-tree rendering — extracted from app.js */
import { state } from "./state.js";
import { createVirtualList } from "./virtual-list.js";

// The tree is flattened into a row model and windowed (see virtual-list.js):
// only viewport rows + overscan exist in the DOM, so rendering and the
// dotfiles toggle cost O(viewport) DOM nodes instead of O(total files).
// Row indent preserves the old nested-DOM geometry: each depth used to add
// 20px of .file-tree-children padding plus a 20px inline step, on an 8px base.
const ROW_HEIGHT = 32; // ~30px row + 2px .file-tree gap; measured after render
const ROW_GAP = 2; // .file-tree { gap: 2px }
const INDENT_STEP = 40; // 20px old children-container padding + 20px inline step
const INDENT_BASE = 8;

function buildFileTree(files) {
  const tree = { name: 'root', type: 'folder', path: '', children: [], expanded: true };

  files.forEach(file => {
    const parts = file.path.split(/[\\/]/);
    let current = tree;

    parts.forEach((part, idx) => {
      const isLast = idx === parts.length - 1;
      const existingChild = current.children.find(child => child.name === part);

      if (existingChild) {
        current = existingChild;
      } else {
        const explicitType = file.type === 'folder' || file.type === 'directory' ? 'folder' : 'file';
        const newNode = {
          name: part,
          type: isLast ? explicitType : 'folder',
          path: parts.slice(0, idx + 1).join('/'),
          children: isLast && explicitType === 'file' ? undefined : [],
          expanded: false
        };
        current.children.push(newNode);
        if (!isLast || explicitType === 'folder') current = newNode;
      }
    });
  });

  return tree;
}

function getFileExtension(filename) {
  const match = filename.match(/\.[^.]+$/);
  return match ? match[0] : '';
}

function sortedChildren(node) {
  if (!node._sortedChildren) {
    node._sortedChildren = (node.children || []).slice().sort((a, b) => {
      if (a.type !== b.type) return a.type === 'folder' ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
  }
  return node._sortedChildren;
}

// Visible rows in depth-first order, skipping children of collapsed folders.
function flattenTree(tree) {
  const rows = [];
  const walk = (node, depth) => {
    rows.push({ node, depth });
    if (node.type === 'folder' && node.expanded) {
      sortedChildren(node).forEach(child => walk(child, depth + 1));
    }
  };
  sortedChildren(tree).forEach(child => walk(child, 0));
  return rows;
}

// Same per-row markup/classes as the old recursive renderer so styles.css,
// the ext-icon rules and file-editor's context-menu delegation keep working.
function buildRowElement(row, selectedPath) {
  const { node, depth } = row;
  const item = document.createElement('div');
  item.className = `file-tree-item ${node.type === 'folder' ? 'folder' : 'file'}`;
  item.style.paddingLeft = `${depth * INDENT_STEP + INDENT_BASE}px`;
  item.dataset.path = node.path;
  if (node.path === selectedPath) item.classList.add('selected');

  if (node.type === 'folder') {
    const toggle = document.createElement('span');
    toggle.className = 'file-tree-toggle';
    toggle.textContent = '▶';
    if (node.expanded) toggle.classList.add('expanded');
    item.appendChild(toggle);

    const icon = document.createElement('span');
    icon.className = 'file-tree-icon';
    icon.textContent = '📂';
    item.appendChild(icon);
  } else {
    const spacer = document.createElement('span');
    spacer.style.width = '16px';
    item.appendChild(spacer);

    item.dataset.ext = getFileExtension(node.name);

    const icon = document.createElement('span');
    icon.className = 'file-tree-icon';
    icon.textContent = '📄';
    item.appendChild(icon);
  }

  const name = document.createElement('span');
  name.className = 'file-tree-name';
  name.textContent = node.name;
  name.title = node.path;
  item.appendChild(name);

  return item;
}

// Single live view (there is one #file-list container). One delegated click
// listener replaces the old per-row listeners.
let treeView = null; // { container, tree, rows, vlist, selectedPath, onSelect }

function refreshTreeRows(view) {
  view.rows = flattenTree(view.tree);
  view.vlist.setRows(view.rows);
}

function handleTreeClick(event) {
  const view = treeView;
  if (!view) return;
  const target = event.target;
  if (!target || typeof target.closest !== 'function') return;
  const item = target.closest('.file-tree-item');
  if (!item || !view.container.contains(item)) return;
  const row = view.rows[Number(item.dataset.vlistIndex)];
  if (!row) return;
  const { node } = row;

  if (node.type === 'folder' && target.closest('.file-tree-toggle')) {
    event.stopPropagation(); // the old per-row toggle handler stopped propagation too
    node.expanded = !node.expanded;
    refreshTreeRows(view);
    return;
  }

  if (node.type === 'file') {
    view.selectedPath = node.path;
    view.container.querySelectorAll('.file-tree-item.selected').forEach(el => el.classList.remove('selected'));
    item.classList.add('selected');
    if (view.onSelect) view.onSelect(node);
  }
}

function dropTreeView() {
  if (!treeView) return;
  treeView.container.removeEventListener('click', handleTreeClick);
  treeView.vlist.destroy();
  treeView = null;
}

function ensureTreeView(container, onSelect) {
  if (treeView && treeView.container === container) {
    treeView.onSelect = onSelect;
    return treeView;
  }
  dropTreeView();
  const view = { container, tree: null, rows: [], vlist: null, selectedPath: "", onSelect };
  view.vlist = createVirtualList({
    container,
    renderRow: row => buildRowElement(row, view.selectedPath),
    estimatedRowHeight: ROW_HEIGHT,
    overscan: 10,
    gap: ROW_GAP,
    holderClass: 'file-tree',
  });
  container.addEventListener('click', handleTreeClick);
  treeView = view;
  return view;
}

function renderFileTree(files, container, onSelect) {
  // Dotfiles/dirs (.cursor, .git-*) are noise for most sessions; the header
  // eye toggle re-renders with state.showDotfiles=true to reveal them.
  if (!state.showDotfiles) {
    files = (files || []).filter(file => {
      const path = String(file.path || file.name || "");
      return !path.split("/").some(segment => segment.length > 1 && segment.startsWith("."));
    });
  }

  if (!files || files.length === 0) {
    dropTreeView();
    container.innerHTML = '';
    const empty = document.createElement('div');
    empty.className = 'file-tree-empty';
    empty.textContent = 'No files found';
    container.appendChild(empty);
    return;
  }

  const view = ensureTreeView(container, onSelect);
  view.tree = buildFileTree(files);
  view.selectedPath = ""; // a full re-render used to drop the .selected class too
  refreshTreeRows(view);
}

export { getFileExtension, renderFileTree };
