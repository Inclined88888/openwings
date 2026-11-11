// Readable text of the page for the OpenWings browser's "read" action: headings
// become # lines, list items "* " lines, tables pipe tables, preformatted
// blocks fenced; everything else is plain text in document order. Hidden
// elements are skipped. Read-only.
() => {
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "HEAD", "IFRAME", "CANVAS", "OBJECT"]);
  const lines = [];
  let inline = "";
  let total = 0;
  const LIMIT = 400000;
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const flush = () => {
    const t = clean(inline);
    if (t) { lines.push(t); total += t.length; }
    inline = "";
  };
  const block = (t) => { flush(); if (t) { lines.push(t); total += t.length; } };
  const shown = (el) => {
    const s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && s.visibility !== "collapse";
  };
  const isBlock = (el) => {
    const d = getComputedStyle(el).display;
    return !(d.startsWith("inline") || d === "contents");
  };
  const table = (el) => {
    const rows = Array.from(el.rows || []).slice(0, 300);
    if (!rows.length) return;
    const cellText = (c) => clean(c.innerText).replace(/\|/g, "/").slice(0, 120);
    const width = Math.min(30, Math.max(...rows.map((r) => r.cells.length)));
    const out = [];
    rows.forEach((r, i) => {
      const cells = Array.from(r.cells).slice(0, width).map(cellText);
      while (cells.length < width) cells.push("");
      out.push("| " + cells.join(" | ") + " |");
      if (i === 0) out.push("|" + " --- |".repeat(width));
    });
    if ((el.rows || []).length > 300) out.push(`(${el.rows.length - 300} more rows)`);
    const caption = el.caption ? clean(el.caption.innerText) : "";
    block((caption ? caption + "\n" : "") + out.join("\n"));
  };
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (total > LIMIT) return;
      if (child.nodeType === Node.TEXT_NODE) { inline += " " + child.nodeValue; continue; }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;
      const el = child, tag = el.tagName;
      if (SKIP.has(tag) || !shown(el)) continue;
      if (/^H[1-6]$/.test(tag)) { block("#".repeat(+tag[1]) + " " + clean(el.innerText)); continue; }
      if (tag === "TABLE") { table(el); continue; }
      if (tag === "LI") { block("* " + clean(el.innerText)); continue; }
      if (tag === "PRE") { block("```\n" + (el.innerText || "").slice(0, 20000) + "\n```"); continue; }
      if (tag === "BR") { flush(); continue; }
      if (tag === "IMG") { const alt = clean(el.alt); if (alt) inline += ` [image: ${alt}]`; continue; }
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON") {
        // What a person typed never leaves the page through read (card
        // numbers, one time codes, a password a "show" toggle revealed),
        // matching innerText. Buttons keep their caption, lists their
        // choices (not card fields), other fields only their label.
        const type = String(el.type || "").toLowerCase();
        const auto = String(el.getAttribute("autocomplete") || "").toLowerCase();
        let t = "";
        if (tag === "BUTTON") t = el.innerText;
        else if (tag === "INPUT" && (type === "submit" || type === "button" || type === "reset")) t = el.value;
        else if (tag === "SELECT" && !auto.startsWith("cc-")) t = el.innerText;
        if (!t) t = el.getAttribute("aria-label") || el.getAttribute("placeholder") || "";
        t = clean(t);
        if (t && type !== "hidden") inline += " " + t;
        continue;
      }
      const blk = isBlock(el);
      if (blk) flush();
      walk(el);
      if (el.shadowRoot) walk(el.shadowRoot);
      if (blk) flush();
    }
  };
  if (document.body) walk(document.body);
  flush();
  return lines.join("\n");
}
