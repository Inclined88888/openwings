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
