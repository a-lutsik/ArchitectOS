/* Workspace file-tree rendering — extracted from app.js */
import { state } from "./state.js";

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

function renderFileTreeNode(node, container, level = 0, onSelect) {
  if (node.name === 'root') {
    node.children.forEach(child => renderFileTreeNode(child, container, 0, onSelect));
    return;
  }

  const item = document.createElement('div');
  item.className = 'file-tree-item';
  item.style.paddingLeft = `${level * 20 + 8}px`;
  item.dataset.path = node.path;

  let childrenContainer = null;

  if (node.type === 'folder') {
    item.classList.add('folder');

    const toggle = document.createElement('span');
    toggle.className = 'file-tree-toggle';
    toggle.textContent = '▶';
    if (node.expanded) toggle.classList.add('expanded');

    toggle.addEventListener('click', e => {
      e.stopPropagation();
      node.expanded = !node.expanded;
      toggle.classList.toggle('expanded');
      if (childrenContainer) {
        childrenContainer.classList.toggle('collapsed');
      }
    });
    item.appendChild(toggle);

    const icon = document.createElement('span');
    icon.className = 'file-tree-icon';
    icon.textContent = '📂';
    item.appendChild(icon);
  } else {
    item.classList.add('file');

    const spacer = document.createElement('span');
    spacer.style.width = '16px';
    item.appendChild(spacer);

    const ext = getFileExtension(node.name);
    item.dataset.ext = ext;

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

  item.addEventListener('click', () => {
    if (node.type === 'file') {
      document.querySelectorAll('.file-tree-item').forEach(el => el.classList.remove('selected'));
      item.classList.add('selected');
      if (onSelect) onSelect(node);
    }
  });

  container.appendChild(item);

  if (node.type === 'folder' && node.children && node.children.length > 0) {
    childrenContainer = document.createElement('div');
    childrenContainer.className = 'file-tree-children';
    if (!node.expanded) childrenContainer.classList.add('collapsed');

    node.children
      .sort((a, b) => {
        if (a.type !== b.type) return a.type === 'folder' ? -1 : 1;
        return a.name.localeCompare(b.name);
      })
      .forEach(child => renderFileTreeNode(child, childrenContainer, level + 1, onSelect));

    container.appendChild(childrenContainer);
  }
}

function renderFileTree(files, container, onSelect) {
  container.innerHTML = '';

  // Dotfiles/dirs (.cursor, .git-*) are noise for most sessions; the header
  // eye toggle re-renders with state.showDotfiles=true to reveal them.
  if (!state.showDotfiles) {
    files = (files || []).filter(file => {
      const path = String(file.path || file.name || "");
      return !path.split("/").some(segment => segment.length > 1 && segment.startsWith("."));
    });
  }

  if (!files || files.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'file-tree-empty';
    empty.textContent = 'No files found';
    container.appendChild(empty);
    return;
  }

  const tree = buildFileTree(files);
  const treeContainer = document.createElement('div');
  treeContainer.className = 'file-tree';
  renderFileTreeNode(tree, treeContainer, 0, onSelect);
  container.appendChild(treeContainer);
}

export { getFileExtension, renderFileTree };
