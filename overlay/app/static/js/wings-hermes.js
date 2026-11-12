// Wings (patch 0026): Import from Hermes, read only imported chats, and the
// Chat mode note.
//
// * Memory window: an "Import from Hermes" button on the Memories and Add
//   tabs, and a "Hermes imports" card on its Settings tab with Undo.
// * Any link to #import-hermes (the assistant writes one when it is asked
//   about Hermes memory or chats) opens the dialog.
// * The dialog: one line for Terminal that packs only Hermes' memory and
//   past chats (static/openwings/hermes-pack.sh), the file upload or pasted
//   text, a preview with counts and ticks, the import, and Undo.
// * A chat imported from Hermes opens read only: the message box is replaced
//   by one sentence and a "Start a new chat" button.
// * A turn sent in Chat mode gets a one time note that tools are off there.
//
// Back end: wings/hermes_routes.py and wings/hermes_import.py.
import { registerEscapeLayer } from './escMenuStack.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const API = '/api/wings/hermes';
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function injectStyle() {
  if (document.getElementById('ow-hermes-style')) return;
  const st = document.createElement('style');
  st.id = 'ow-hermes-style';
  st.textContent = `
    .ow-hermes-dialog .modal-content { width: 640px; max-width: calc(100vw - 24px); max-height: calc(100dvh - 24px); display: flex; flex-direction: column; }
    .ow-hermes-dialog .modal-body { overflow-y: auto; overscroll-behavior: contain; display: flex; flex-direction: column; gap: 12px; padding-bottom: 14px; }
    .ow-hermes-dialog p { margin: 0; line-height: 1.5; font-size: 13px; }
    .ow-hermes-step { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
    .ow-hermes-step[hidden] { display: none; }
    .ow-hermes-step h5 { margin: 0; font-size: 13px; font-weight: 600; }
    .ow-hermes-cmd { display: flex; gap: 6px; align-items: stretch; }
    .ow-hermes-cmd code { flex: 1; min-width: 0; font-size: 12px; padding: 8px 10px; border-radius: 8px; background: color-mix(in srgb, var(--fg) 6%, transparent); overflow-x: auto; white-space: nowrap; user-select: all; }
    .ow-hermes-small { font-size: 12px; opacity: 0.75; line-height: 1.45; }
    .ow-hermes-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .ow-hermes-dialog textarea { width: 100%; min-height: 70px; resize: vertical; box-sizing: border-box; font: inherit; font-size: 13px; padding: 8px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg); color: var(--fg); }
    .ow-hermes-list { display: flex; flex-direction: column; gap: 4px; max-height: 240px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; padding: 6px; }
    .ow-hermes-item { display: flex; gap: 8px; align-items: flex-start; font-size: 12.5px; line-height: 1.4; padding: 4px; border-radius: 6px; }
    .ow-hermes-item input { margin-top: 2px; flex-shrink: 0; }
    .ow-hermes-item .ow-t { flex: 1; min-width: 0; overflow-wrap: anywhere; }
    .ow-hermes-tag { display: inline-block; font-size: 11px; padding: 0 6px; border-radius: 999px; margin-left: 6px; border: 1px solid var(--border); opacity: 0.85; white-space: nowrap; }
    .ow-hermes-tag.warn { border-color: color-mix(in srgb, #c0392b 60%, var(--border)); color: color-mix(in srgb, #c0392b 80%, var(--fg)); }
    .ow-hermes-summary { font-size: 13px; font-weight: 600; }
    .ow-hermes-msg { font-size: 12.5px; min-height: 1em; }
    .ow-hermes-msg.err { color: color-mix(in srgb, #c0392b 85%, var(--fg)); }
    .ow-hermes-readonly { margin: 0 auto 8px; max-width: 760px; width: calc(100% - 24px); box-sizing: border-box; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: center; font-size: 13px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 12px; background: color-mix(in srgb, var(--fg) 4%, var(--bg)); }
    .ow-hermes-hidden-input { display: none !important; }
    .ow-mode-note { position: fixed; left: 50%; bottom: calc(110px + var(--safe-bottom, 0px)); transform: translateX(-50%); z-index: 400; width: max-content; max-width: min(520px, calc(100vw - 32px)); box-sizing: border-box; font-size: 13px; line-height: 1.45; padding: 10px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--bg); color: var(--fg); box-shadow: 0 6px 24px rgba(0,0,0,.18); display: flex; gap: 10px; align-items: flex-start; }
    .ow-mode-note button { flex-shrink: 0; }
    @media (max-width: 600px) {
      .ow-hermes-cmd { flex-direction: column; }
      .ow-hermes-cmd code { white-space: pre-wrap; word-break: break-all; }
    }
  `;
  document.head.appendChild(st);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, { credentials: 'same-origin', ...opts });
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  if (!res.ok) {
    const detail = body && (typeof body.detail === 'string' ? body.detail : body.error);
    throw new Error(detail || `Something went wrong (${res.status}).`);
  }
  return body || {};
}

