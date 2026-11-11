"""Consistency checks for the OpenWings looks (patches 0020 and 0023): every
ow-* theme in static/js/theme.js has its look stylesheet, a display name and
matching first paint colours in skins.css, the default looks the boot scripts
name exist, the theme picker lists only the looks unless
WINGS_SHOW_CLASSIC_THEMES=1, no upstream font is used under a look, and busy
states have the neutral mark. Run with ./tests/unit/run.sh (inside the built
image, network off)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path("/app/static")
THEME_JS = STATIC / "js" / "theme.js"
SKINS = STATIC / "openwings" / "skins.css"
KEYS = ("bg", "fg", "panel", "border", "red")
# One mode for everyone: these looks have no -night variant (owner's ruling).
SINGLE_MODE_LOOKS = {"ow-sage"}


def _themes() -> dict[str, dict[str, str]]:
    body = re.search(r"export const THEMES = \{(.*?)\n\};", THEME_JS.read_text(), re.S).group(1)
    out = {}
    for name, colours in re.findall(r"'(ow-[a-z-]+)':\s*\{([^}]*)\}", body):
        out[name] = {k: v.lower() for k, v in re.findall(r"(\w+):'(#[0-9A-Fa-f]{6})'", colours)}
    return out


def _first_paint() -> dict[str, dict[str, str]]:
    out = {}
    for name, decls in re.findall(r'html\[data-ow-skin="(ow-[a-z-]+)"\]\s*\{([^}]*)\}', SKINS.read_text()):
        found = {k: v.lower() for k, v in re.findall(r"--(bg|fg|panel|border|red):\s*(#[0-9A-Fa-f]{6})", decls)}
        if found:
            out[name] = found
    return out


@pytest.fixture(scope="module")
def themes():
    found = _themes()
    assert found, "no ow-* themes in theme.js"
    return found


def test_every_look_has_day_and_night_with_full_colours(themes):
    for name, colours in themes.items():
        assert set(colours) == set(KEYS), name
        base = name[: -len("-night")] if name.endswith("-night") else name
        if base in SINGLE_MODE_LOOKS:
            assert base + "-night" not in themes, name
            continue
        assert base in themes and base + "-night" in themes, name


def test_every_look_has_its_stylesheet_imported(themes):
    imports = re.findall(r'@import url\("looks/([a-z-]+)\.css"\)', SKINS.read_text())
    for name in themes:
        look = name[3:].removesuffix("-night")
        assert look in imports, name
        assert (STATIC / "openwings" / "looks" / f"{look}.css").is_file(), name


def test_first_paint_colours_match_the_theme_table(themes):
    assert _first_paint() == themes


def test_every_look_has_a_display_name(themes):
    names = re.search(r"const THEME_DISPLAY_NAMES = \{(.*?)\};", THEME_JS.read_text(), re.S).group(1)
    for name in themes:
        assert f"'{name}':" in names, name


def test_boot_defaults_name_existing_looks(themes):
    default = re.search(r"const OW_DEFAULT_LOOK = '(ow-[a-z-]+)';", THEME_JS.read_text()).group(1)
    assert default in themes
    for page in ("index.html", "login.html"):
        text = (STATIC / page).read_text()
        if default in SINGLE_MODE_LOOKS:
            assert f"var owName = (t && t.name) || '{default}';" in text, page
        else:
            assert default + "-night" in themes
            pair = re.search(r"\? '(ow-[a-z-]+)' : '(ow-[a-z-]+)'\)", text)
            assert pair and pair.groups() == (default, default + "-night"), page


def test_motion_script_is_loaded_by_the_app_page():
    assert '<script src="/static/openwings/motion.js' in (STATIC / "index.html").read_text()
    assert (STATIC / "openwings" / "motion.js").is_file()


def test_fonts_are_self_hosted_with_licences():
    fonts = STATIC / "openwings" / "fonts"
    css = "\n".join(p.read_text() for p in (STATIC / "openwings" / "looks").glob("*.css"))
    # Nothing is fetched from another site (inline SVG namespaces are fine).
    assert not re.search(r"""(url\(\s*['"]?|@import\s+['"])(https?:)?//""", css + SKINS.read_text())
    used = set(re.findall(r"""url\(['"]?\.\./fonts/([^'")]+)['"]?\)""", css))
    assert used, "no fonts referenced"
    for file in used:
        assert (fonts / file).is_file(), file
        family = re.sub(r"-latin.*$", "", file)
        assert (fonts / f"{family}.LICENSE.txt").is_file(), family


def test_picker_lists_the_looks_first_and_hides_classic_themes(themes):
    js = THEME_JS.read_text()
    order = re.findall(r"'(ow-[a-z-]+)'", re.search(r"const OW_PICKER_ORDER = \[(.*?)\];", js).group(1))
    default = re.search(r"const OW_DEFAULT_LOOK = '(ow-[a-z-]+)';", js).group(1)
    assert order[0] == default and sorted(order) == sorted(themes)
    # Upstream themes stay available (a saved one keeps working).
    assert "dark:" in re.search(r"export const THEMES = \{(.*?)\n\};", js, re.S).group(1)
    assert 'data-ow-classic-themes="{{WINGS_CLASSIC_THEMES}}"' in (STATIC / "index.html").read_text()


@pytest.mark.parametrize("value,flag", [("", "0"), ("0", "0"), ("1", "1"), ("true", "1")])
def test_classic_themes_flag_is_filled_in_when_pages_are_served(monkeypatch, value, flag):
    import sys
    from types import SimpleNamespace
    sys.path.insert(0, "/app")
    from src.app_helpers import serve_html_with_nonce
    monkeypatch.setenv("WINGS_SHOW_CLASSIC_THEMES", value)
    page = serve_html_with_nonce(SimpleNamespace(state=SimpleNamespace(csp_nonce="n")), str(STATIC / "index.html"))
    assert f'data-ow-classic-themes="{flag}"'.encode() in page.body


def test_no_upstream_font_under_a_look():
    skins = SKINS.read_text()
    for look in ("sage", "duet", "hush"):
        css = (STATIC / "openwings" / "looks" / f"{look}.css").read_text()
        assert re.search(r"--font-family:\s*var\(--[a-z-]+\)\s*!important;", css), look
        assert "--ow-font-sans:" in css and "--ow-font-mono:" in css, look
    assert "var(--ow-font-mono" in skins and "var(--ow-font-sans" in skins
    index = (STATIC / "index.html").read_text()
    # Fira Code is preloaded from the boot script for upstream themes only.
    assert not re.search(r'<link rel="preload"[^>]*FiraCode', index)
    for page in ("index.html", "login.html"):
        assert re.search(r'<link rel="stylesheet" href="/?static/openwings/skins\.css', (STATIC / page).read_text()), page


def test_busy_states_have_a_neutral_mark_under_the_looks():
    spinner = (STATIC / "js" / "spinner.js").read_text()
    assert "_owLook()" in spinner and "ow-busy" in spinner
    skins = SKINS.read_text()
    assert ".ow-busy::before" in skins and "@keyframes ow-busy-breathe" in skins
    for cls in (".agent-thread-wave", ".compact-wave", ".cookbook-task-wave", ".compare-probe-spinner", "#loader-wave"):
        assert cls in skins, cls


def _looks_token() -> str:
    m = re.search(r'href="/?static/openwings/skins\.css\?v=([A-Za-z0-9]+)"', (STATIC / "index.html").read_text())
    assert m, "index.html does not link skins.css with a version token"
    return m.group(1)


def test_every_page_asks_for_the_looks_with_one_token_of_their_own():
    token = _looks_token()
    # The looks have their own cache-bust token, not upstream's shared CSS one.
    assert "csssplit" not in token
    sw = (STATIC / "sw.js").read_text()
    assert f"'/static/openwings/skins.css?v={token}'" in sw
    pages = {
        "login.html": (STATIC / "login.html").read_text(),
        "live.html": Path("/app/wings/static/live.html").read_text(),
        "files_routes.py": Path("/app/wings/files_routes.py").read_text(),
        "search_routes.py": Path("/app/routes/search/search_routes.py").read_text(),
        "mcp_routes.py": Path("/app/routes/mcp/mcp_routes.py").read_text(),
    }
    for name, text in pages.items():
        found = set(re.findall(r"openwings/skins\.css\?v=([A-Za-z0-9]+)", text))
        assert found == {token}, (name, found)
    from wings.pages import LOOKS_VERSION, not_found_page
    assert LOOKS_VERSION == token
    page = not_found_page()
    assert f"skins.css?v={token}" in page and "/static/openwings/look.js" in page
    assert (STATIC / "openwings" / "look.js").is_file()


def test_installed_app_and_login_use_the_default_looks_colour(themes):
    import json
    default = re.search(r"const OW_DEFAULT_LOOK = '(ow-[a-z-]+)';", THEME_JS.read_text()).group(1)
    bg = themes[default]["bg"]
    manifest = json.loads((STATIC / "manifest.json").read_text())
    assert manifest["background_color"].lower() == bg and manifest["theme_color"].lower() == bg
    for page in ("index.html", "login.html"):
        meta = re.search(r'<meta name="theme-color" content="(#[0-9A-Fa-f]{6})">', (STATIC / page).read_text())
        assert meta and meta.group(1).lower() == bg, page


UPSTREAM_NAMED_COLOURS = ("--color-subheader", "--color-blind-orange", "--color-link-hover",
                          "--color-agent-active", "--color-recording", "--color-error", "--color-success",
                          "--color-warning")


@pytest.mark.parametrize("look", ["sage", "duet", "hush"])
def test_each_look_remaps_upstreams_named_colours_and_has_a_palette(look):
    css = (STATIC / "openwings" / "looks" / f"{look}.css").read_text()
    tokens = (STATIC / "openwings" / "tokens.css").read_text() if look == "sage" else ""
    for var in UPSTREAM_NAMED_COLOURS:
        assert re.search(re.escape(var) + r":\s*var\(", css + tokens), (look, var)
    for name in ("--ow-palette", "--ow-tints"):
        lists = re.findall(re.escape(name) + r":\s*([^;]+);", css)
        assert lists, (look, name)
        for value in lists:
            colours = value.split()
            assert len(colours) == 8 and all(re.fullmatch(r"#[0-9A-Fa-f]{6}", c) for c in colours), (look, name, value)
    # OpenWings' own pages (Live browser, Workspace files, web search, sign-in
    # result, not found) take the look's inks, day and night.
    assert "body.ow-page" in css and "--muted:" in css and "--on-accent:" in css, look


def test_theme_swatches_are_a_keyboard_radio_group():
    js = THEME_JS.read_text()
    assert "role', 'radiogroup'" in js and "'aria-checked'" in js and "ArrowRight" in js
    assert ".theme-swatch:focus-visible" in SKINS.read_text()
