"""Consistency checks for the OpenWings looks (patch 0020): every ow-* theme in
static/js/theme.js has its look stylesheet, a display name and matching first
paint colours in skins.css, and the default looks the boot scripts name exist.
Run with ./tests/unit/run.sh (inside the built image, network off)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path("/app/static")
THEME_JS = STATIC / "js" / "theme.js"
SKINS = STATIC / "openwings" / "skins.css"
KEYS = ("bg", "fg", "panel", "border", "red")


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
    assert default in themes and default + "-night" in themes
    for page in ("index.html", "login.html"):
        pair = re.search(r"\? '(ow-[a-z-]+)' : '(ow-[a-z-]+)'\)", (STATIC / page).read_text())
        assert pair and pair.groups() == (default, default + "-night"), page


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