// Piping a script into bash is only safe over https or from this same
// computer. On a plain http address in a home network someone in between
// could swap the script, so the person downloads it and runs the saved file.
function secureOrigin() {
  const h = location.hostname;
  return location.protocol === 'https:' || h === 'localhost' || h === '127.0.0.1' || h === '[::1]' || h === '::1';
}

function packCommand() {
  if (!secureOrigin()) return 'bash ~/Downloads/hermes-pack.sh';
  return `curl -fsSL "${location.origin}/static/openwings/hermes-pack.sh" | bash`;
}

function packHelp() {
  if (!secureOrigin()) {
    return `First <a href="/static/openwings/hermes-pack.sh" download="hermes-pack.sh">download the script</a> (it lands in Downloads), then run the line above. You can open it in a text editor to read it first.`;
  }
  return `Want to read it first? <a href="/static/openwings/hermes-pack.sh" target="_blank" rel="noopener">Show the script</a>.`;
}

const MEMORIES_ONLY = 'tar -czf ~/Desktop/openwings-hermes-memory.tar.gz -C "${HERMES_HOME:-$HOME/.hermes}" memories/MEMORY.md memories/USER.md';

function monthRange(a, b) {
  if (!a) return '';
  const f = (s) => new Date(s).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  const x = f(a), y = f(b || a);
  return x === y ? x : `${x} to ${y}`;
}

function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

// ------------------------------------------------------------------ dialog

let _dialog = null;

function closeDialog() { if (_dialog) _dialog.close(); }

