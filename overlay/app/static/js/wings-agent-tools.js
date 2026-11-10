// Wings: per-agent tool access in the persona editor (patch 0005).
//
// Adds a "Tool access" section under the system prompt. When "Limit this
// agent's tools" is on, only the checked tool families are offered to the
// model for chats with this agent; the server enforces it (wings/agent_tools.py).
// Value semantics: null = no restriction, [] = no tools, [ids] = those families.

const FALLBACK_FAMILIES = [
  { id: 'web', label: 'Web search and reading' },
  { id: 'notes', label: 'Notes' },
  { id: 'tasks', label: 'Tasks and schedules' },
  { id: 'calendar', label: 'Calendar' },
  { id: 'email', label: 'Email (sending always needs your click)' },
  { id: 'contacts', label: 'Contacts' },
  { id: 'documents', label: 'Documents' },
  { id: 'memory', label: 'Memory, skills and past chats' },
  { id: 'research', label: 'Deep research' },
  { id: 'files', label: 'Read files and media' },
  { id: 'images', label: 'Image generation and editing' },
  { id: 'shell', label: 'Shell, code and file writes (admin)' },
  { id: 'sessions', label: 'Other chats and models' },
  { id: 'admin', label: 'Admin, models and settings' },
  { id: 'external_mcp', label: 'MCP servers you added' },
];

let _families = null;
let _pending = null;
// True once the checkboxes show _pending. Until then read() returns _pending,
// so saving before the family list loads can never erase a saved limit.
let _applied = false;

async function _loadFamilies(apiBase) {
  if (_families) return _families;
  try {
    const res = await fetch(`${apiBase || ''}/api/tools`, { credentials: 'same-origin' });
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data.agent_families) && data.agent_families.length) {
        _families = data.agent_families;
        return _families;
      }
    }
  } catch (_) { /* fall back below */ }
  _families = FALLBACK_FAMILIES;
  return _families;
}

function _injectStyles() {
  if (document.getElementById('wings-tool-access-style')) return;
  const style = document.createElement('style');
  style.id = 'wings-tool-access-style';
  style.textContent = `
    #wings-tool-access.wings-tool-access { margin: 10px 0 0; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; min-width: 0; }
    #wings-tool-access label { display: flex !important; flex-direction: row !important; align-items: center; gap: 8px; margin: 0; cursor: pointer; text-align: left; }
    #wings-tool-access > label { font-weight: 600; }
    #wings-tool-access input[type="checkbox"] { display: inline-block !important; width: auto !important; height: auto; margin: 0 !important; flex: 0 0 auto; }
    #wings-tool-access .wings-ta-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 6px 14px; margin-top: 8px; }
    #wings-tool-access .wings-ta-grid[hidden] { display: none; }
    #wings-tool-access .wings-ta-grid label { font-size: 0.9em; }
    #wings-tool-access .wings-ta-hint { margin: 6px 0 0; font-size: 0.8em; color: var(--color-muted); }
    #wings-tool-access input:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }
  `;
  document.head.appendChild(style);
}

function _mount() {
  let root = document.getElementById('wings-tool-access');
  if (root) return root;
  const prompt = document.getElementById('custom-system-prompt');
  const anchor = prompt ? (prompt.closest('.char-prompt-wrap') || prompt) : null;
  if (!anchor || !anchor.parentNode) return null;
  _injectStyles();
  root = document.createElement('fieldset');
  root.id = 'wings-tool-access';
  root.className = 'wings-tool-access';
  root.innerHTML = `
    <label for="wings-ta-restrict"><input type="checkbox" id="wings-ta-restrict"> Limit this agent's tools</label>
    <div class="wings-ta-grid" id="wings-ta-grid" role="group" aria-label="Allowed tool families" hidden></div>
    <p class="wings-ta-hint" id="wings-ta-hint">Off: the agent can use every tool your account can. On: only the checked families, enforced by the server.</p>`;
  anchor.parentNode.insertBefore(root, anchor.nextSibling);
  root.querySelector('#wings-ta-restrict').addEventListener('change', (e) => {
    root.querySelector('#wings-ta-grid').hidden = !e.target.checked;
  });
  return root;
}

function _apply(root, families, value) {
  const grid = root.querySelector('#wings-ta-grid');
  const restrict = root.querySelector('#wings-ta-restrict');
  if (!grid.childElementCount) {
    for (const fam of families) {
      const id = `wings-ta-${fam.id}`;
      const label = document.createElement('label');
      label.setAttribute('for', id);
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.id = id;
      box.value = fam.id;
      label.appendChild(box);
      label.appendChild(document.createTextNode(' ' + fam.label));
      grid.appendChild(label);
    }
  }
  const limited = Array.isArray(value);
  restrict.checked = limited;
  grid.hidden = !limited;
  const chosen = new Set(limited ? value : []);
  grid.querySelectorAll('input[type="checkbox"]').forEach((box) => { box.checked = chosen.has(box.value); });
}

/** Show the given value (null | string[]) in the editor. */
export function render(value, apiBase = '') {
  _pending = Array.isArray(value) ? value.slice() : null;
  _applied = false;
  const root = _mount();
  if (!root) return;
  const token = _pending;
  _loadFamilies(apiBase).then((families) => {
    if (token !== _pending) return;  // a newer render() won
    _apply(root, families, _pending);
    _applied = true;
  });
}

/** Current editor value: null (no restriction) or the checked family ids. */
export function read() {
  const root = document.getElementById('wings-tool-access');
  if (!root || !_applied) return _pending;
  if (!root.querySelector('#wings-ta-restrict').checked) return null;
  return Array.from(root.querySelectorAll('#wings-ta-grid input[type="checkbox"]:checked')).map((b) => b.value);
}
