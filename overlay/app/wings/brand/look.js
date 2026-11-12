// OpenWings: wear the chosen look on a page outside the app shell (the sign-in
// result page, the "page not found" page). Loaded in <head> before skins.css,
// so the look is on before the first paint. skins.css styles only
// html[data-ow-skin=...]; with nothing saved, the default look (Axon, light or
// Axon Night, following the operating system). An upstream theme leaves the
// page plain.
try {
  const t = JSON.parse(localStorage.getItem('openwings-theme') || 'null');
  const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const n = (t && t.name) || (dark ? 'ow-axon-night' : 'ow-axon');
  if (/^ow-[a-z-]+$/.test(n)) document.documentElement.setAttribute('data-ow-skin', n);
} catch (_) {}