export function openHermesImport() {
  injectStyle();
  closeDialog();
  const opener = document.activeElement;
  const overlay = document.createElement('div');
  overlay.id = 'ow-hermes-dialog';
  overlay.className = 'modal ow-acct-dialog ow-hermes-dialog';
  overlay.style.zIndex = '320';
  overlay.innerHTML = `<div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="ow-hermes-title">
      <div class="modal-header"><h4 id="ow-hermes-title">Import from Hermes</h4><button type="button" class="close-btn" data-ow-close aria-label="Close">✖</button></div>
      <div class="modal-body">
        <p>OpenWings runs in its own container, so it cannot open the Hermes folder on your computer. Run this one line in Terminal on the computer where Hermes lives. It packs only your Hermes memory and past chats into one file on your Desktop. It never reads your keys or settings.</p>
        <div class="ow-hermes-step">
          <h5>1. Pack your Hermes memory and chats</h5>
          <div class="ow-hermes-cmd"><code data-ow-cmd>${esc(packCommand())}</code><button type="button" class="admin-btn-add" data-ow-copy="cmd">Copy</button></div>
          <div class="ow-hermes-small">${packHelp()} Memories only, with nothing to install: <code>${esc(MEMORIES_ONLY)}</code></div>
        </div>
        <div class="ow-hermes-step">
          <h5>2. Choose the file it saved</h5>
          <div class="ow-hermes-row"><input type="file" data-ow-file accept=".gz,.tgz,.tar,.zip,.md,.txt,.jsonl,.json" aria-label="Hermes file"></div>
          <div class="ow-hermes-small">Or paste text from Hermes, such as its memory or a conversation:</div>
          <textarea data-ow-paste placeholder="Paste here" aria-label="Pasted Hermes text"></textarea>
          <div class="ow-hermes-row"><button type="button" class="admin-btn-add" data-ow-preview>Preview</button></div>
        </div>
        <div data-ow-result hidden class="ow-hermes-step"></div>
        <div class="ow-hermes-msg" data-ow-msg role="status" aria-live="polite"></div>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.classList.remove('hidden');
  overlay.style.display = 'flex';
  const box = overlay.querySelector('.modal-content');
  const msg = overlay.querySelector('[data-ow-msg]');
  const result = overlay.querySelector('[data-ow-result]');
  const say = (text, err = false) => { msg.textContent = text || ''; msg.classList.toggle('err', !!err); };
  let closed = false;
  let unregister = () => {};
  const onKey = (e) => {
    if (e.key !== 'Tab') return;
    const items = [...box.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  const close = () => {
    if (closed) return;
    closed = true;
    unregister();
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    _dialog = null;
    try { if (opener && opener.isConnected) opener.focus(); } catch (_) {}
  };
  unregister = registerEscapeLayer(() => close(), () => !closed && overlay.isConnected);
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('click', (e) => { if (e.target === overlay || e.target.closest('[data-ow-close]')) close(); });
  _dialog = { close };
  setTimeout(() => overlay.querySelector('[data-ow-copy]')?.focus(), 30);

  overlay.querySelector('[data-ow-copy]').addEventListener('click', async (e) => {
    try { await navigator.clipboard.writeText(packCommand()); e.target.textContent = 'Copied'; }
    catch (_) { say('Select the line and copy it.'); }
  });

  overlay.querySelector('[data-ow-preview]').addEventListener('click', async () => {
    const file = overlay.querySelector('[data-ow-file]').files[0];
    const text = overlay.querySelector('[data-ow-paste]').value;
    if (!file && !text.trim()) { say('Choose the file or paste some text first.', true); return; }
    const fd = new FormData();
    if (file) fd.append('file', file); else fd.append('text', text);
    say('Reading…');
    result.hidden = true;
    try {
      const p = await api('/preview', { method: 'POST', body: fd });
      say('');
      renderPreview(p);
    } catch (err) { say(err.message, true); }
  });

  function renderPreview(p) {
    const c = p.counts || {};
    const profiles = (p.profiles || []).filter((x) => x !== 'pasted');
    const range = monthRange(p.first, p.last);
    const parts = [];
    parts.push(`Found ${plural(c.memories || 0, 'memory', 'memories')}${c.memories_duplicate ? ` (${c.memories_duplicate} already in your memory)` : ''}`);
    parts.push(`${plural(c.chats || 0, 'chat', 'chats')}${c.chats_already ? ` (${c.chats_already} imported before)` : ''}`);
    let summary = parts.join(' and ');
    if (profiles.length > 1) summary += ` from ${profiles.length} profiles`;
    if (range) summary += `, ${range}`;
    summary += '. Choose what to bring in.';
    const notes = [];
    if (c.memories_flagged) notes.push(`${plural(c.memories_flagged, 'memory reads', 'memories read')} like instructions to an assistant, so ${c.memories_flagged === 1 ? 'it is' : 'they are'} not ticked. Imported text is kept as information only.`);
    if (c.secrets_removed) notes.push(`${plural(c.secrets_removed, 'thing that looked like a key or password was', 'things that looked like keys or passwords were')} replaced with [removed].`);
    if (c.ignored_files) notes.push(`${plural(c.ignored_files, 'other file was', 'other files were')} left out and not read: ${esc((p.ignored || []).slice(0, 6).join(', '))}${(p.ignored || []).length > 6 ? ' and more' : ''}.`);
    if (c.chats_skipped_background) notes.push(`${plural(c.chats_skipped_background, 'background run', 'background runs')} (scheduled jobs and helpers) left out.`);
    const mems = (p.memories || []).map((m) => `<label class="ow-hermes-item"><input type="checkbox" data-ow-mem="${esc(m.key)}" ${m.selected ? 'checked' : ''}><span class="ow-t">${esc(m.text)}${m.duplicate ? '<span class="ow-hermes-tag">already in memory</span>' : ''}${m.flag ? `<span class="ow-hermes-tag warn">${esc(m.flag)}</span>` : ''}${m.profile && m.profile !== 'default' && m.profile !== 'pasted' ? `<span class="ow-hermes-tag">${esc(m.profile.replace(/^profile-/, ''))}</span>` : ''}</span></label>`).join('');
    const chats = (p.chats || []).map((ch) => `<label class="ow-hermes-item"><input type="checkbox" data-ow-chat="${esc(ch.key)}" ${ch.selected ? 'checked' : ''}><span class="ow-t"><strong>${esc(ch.title)}</strong> <span class="ow-hermes-small">${esc(ch.started ? new Date(ch.started).toLocaleDateString() : '')}, ${plural(ch.messages, 'message', 'messages')}</span>${ch.already ? '<span class="ow-hermes-tag">imported before</span>' : ''}</span></label>`).join('');
    result.innerHTML = `<div class="ow-hermes-summary" data-ow-summary>${esc(summary)}</div>
      ${notes.map((n) => `<div class="ow-hermes-small">${n}</div>`).join('')}
      ${mems ? `<h5>Memories</h5><div class="ow-hermes-list" data-ow-mems>${mems}</div>` : ''}
      ${chats ? `<h5>Chats</h5><div class="ow-hermes-small">They come in read only, in a From Hermes folder, with their original dates. OpenWings can search them.</div><div class="ow-hermes-list" data-ow-chats>${chats}</div>` : ''}
      <div class="ow-hermes-row"><button type="button" class="admin-btn-add" data-ow-import>Import selected</button></div>`;
    result.hidden = false;
    result.querySelector('[data-ow-import]').addEventListener('click', async (e) => {
      const memories = [...result.querySelectorAll('[data-ow-mem]:checked')].map((x) => x.dataset.owMem);
      const chatsSel = [...result.querySelectorAll('[data-ow-chat]:checked')].map((x) => x.dataset.owChat);
      if (!memories.length && !chatsSel.length) { say('Tick at least one memory or chat.', true); return; }
      e.target.disabled = true;
      say('Importing…');
      try {
        const r = await api('/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: p.token, memories, chats: chatsSel }) });
        renderDone(r);
        refreshApp();
      } catch (err) { e.target.disabled = false; say(err.message, true); }
    });
  }

  function renderDone(r) {
    say('');
    result.innerHTML = `<div class="ow-hermes-summary" data-ow-done>Imported ${esc(plural(r.memories, 'memory', 'memories'))} and ${esc(plural(r.chats, 'chat', 'chats'))}.${r.chats ? ' Imported chats are in the From Hermes folder and are read only.' : ''}</div>
      <div class="ow-hermes-row"><button type="button" class="admin-btn-add" data-ow-done-close>Done</button><button type="button" class="admin-btn-add" data-ow-undo>Undo</button></div>`;
    result.querySelector('[data-ow-done-close]').addEventListener('click', close);
    result.querySelector('[data-ow-undo]').addEventListener('click', async (e) => {
      e.target.disabled = true;
      try {
        const u = await api(`/undo/${encodeURIComponent(r.batch_id)}`, { method: 'POST' });
        result.innerHTML = `<div class="ow-hermes-summary" data-ow-undone>Undone. Removed ${esc(plural(u.memories, 'memory', 'memories'))} and ${esc(plural(u.chats, 'chat', 'chats'))}.</div>`;
        refreshApp();
      } catch (err) { e.target.disabled = false; say(err.message, true); }
    });
  }
}

function refreshApp() {
  import('./memory.js').then((m) => { try { (m.loadMemories || m.default?.loadMemories)?.(); } catch (_) {} }).catch(() => {});
  try { window.sessionModule?.loadSessions?.(); } catch (_) {}
  renderImportsCard();
}

// ------------------------------------------------------------------ Memory window

const ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px;" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';

function addButtons() {
  const row = document.querySelector('[data-memory-panel="browse"] .memory-toolbar-row');
  if (row && !row.querySelector('[data-ow-hermes-open]')) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'memory-toolbar-btn';
    b.dataset.owHermesOpen = 'browse';
    b.title = 'Bring in memories and chats from Hermes';
    b.innerHTML = `${ICON}Import from Hermes`;
    row.appendChild(b);
  }
  const imp = document.getElementById('memory-import-btn');
  if (imp && !imp.parentElement.querySelector('[data-ow-hermes-open]')) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'theme-io-btn';
    b.dataset.owHermesOpen = 'add';
    b.style.cssText = 'height:26px;font-size:12px;';
    b.innerHTML = `${ICON}Import from Hermes`;
    imp.parentElement.insertBefore(b, imp);
  }
  const settings = document.querySelector('[data-memory-panel="settings"]');
  if (settings && !document.getElementById('ow-hermes-imports-card')) {
    const card = document.createElement('div');
    card.className = 'admin-card';
    card.id = 'ow-hermes-imports-card';
    card.dataset.memoryScope = 'memory';
    card.innerHTML = `<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:32px"><h2 style="margin:0">Import from Hermes</h2><button type="button" class="theme-io-btn" data-ow-hermes-open="settings" style="height:26px;font-size:12px;">${ICON}Import</button></div>
      <span class="admin-toggle-sub" style="display:block;margin-top:6px">Bring your Hermes memories and past chats into OpenWings. Only you see what you import.</span>
      <div data-ow-imports style="margin-top:8px;display:flex;flex-direction:column;gap:6px"></div>`;
    settings.insertBefore(card, settings.firstChild);
    renderImportsCard();
  }
}

async function renderImportsCard() {
  const host = document.querySelector('#ow-hermes-imports-card [data-ow-imports]');
  if (!host) return;
  let list = [];
  try { list = (await api('/imports')).imports || []; } catch (_) { host.innerHTML = ''; return; }
  host.innerHTML = list.map((b) => `<div class="ow-hermes-row" style="justify-content:space-between;font-size:12.5px"><span>${esc(b.created_at ? new Date(b.created_at).toLocaleString() : '')}: ${esc(plural(b.memories || 0, 'memory', 'memories'))}, ${esc(plural(b.chats || 0, 'chat', 'chats'))}</span><button type="button" class="theme-io-btn" data-ow-undo-batch="${esc(b.batch_id)}" style="height:24px;font-size:12px">Undo</button></div>`).join('');
}

document.addEventListener('click', async (e) => {
  const open = e.target.closest && e.target.closest('[data-ow-hermes-open], a[href="#import-hermes"]');
  if (open) {
    e.preventDefault();
    e.stopPropagation();
    openHermesImport();
    return;
  }
  const undo = e.target.closest && e.target.closest('[data-ow-undo-batch]');
  if (undo) {
    undo.disabled = true;
    try { await api(`/undo/${encodeURIComponent(undo.dataset.owUndoBatch)}`, { method: 'POST' }); refreshApp(); }
    catch (err) { undo.disabled = false; alert(err.message); }
  }
}, true);

// ------------------------------------------------------------------ read only imported chats

let _roFor = null;
async function checkReadOnly(sessionId) {
  _roFor = sessionId || null;
  const bar = document.querySelector('.chat-input-bar');
  const note = document.getElementById('ow-hermes-readonly');
  const reset = () => { note?.remove(); bar?.classList.remove('ow-hermes-hidden-input'); };
  if (!sessionId) { reset(); return; }
  let ro = null;
  try { ro = await api(`/read-only/${encodeURIComponent(sessionId)}`); } catch (_) { ro = null; }
  if (_roFor !== sessionId) return;
  if (!ro || !ro.read_only) { reset(); return; }
  injectStyle();
  if (!bar) return;
  bar.classList.add('ow-hermes-hidden-input');
  if (!document.getElementById('ow-hermes-readonly')) {
    const n = document.createElement('div');
    n.id = 'ow-hermes-readonly';
    n.className = 'ow-hermes-readonly';
    n.innerHTML = `<span>${esc(ro.message || 'This chat was imported from Hermes and is read only.')}</span><button type="button" class="admin-btn-add" data-ow-newchat>Start a new chat</button>`;
    bar.parentElement.insertBefore(n, bar);
    n.querySelector('[data-ow-newchat]').addEventListener('click', () => {
      (document.getElementById('sidebar-new-chat-btn') || document.getElementById('rail-new-session'))?.click();
    });
  }
}
window.addEventListener('odysseus:session-changed', (e) => checkReadOnly(e.detail && e.detail.sessionId));

// ------------------------------------------------------------------ Chat mode note

let _modeNoted = false;
try { _modeNoted = sessionStorage.getItem('ow-chat-mode-noted') === '1'; } catch (_) {}
function showNote(html) {
  injectStyle();
  const n = document.createElement('div');
  n.className = 'ow-mode-note';
  n.setAttribute('role', 'status');
  n.innerHTML = html + '<button type="button" class="close-btn" aria-label="Close">✖</button>';
  document.body.appendChild(n);
  const remove = () => n.remove();
  n.querySelector('.close-btn').addEventListener('click', remove);
  setTimeout(remove, 20000);
  return { n, remove };
}

// Agent mode on a model that runs without native tool calling: say so once
// per model and session (the server sends the sentence with the turn mode).
window.addEventListener('openwings:turn-mode', (e) => {
  const d = e.detail || {};
  if (d.mode !== 'agent' || !d.tools_note) return;
  const key = 'ow-tools-noted:' + d.tools_note.length + ':' + (window.sessionModule?.getCurrentSessionId?.() || '');
  try { if (sessionStorage.getItem(key) === '1') return; sessionStorage.setItem(key, '1'); } catch (_) {}
  showNote(`<span>${esc(d.tools_note)}</span>`);
});

window.addEventListener('openwings:turn-mode', (e) => {
  const d = e.detail || {};
  if (d.mode !== 'chat' || _modeNoted) return;
  _modeNoted = true;
  try { sessionStorage.setItem('ow-chat-mode-noted', '1'); } catch (_) {}
  injectStyle();
  const n = document.createElement('div');
  n.className = 'ow-mode-note';
  n.setAttribute('role', 'status');
  // Phones hide the Agent and Chat switch, so the note carries its own button.
  n.innerHTML = '<span>Chat mode answers without tools, so OpenWings cannot search your mail, files or the web, or save new memories in it.</span><button type="button" class="admin-btn-add" data-ow-use-agent>Use Agent mode</button><button type="button" class="close-btn" aria-label="Close">✖</button>';
  document.body.appendChild(n);
  const remove = () => n.remove();
  n.querySelector('.close-btn').addEventListener('click', remove);
  n.querySelector('[data-ow-use-agent]').addEventListener('click', () => {
    try { (window.__odysseusSetChatMode || (() => document.getElementById('mode-agent-btn')?.click()))('agent'); } catch (_) {}
    remove();
  });
  setTimeout(remove, 20000);
});

function init() {
  addButtons();
  const modal = document.getElementById('memory-modal');
  if (modal) new MutationObserver(() => addButtons()).observe(modal, { childList: true, subtree: true });
  if (location.hash === '#import-hermes') openHermesImport();
  try { checkReadOnly(window.sessionModule?.getCurrentSessionId?.()); } catch (_) {}
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
window.openHermesImport = openHermesImport;
