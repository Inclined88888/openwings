// OpenWings live browser viewer. Uses noVNC's RFB client (relayed from the
// browser container by the OpenWings app) over an authenticated WebSocket.
import RFB from "./novnc/core/rfb.js";

// OpenWings: wear the chosen look (static/openwings/skins.css styles only
// html[data-ow-skin=...]); with nothing saved, the default look.
try { const t = JSON.parse(localStorage.getItem('openwings-theme') || 'null'); const n = (t && t.name) || 'ow-sage';
  if (/^ow-/.test(n)) document.documentElement.setAttribute('data-ow-skin', n); } catch (_) {}

const screen = document.getElementById("screen");
const statusEl = document.getElementById("status");
const viewOnly = document.getElementById("viewonly");
const scale = document.getElementById("scale");
const reconnect = document.getElementById("reconnect");
const typeText = document.getElementById("typetext");
const showText = document.getElementById("showtext");
let rfb = null;
let retries = 0;

const setStatus = (text, cls) => {
  statusEl.textContent = text;
  statusEl.className = "status " + cls;
};

function socketUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const base = location.pathname.endsWith("/") ? location.pathname : location.pathname + "/";
  return `${proto}//${location.host}${base}websockify`;
}

function connect() {
  setStatus("Connecting…", "connecting");
  reconnect.hidden = true;
  rfb = new RFB(screen, socketUrl(), { shared: true });
  rfb.scaleViewport = scale.checked;
  rfb.resizeSession = false;
  rfb.viewOnly = viewOnly.checked;
  rfb.focusOnClick = true;
  rfb.showDotCursor = true;
  rfb.addEventListener("connect", () => {
    retries = 0;
    setStatus("Live", "live");
  });
  rfb.addEventListener("disconnect", (event) => {
    rfb = null;
    setStatus(event.detail && event.detail.clean ? "Disconnected" : "Connection lost", "down");
    reconnect.hidden = false;
    if (retries < 5) {
      retries += 1;
      setTimeout(connect, 1500 * retries);
    }
  });
  rfb.addEventListener("credentialsrequired", () => {
    setStatus("The browser asked for a VNC password (unexpected)", "down");
  });
}

viewOnly.addEventListener("change", () => { if (rfb) rfb.viewOnly = viewOnly.checked; });
scale.addEventListener("change", () => { if (rfb) rfb.scaleViewport = scale.checked; });
reconnect.addEventListener("click", () => { retries = 0; if (!rfb) connect(); });
showText.addEventListener("change", () => { typeText.type = showText.checked ? "text" : "password"; });

// X11 keysyms: Latin-1 characters map to themselves, other Unicode
// characters to 0x01000000 + code point. Newline becomes Return.
function keysymFor(ch) {
  if (ch === "\n") return 0xff0d;
  if (ch === "\t") return 0xff09;
  const cp = ch.codePointAt(0);
  return cp < 0x100 ? cp : 0x01000000 + cp;
}

document.getElementById("typeit").addEventListener("click", async () => {
  if (!rfb || rfb.viewOnly) return;
  const text = typeText.value;
  typeText.value = "";
  for (const ch of text) {
    rfb.sendKey(keysymFor(ch), null);
    await new Promise((r) => setTimeout(r, 15));
  }
  screen.focus();
});

document.getElementById("cliptext").addEventListener("click", () => {
  if (!rfb) return;
  rfb.clipboardPasteFrom(typeText.value);
  typeText.value = "";
});

// Files the browser downloaded, newest first. Links download through OpenWings.
const dlList = document.getElementById("dllist");
const dlCount = document.getElementById("dlcount");
let dlSignature = "";

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

async function refreshDownloads() {
  let data;
  try {
    const res = await fetch("../../api/wings/browser/downloads", { credentials: "same-origin" });
    if (!res.ok) return;
    data = await res.json();
  } catch (e) {
    return;
  }
  const items = (data && data.downloads) || [];
  const signature = JSON.stringify(items);
  if (signature === dlSignature) return;
  dlSignature = signature;
  dlCount.textContent = items.length ? `(${items.length})` : "";
  dlList.replaceChildren();
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "Nothing downloaded yet.";
    dlList.appendChild(li);
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = "downloads/" + encodeURIComponent(item.name);
    a.textContent = item.name;
    a.setAttribute("download", item.name);
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = ` ${formatSize(item.size)}, ${new Date(item.modified * 1000).toLocaleString()}`;
    li.append(a, meta);
    dlList.appendChild(li);
  }
}

refreshDownloads();
setInterval(refreshDownloads, 10000);

connect();
