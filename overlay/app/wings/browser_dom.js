// Page observer injected by the Wings browser backend (wings/browser.py).
// Runs inside one frame. Stamps interactive elements with a ref attribute
// (args.attr, a random name per tab) and
// returns a compact, document-ordered outline: headings, visible text and
// interactive elements, plus scroll and dialog facts. Read-only apart from
// those attributes and window.__wingsDoc.
(args) => {
  const start = args.start | 0;
  const maxItems = args.maxItems | 0;
  const textBudget = args.textBudget | 0;
  const fullPage = !!args.fullPage;
  const query = (args.query || "").toLowerCase();
  const attr = args.attr;
  if (!window.__wingsDoc) {
    window.__wingsDoc = (crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Math.random()).slice(2);
  }
  const vw = window.innerWidth, vh = window.innerHeight;
  const ROLES = new Set(["button", "link", "checkbox", "radio", "tab", "menuitem", "menuitemcheckbox",
    "menuitemradio", "option", "switch", "combobox", "textbox", "searchbox", "slider", "spinbutton",
    "listbox", "treeitem", "gridcell"]);
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "HEAD", "META", "LINK", "IFRAME", "OBJECT"]);
  const clean = (s, n) => {
    s = String(s || "").replace(/\s+/g, " ").trim();
    return n && s.length > n ? s.slice(0, n - 1) + "…" : s;
  };
  let next = start;
  const items = [];
  let textUsed = 0, interactiveCount = 0, below = 0, above = 0, matched = 0;

  const styleCache = new Map();
  const style = (el) => {
    let s = styleCache.get(el);
    if (!s) { s = getComputedStyle(el); styleCache.set(el, s); }
    return s;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return null;
    const s = style(el);
    if (s.visibility === "hidden" || s.visibility === "collapse" || s.display === "none") return null;
    return r;
  };
  const isInteractive = (el) => {
    const tag = el.tagName;
    if (tag === "A") return el.hasAttribute("href");
    if (tag === "BUTTON" || tag === "SELECT" || tag === "TEXTAREA" || tag === "SUMMARY") return true;
    if (tag === "INPUT") return (el.type || "").toLowerCase() !== "hidden";
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role && ROLES.has(role)) return true;
    if (el.isContentEditable && el.getAttribute("contenteditable") !== null) return true;
    if (el.hasAttribute("onclick")) return true;
    const s = style(el);
    if (s.cursor === "pointer") {
      const p = el.parentElement;
      if (!p || style(p).cursor !== "pointer") {
        return !!(el.getAttribute("aria-label") || clean(el.innerText, 80));
      }
    }
    return false;
  };
  const controlFree = (node) => {
    // Text of a node without the text of form controls inside it, so a label
    // wrapping a <select> does not repeat every option.
    const parts = [];
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      let p = n.parentElement, skip = false;
      while (p && p !== node) {
        if (["SELECT", "OPTION", "TEXTAREA", "BUTTON", "SCRIPT", "STYLE"].includes(p.tagName)) { skip = true; break; }
        p = p.parentElement;
      }
      if (!skip) parts.push(n.nodeValue);
    }
    return parts.join(" ");
  };
  const labelOf = (el) => {
    const tag = el.tagName;
    let s = el.getAttribute("aria-label") || "";
    if (!s && el.getAttribute("aria-labelledby")) {
      s = el.getAttribute("aria-labelledby").split(/\s+/).map(id => {
        const n = document.getElementById(id); return n ? n.innerText : "";
      }).join(" ");
    }
    if (!s && el.labels && el.labels.length) s = Array.from(el.labels).map(controlFree).join(" ");
    const type = (el.type || "").toLowerCase();
    if (!s && tag === "INPUT" && ["button", "submit", "reset"].includes(type)) s = el.value;
    if (!s && tag === "IMG") s = el.alt;
    if (!s && tag !== "SELECT" && tag !== "TEXTAREA" && tag !== "INPUT") s = el.innerText || el.textContent || "";
    if (!s) s = el.getAttribute("title") || el.getAttribute("placeholder") || "";
    if (!s) { const img = el.querySelector && el.querySelector("img[alt]"); if (img) s = img.alt; }
    if (!s) { const t = el.querySelector && el.querySelector("svg title"); if (t) s = t.textContent; }
    if (!s && tag === "INPUT" && type === "submit") s = "Submit";
    if (!s) s = el.getAttribute("name") || "";
    return clean(s, 90);
  };
  const roleOf = (el) => {
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role) return role;
    const tag = el.tagName;
    if (tag === "A") return "link";
    if (tag === "BUTTON" || tag === "SUMMARY") return "button";
    if (tag === "SELECT") return el.multiple ? "listbox" : "select";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      if (["button", "submit", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox" || t === "radio") return t;
      if (t === "range") return "slider";
      if (t === "file") return "file";
      if (t === "search") return "searchbox";
      return "textbox";
    }
    if (el.isContentEditable) return "textbox";
    return "clickable";
  };
  const covered = (el, r) => {
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), vw - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 0), vh - 1);
    const root = el.getRootNode && el.getRootNode();
    const top = (root && root.elementFromPoint) ? root.elementFromPoint(cx, cy) : document.elementFromPoint(cx, cy);
    if (!top) return false;
    return !(top === el || el.contains(top) || top.contains(el));
  };
  const describe = (el, r, inView) => {
    const role = roleOf(el);
    const tag = el.tagName;
    const out = { role, name: labelOf(el), tag: tag.toLowerCase() };
    const type = (el.type || "").toLowerCase();
    if (tag === "INPUT" || tag === "TEXTAREA") {
      out.type = type || (tag === "TEXTAREA" ? "textarea" : "text");
      out.autocomplete = el.getAttribute("autocomplete") || "";
      out.fieldName = el.getAttribute("name") || "";
      out.id = el.id || "";
      out.placeholder = el.getAttribute("placeholder") || "";
      if (type === "checkbox" || type === "radio") out.checked = !!el.checked;
      else if (type === "password") out.value = el.value ? "(filled)" : "";
      else if (type !== "file") out.value = clean(el.value, 80);
      if (el.required) out.required = true;
      if (el.readOnly) out.readonly = true;
    } else if (tag === "SELECT") {
      const sel = Array.from(el.selectedOptions || []).map(o => clean(o.text, 40));
      out.value = sel.join(", ");
      out.options = Array.from(el.options || []).slice(0, 12).map(o => clean(o.text, 30));
      out.optionCount = (el.options || []).length;
      if (el.required) out.required = true;
    } else if (el.isContentEditable) {
      out.value = clean(el.innerText, 80);
    }
    const ariaChecked = el.getAttribute("aria-checked");
    if (ariaChecked !== null && out.checked === undefined) out.checked = ariaChecked === "true";
    const expanded = el.getAttribute("aria-expanded");
    if (expanded !== null) out.expanded = expanded === "true";
    const selected = el.getAttribute("aria-selected");
    if (selected === "true") out.selected = true;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") out.disabled = true;
    if (tag === "A") {
      const href = el.getAttribute("href") || "";
      if (href && !href.startsWith("javascript:") && href !== "#") out.href = clean(href, 80);
      if (el.target === "_blank") out.newTab = true;
    }
    if (el.form && (tag === "BUTTON" || (tag === "INPUT" && ["submit", "image"].includes(type)))) {
      out.submits = true;
    }
    if (inView && covered(el, r)) out.covered = true;
    return out;
  };
  const isBlock = (el) => {
    const d = style(el).display;
    return d && !d.startsWith("inline") && d !== "contents" && d !== "none";
  };
  const ownText = (el) => {
    // Text of a leaf block, skipping text inside interactive descendants.
    const parts = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      let p = n.parentElement, skip = false;
      while (p && p !== el) {
        if (SKIP.has(p.tagName) || p.tagName === "LABEL" || p.tagName === "OPTION" || isInteractive(p)) { skip = true; break; }
        p = p.parentElement;
      }
      if (!skip) parts.push(n.nodeValue);
    }
    const text = clean(parts.join(" "), 220);
    return /[\p{L}\p{N}]/u.test(text) ? text : "";
  };
  const hasBlockChild = (el) => {
    for (const c of el.children) { if (!SKIP.has(c.tagName) && isBlock(c)) return true; }
    return false;
  };
  const near = (r) => fullPage || (r.bottom > -vh * 0.5 && r.top < vh * 2);
  const inViewport = (r) => r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;

  let visited = 0;
  const walk = (root) => {
    const stack = [root];
    while (stack.length) {
      const el = stack.pop();
      if (++visited > 25000) break;
      if (!(el instanceof Element)) continue;
      if (SKIP.has(el.tagName)) continue;
      let r = null;
      const interactive = isInteractive(el);
      if (interactive) {
        r = visible(el);
        const s = r ? null : style(el);
        // Visually hidden checkbox/radio/file inputs still work through their label.
        const hiddenInput = !r && el.tagName === "INPUT" && ["checkbox", "radio", "file"].includes((el.type || "").toLowerCase())
          && s.display !== "none";
        if (r || hiddenInput) {
          const rect = r || (el.parentElement ? el.parentElement.getBoundingClientRect() : el.getBoundingClientRect());
          if (near(rect) || query) {
            interactiveCount++;
            let ref = el.getAttribute(attr);
            const info = describe(el, rect, inViewport(rect));
            const hay = (info.name + " " + (info.value || "") + " " + (info.placeholder || "")).toLowerCase();
            if (!query || hay.includes(query)) {
              if (items.length < maxItems) {
                if (!ref || !/^e\d{1,6}$/.test(ref)) { next += 1; ref = "e" + next; el.setAttribute(attr, ref); }
                info.ref = ref;
                info.kind = "el";
                info.inView = inViewport(rect);
                if (!r) info.hiddenInput = true;
                items.push(info);
                if (query) matched++;
              }
            }
          } else if (rect.top >= vh * 2) { below++; } else { above++; }
          // Do not descend into links/buttons: their text is their label.
          if (el.tagName !== "SELECT" && !el.isContentEditable && el.children.length && !["A", "BUTTON"].includes(el.tagName)) {
            for (let i = el.children.length - 1; i >= 0; i--) stack.push(el.children[i]);
          }
          if (el.shadowRoot) for (let i = el.shadowRoot.children.length - 1; i >= 0; i--) stack.push(el.shadowRoot.children[i]);
          continue;
        }
      }
      // Text: headings and leaf blocks in or near the viewport.
      if (textUsed < textBudget && el !== document.body && el !== document.documentElement) {
        const tag = el.tagName;
        const heading = /^H[1-6]$/.test(tag);
        if (heading || (isBlock(el) && !hasBlockChild(el))) {
          r = r || visible(el);
          if (r && (fullPage ? true : inViewport(r)) ) {
            const text = heading ? clean(el.innerText, 160) : ownText(el);
            if (text && (!query || text.toLowerCase().includes(query))) {
              items.push({ kind: heading ? "heading" : "text", level: heading ? +tag[1] : 0, text });
              textUsed += text.length;
              if (query) matched++;
            }
          }
        }
      }
      if (el.shadowRoot) for (let i = el.shadowRoot.children.length - 1; i >= 0; i--) stack.push(el.shadowRoot.children[i]);
      for (let i = el.children.length - 1; i >= 0; i--) stack.push(el.children[i]);
    }
  };
  if (document.body) walk(document.body);

  const se = document.scrollingElement || document.documentElement;
  const dialogEl = document.querySelector("dialog[open], [role=dialog][aria-modal=true], [role=alertdialog]");
  let dialog = null;
  if (dialogEl && visible(dialogEl)) dialog = labelOf(dialogEl) || clean(dialogEl.innerText, 120);
  return {
    doc: window.__wingsDoc,
    next,
    items,
    truncated: visited > 25000,
    interactiveCount,
    below,
    above,
    matched,
    title: document.title || "",
    url: location.href,
    scrollY: Math.round(se.scrollTop),
    scrollHeight: Math.round(se.scrollHeight),
    viewportHeight: vh,
    viewportWidth: vw,
    dialog,
    focused: (document.activeElement && document.activeElement.getAttribute) ? document.activeElement.getAttribute(attr) : null,
  };
}
