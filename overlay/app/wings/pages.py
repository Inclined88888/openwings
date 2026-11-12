"""Small standalone OpenWings pages.

The page a browser sees for an address that does not exist (app.py answers
browser GETs with it instead of upstream's raw JSON). Like the other pages
outside the app shell (Live browser, Workspace files, web search results) it
wears the user's look: static/openwings/look.js marks the look before the
first paint and skins.css colours body:has(> header.bar > a.back). Without a
look it is a plain light or dark page.
"""
from __future__ import annotations

# Bump with the looks' token in index.html, sw.js, login.html and live.html.
LOOKS_VERSION = "owlooks5"

_NOT_FOUND = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Page not found · OpenWings</title>
<link rel="icon" type="image/svg+xml" href="/static/openwings/openwings-tile.svg">
<script src="/static/openwings/look.js?v={LOOKS_VERSION}"></script>
<link rel="stylesheet" href="/static/openwings/skins.css?v={LOOKS_VERSION}">
<style>
:root {{ color-scheme: light dark; --bg:#f6f7f9; --panel:#fff; --text:#1d2330; --muted:#5b6474; --line:#dfe3ea; --accent:#2f6fec; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1218; --panel:#171b23; --text:#e6e9ef; --muted:#9aa3b2; --line:#2a303b; --accent:#6d9bff; }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
.bar {{ display:flex; gap:12px; align-items:center; padding:8px 14px; background:var(--panel); border-bottom:1px solid var(--line); flex-wrap:wrap; }}
.back {{ color:var(--accent); text-decoration:none; }}
main {{ max-width:640px; margin:0 auto; padding:32px 16px; }}
main p {{ margin:0 0 12px; }}
main a {{ color:var(--accent); }}
.muted {{ color:var(--muted); }}
</style></head>
<body class="ow-notfound"><header class="bar"><a class="back" href="/" title="Back to OpenWings">← OpenWings</a><strong class="title">Page not found</strong></header>
<main><p>There is nothing at this address.</p>
<p class="muted">Check the link, or go back to <a href="/">OpenWings</a>.</p></main>
</body></html>"""


def not_found_page() -> str:
    return _NOT_FOUND
