"""Search routes — /api/search/config GET, /api/search POST."""

import html
import json
import logging
from typing import Dict, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

import time

from services.search import get_search_config, comprehensive_web_search, PROVIDER_INFO
from services.search.core import _call_provider
from services.search.providers import _get_provider_key, _get_search_instance

logger = logging.getLogger(__name__)


async def _request_values(request: Request) -> Dict[str, Any]:
    """Accept JSON, form data, or query params for search endpoints.

    The browser UI posts FormData, while the agent's generic app_api tool
    posts JSON. FastAPI Form(...) rejects JSON with a 422 before our handler
    runs, which made the model think SearXNG was broken.
    """
    values: Dict[str, Any] = dict(request.query_params)
    content_type = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict):
                values.update(body)
        else:
            form = await request.form()
            values.update(dict(form))
    except Exception:
        pass
    return values


def setup_search_routes(config) -> APIRouter:
    router = APIRouter(tags=["search"])

    @router.get("/search/web", response_class=HTMLResponse)
    async def web_search_page(request: Request, q: str = Query("", min_length=0)) -> HTMLResponse:
        """Browser-facing search results page for clickable agent web_search rows."""
        # The site CSP allows inline script only with the per-request nonce
        # (SecurityHeadersMiddleware); without it this page's script never ran.
        nonce = html.escape(getattr(request.state, "csp_nonce", "") or "", quote=True)
        safe_q = str(q or "").strip()
        title = html.escape(safe_q or "Web search")
        # json.dumps leaves `<`, `>` and `&` alone, so q=</script><script ...>
        # would close this inline <script> and inject markup (reflected XSS).
        # Unicode-escape them; the JS string value is unchanged.
        q_json = (
            json.dumps(safe_q)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        # Wings: the page wears the user's OpenWings look (static/openwings/
        # skins.css styles html[data-ow-skin=...]; the look's "own pages"
        # rules colour body:has(> header.bar > a.back)). Its one inline
        # script sets the look before the first paint, then runs the search
        # once the page has loaded. Without a look: a plain light/dark page.
        page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title} · OpenWings search</title>
  <link rel="icon" type="image/svg+xml" href="/static/openwings/openwings-tile.svg">
  <link rel="stylesheet" href="/static/openwings/skins.css?v=owlooks4">
  <style>
    :root {{ color-scheme: light dark; --bg:#f6f7f9; --panel:#fff; --text:#1d2330; --muted:#5b6474; --line:#dfe3ea; --accent:#2f6fec; --on-accent:#fff; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1218; --panel:#171b23; --text:#e6e9ef; --muted:#9aa3b2; --line:#2a303b; --accent:#6d9bff; --on-accent:#0f1218; }} }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
    .bar {{ display:flex; gap:12px; align-items:center; padding:8px 14px; background:var(--panel); border-bottom:1px solid var(--line); flex-wrap:wrap; }}
    .bar .title {{ font-size:15px; }}
    .back {{ color:var(--accent); text-decoration:none; }}
    main {{ max-width:860px; margin:0 auto; padding:20px 16px 40px; }}
    form {{ display:flex; gap:8px; margin:0 0 12px; }}
    input {{ flex:1; min-width:0; height:38px; padding:0 12px; border:1px solid var(--muted); border-radius:6px; background:var(--panel); color:var(--text); font:inherit; }}
    button {{ height:38px; padding:0 16px; border:0; border-radius:6px; background:var(--accent); color:var(--on-accent); font:inherit; font-weight:600; cursor:pointer; }}
    :focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
    .status {{ color:var(--muted); font-size:14px; margin:8px 0 12px; }}
    .result {{ display:block; padding:12px 0; border-top:1px solid var(--line); text-decoration:none; color:inherit; }}
    .result-title {{ color:var(--text); font-weight:600; }}
    .result:hover .result-title, .result:focus-visible .result-title {{ text-decoration:underline; text-underline-offset:3px; }}
    .result-url {{ margin-top:2px; color:var(--muted); font-size:14px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
    .result-snippet {{ margin-top:4px; color:var(--text); font-size:14px; }}
  </style>
  <script nonce="{nonce}">
    try {{ const t = JSON.parse(localStorage.getItem('openwings-theme') || 'null'); const n = (t && t.name) || 'ow-sage';
      if (/^ow-[a-z-]+$/.test(n)) document.documentElement.setAttribute('data-ow-skin', n); }} catch (_) {{}}
    const initialQuery = {q_json};
    function esc(value) {{
      return String(value || '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
    }}
    async function runSearch(query) {{
      const statusEl = document.getElementById('status');
      const resultsEl = document.getElementById('results');
      query = String(query || '').trim();
      if (!query) {{ statusEl.textContent = 'Enter a search query.'; resultsEl.innerHTML = ''; return; }}
      statusEl.textContent = 'Searching…';
      resultsEl.innerHTML = '';
      const fd = new FormData();
      fd.append('query', query);
      const res = await fetch('/api/search', {{ method: 'POST', credentials: 'same-origin', body: fd }});
      const data = await res.json().catch(() => ({{}}));
      const sources = Array.isArray(data.sources) ? data.sources : [];
      if (!res.ok || data.error) {{
        statusEl.textContent = data.error || `Search failed (${{res.status}})`;
        return;
      }}
      statusEl.textContent = sources.length ? `${{sources.length}} results` : 'No results';
      resultsEl.innerHTML = sources.map(s => {{
        const rawUrl = String(s.url || s.link || '');
        // Result URLs come from third-party search results: only http(s) may become a link.
        const url = /^https?:[/][/]/i.test(rawUrl) ? rawUrl : '';
        const title = s.title || url || 'Untitled';
        const snippet = s.snippet || s.content || '';
        return `<a class="result" href="${{esc(url)}}" target="_blank" rel="noopener noreferrer">
          <div class="result-title">${{esc(title)}}</div>
          <div class="result-url">${{esc(url)}}</div>
          <div class="result-snippet">${{esc(snippet)}}</div>
        </a>`;
      }}).join('');
    }}
    document.addEventListener('DOMContentLoaded', () => {{
      const input = document.getElementById('query');
      document.getElementById('search-form').addEventListener('submit', ev => {{
        ev.preventDefault();
        const q = input.value.trim();
        const url = new URL(window.location.href);
        url.searchParams.set('q', q);
        history.replaceState(null, '', url);
        runSearch(q);
      }});
      runSearch(initialQuery);
    }});
  </script>
</head>
<body>
  <header class="bar"><a class="back" href="/" title="Back to OpenWings">← OpenWings</a><strong class="title">Web search</strong></header>
  <main>
    <form id="search-form" role="search">
      <input id="query" type="search" value="{html.escape(safe_q, quote=True)}" autocomplete="off" aria-label="Search the web">
      <button type="submit">Search</button>
    </form>
    <div class="status" id="status" role="status" aria-live="polite">Loading…</div>
    <div id="results"></div>
  </main>
</body>
</html>"""
        return HTMLResponse(page)

    @router.get("/api/search/config")
    async def get_search_settings() -> Dict[str, Any]:
        return get_search_config()

    @router.post("/api/search")
    async def do_web_search(request: Request) -> Dict[str, Any]:
        """Standalone web search — returns context string + source list.

        Used by Compare mode to pre-search once and share results across panes.
        """
        values = await _request_values(request)
        query = str(values.get("query") or values.get("q") or "").strip()
        if not query:
            return {"context": "", "sources": [], "error": "query is required"}
        time_filter = values.get("time_filter") or values.get("freshness")
        if time_filter is not None:
            time_filter = str(time_filter).strip() or None
        try:
            context, sources = comprehensive_web_search(
                query, return_sources=True, time_filter=time_filter,
            )
            return {"context": context, "sources": sources}
        except Exception as e:
            logger.error(f"Standalone web search failed: {e}")
            return {"context": "", "sources": [], "error": str(e)}

    @router.get("/api/search/providers")
    async def list_search_providers():
        """Return available search providers with config status."""
        providers = []
        for pid, (label, needs_key, needs_url) in PROVIDER_INFO.items():
            if pid == "disabled":
                continue
            available = True
            if needs_key and not _get_provider_key(pid):
                available = False
            if needs_url and pid == "searxng" and not _get_search_instance():
                available = False
            providers.append({
                "id": pid,
                "label": label,
                "available": available,
            })
        return providers

    @router.post("/api/search/query")
    async def search_with_provider(request: Request) -> Dict[str, Any]:
        """Search using a specific provider. Used by compare search mode."""
        values = await _request_values(request)
        query = str(values.get("query") or values.get("q") or "").strip()
        provider = str(values.get("provider") or "").strip()
        try:
            count = int(values.get("count") or values.get("limit") or 10)
        except Exception:
            count = 10
        if not query:
            return {"results": [], "provider": provider, "error": "query is required"}
        if provider not in PROVIDER_INFO or provider == "disabled":
            return {"results": [], "provider": provider, "error": "Unknown provider"}
        t0 = time.time()
        try:
            results = _call_provider(provider, query, min(count, 20))
            elapsed = round(time.time() - t0, 2)
            return {"results": results, "provider": provider, "time": elapsed}
        except Exception as e:
            elapsed = round(time.time() - t0, 2)
            logger.error(f"Search provider {provider} failed: {e}")
            return {"results": [], "provider": provider, "time": elapsed, "error": str(e)}

    return router
