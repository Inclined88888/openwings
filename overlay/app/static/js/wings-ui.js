// Wings: sidebar entry for Agents (patch 0006).
// Opens the persona editor on its Character tab, where an agent's
// instructions and (patch 0005) tool access are set. Same module URL as
// app.js, so this is the same presets instance the rest of the app uses.
import presetsModule from './presets.js?v=20260908personaname1';

function openAgents() {
  if (presetsModule && typeof presetsModule.openCustomPresetModal === 'function') {
    presetsModule.openCustomPresetModal('character');
  }
}

function wire() {
  const btn = document.getElementById('tool-agents-btn');
  if (!btn || btn.dataset.wingsWired) return;
  btn.dataset.wingsWired = '1';
  btn.addEventListener('click', openAgents);
  btn.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openAgents();
    }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', wire);
} else {
  wire();
}

// Wings: sidebar entry for the live browser (patch 0008). Shown only when the
// browser container is enabled and this account may use it. Opens the live
// view in its own tab so the chat stays usable while you watch or take over.
const LIVE_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;opacity:0.5;" '
  + 'aria-hidden="true"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M3 8h18"/>'
  + '<circle cx="6" cy="6" r="0.6"/><circle cx="8.5" cy="6" r="0.6"/><path d="M8 22h8M12 18v4"/></svg>';

function openLiveBrowser() {
  window.open('/wings/browser/', 'wings-live-browser', 'noopener');
}

async function addLiveBrowserEntry() {
  if (document.getElementById('wings-live-browser-btn')) return;
  const anchor = document.getElementById('tool-agents-btn');
  if (!anchor) return;
  let status;
  try {
    const res = await fetch('/api/wings/browser/status', { credentials: 'same-origin' });
    if (!res.ok) return;
    status = await res.json();
  } catch (e) {
    return;
  }
  if (!status || !status.enabled || !status.allowed) return;
  const item = document.createElement('div');
  item.className = 'list-item';
  item.id = 'wings-live-browser-btn';
  item.setAttribute('role', 'button');
  item.tabIndex = 0;
  item.title = 'Live browser: watch the agent browse, sign in, or take over';
  item.innerHTML = LIVE_ICON;
  const label = document.createElement('span');
  label.className = 'grow';
  label.textContent = 'Live browser';
  item.appendChild(label);
  item.addEventListener('click', openLiveBrowser);
  item.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openLiveBrowser();
    }
  });
  anchor.insertAdjacentElement('afterend', item);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', addLiveBrowserEntry);
} else {
  addLiveBrowserEntry();
}

// Wings: sidebar entry for the files the agent wrote in its workspace
// (patch 0009). Upstream's file and code tools are admin-only, so the page
// is too; the entry only appears when the list loads.
const FILES_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;opacity:0.5;" '
  + 'aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

async function addWorkspaceFilesEntry() {
  if (document.getElementById('wings-workspace-files-btn')) return;
  const anchor = document.getElementById('tool-agents-btn');
  if (!anchor) return;
  try {
    const res = await fetch('/api/wings/files', { credentials: 'same-origin' });
    if (!res.ok) return;
  } catch (e) {
    return;
  }
  const item = document.createElement('div');
  item.className = 'list-item';
  item.id = 'wings-workspace-files-btn';
  item.setAttribute('role', 'button');
  item.tabIndex = 0;
  item.title = 'Workspace files: download what the agent created';
  item.innerHTML = FILES_ICON;
  const label = document.createElement('span');
  label.className = 'grow';
  label.textContent = 'Workspace files';
  item.appendChild(label);
  const open = () => window.open('/wings/files/', 'wings-workspace-files', 'noopener');
  item.addEventListener('click', open);
  item.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open();
    }
  });
  anchor.insertAdjacentElement('afterend', item);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', addWorkspaceFilesEntry);
} else {
  addWorkspaceFilesEntry();
}
