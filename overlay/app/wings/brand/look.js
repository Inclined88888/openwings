// OpenWings: wear the chosen look on a page outside the app shell (the sign-in
// result page, the "page not found" page). Loaded in <head> before skins.css,
// so the look is on before the first paint. skins.css styles only
// html[data-ow-skin=...]; with nothing saved, the default look (Lichen). An
// upstream theme leaves the page plain.
try {
  const t = JSON.parse(localStorage.getItem('odysseus-theme') || 'null');
  const n = (t && t.name) || 'ow-sage';
  if (/^ow-[a-z-]+$/.test(n)) document.documentElement.setAttribute('data-ow-skin', n);
} catch (_) {}
