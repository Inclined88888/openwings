"""Workspace files: see and download what the agent created.

  GET /wings/files/                     list of files in the agent workspaces
  GET /api/wings/files                  the same list as JSON
  GET /wings/files/download?path=...    one file, always as an attachment

The agent's file tools and the code runner work in /workspaces (the
agent-workspaces volume). Upstream's file tools are admin-only, so this page
is too: a signed-in person (session cookie, not an API token) who is an
admin. Files are never rendered on the app's origin, symlinks are never
followed and nothing outside /workspaces is reachable.
"""
from __future__ import annotations

import html
import os
import stat
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from wings.email_gate import is_human_request

router = APIRouter()
ROOT = "/workspaces"
_SKIP_DIRS = {".tmp", ".cache", "__pycache__", ".git", "node_modules"}
_MAX_DOWNLOAD = 200 * 1024 * 1024


def _allowed(request: Request) -> bool:
    if not is_human_request(request):
        return False
    try:
        from src.auth_helpers import get_current_user
        from src.tool_security import owner_is_admin_or_single_user
        return bool(owner_is_admin_or_single_user(get_current_user(request)))
    except Exception:
        return False


def list_files(limit: int = 500) -> list[dict]:
    root = os.path.realpath(os.environ.get("WINGS_FILES_ROOT", ROOT))
    out = []
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in files:
            path = os.path.join(base, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                out.append({"path": os.path.relpath(path, root), "bytes": st.st_size, "modified": int(st.st_mtime)})
        if len(out) >= 5000:
            break
    out.sort(key=lambda f: f["modified"], reverse=True)
    return out[:limit]


def read_file_bytes(rel: str) -> bytes | None:
    """Bytes of a regular file inside the root, opened without following
    symlinks at any level; None if anything is off."""
    root = os.path.realpath(os.environ.get("WINGS_FILES_ROOT", ROOT))
    parts = [p for p in str(rel or "").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or "\x00" in p for p in parts):
        return None
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        try:
            ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError:
            return None
        try:
            st = os.fstat(ffd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_DOWNLOAD:
                return None
            chunks = []
            while True:
                chunk = os.read(ffd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(ffd)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def download_path(rel: str) -> str:
    return "/wings/files/download?path=" + quote(rel)


@router.get("/api/wings/files")
async def files_json(request: Request):
    if not _allowed(request):
        return JSONResponse({"error": "Workspace files are for signed-in admins."}, status_code=403)
    return {"root": ROOT, "files": list_files()}


@router.get("/wings/files/download", include_in_schema=False)
async def files_download(request: Request, path: str = ""):
    if not _allowed(request):
        return PlainTextResponse("Workspace files are for signed-in admins.", status_code=403)
    data = read_file_bytes(path)
    if data is None:
        return PlainTextResponse("Not found", status_code=404)
    name = os.path.basename(path)
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "no-store", "Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"})


@router.get("/wings/files/", include_in_schema=False)
async def files_page(request: Request):
    if not _allowed(request):
        return PlainTextResponse("Workspace files are for signed-in admins.", status_code=403)
    rows = []
    for item in list_files():
        size = item["bytes"]
        human = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1048576:.1f} MB"
        rows.append(f'<tr><td><a href="{html.escape(download_path(item["path"]))}" download>{html.escape(item["path"])}</a></td>'
                    f'<td>{human}</td><td class="t" data-t="{item["modified"]}"></td></tr>')
    body = "\n".join(rows) or '<tr><td colspan="3" class="muted">No files yet. Files the agent writes appear here.</td></tr>'
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Workspace files · OpenWings</title>
<link rel="stylesheet" href="/static/openwings/skins.css?v=owlooks5">
<style>
:root {{ --bg:#f6f7f9; --panel:#fff; --text:#1d2330; --muted:#5b6474; --line:#dfe3ea; --accent:#2f6fec; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1218; --panel:#171b23; --text:#e6e9ef; --muted:#9aa3b2; --line:#2a303b; --accent:#6d9bff; }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.4 system-ui, sans-serif; }}
.bar {{ display:flex; gap:12px; align-items:center; padding:8px 14px; background:var(--panel); border-bottom:1px solid var(--line); flex-wrap:wrap; }}
a {{ color:var(--accent); }} .muted {{ color:var(--muted); }}
table.files {{ border-collapse:collapse; width:100%; }}
table.files th, table.files td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }}
table.files th {{ color:var(--muted); font-weight:600; }}
</style></head>
<body><header class="bar"><a class="back" href="/" title="Back to OpenWings">← OpenWings</a><strong class="title">Workspace files</strong>
<span class="muted">Everything the agent wrote in {html.escape(ROOT)}, newest first</span></header>
<main style="padding:12px 16px"><table class="files">
<thead><tr><th>File</th><th>Size</th><th>Changed</th></tr></thead><tbody>{body}</tbody></table></main>
<script type="module" src="/wings/files/files.js"></script></body></html>"""
    return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@router.get("/wings/files/files.js", include_in_schema=False)
async def files_js(request: Request):
    if not _allowed(request):
        return PlainTextResponse("", status_code=403)
    js = ("try { const t = JSON.parse(localStorage.getItem('openwings-theme') || 'null'); const n = (t && t.name) || (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'ow-axon-night' : 'ow-axon');"
          " if (/^ow-/.test(n)) document.documentElement.setAttribute('data-ow-skin', n); } catch (_) {}\n"
          "for (const el of document.querySelectorAll('td.t')) el.textContent = new Date(+el.dataset.t * 1000).toLocaleString();\n")
    return Response(js, media_type="text/javascript", headers={"Cache-Control": "no-cache"})
