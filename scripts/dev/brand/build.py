#!/usr/bin/env python3
"""Build every OpenWings brand file from one mark.

  python3 scripts/dev/brand/build.py [mark.svg]

The mark is a 512 x 512 SVG: a white background square plus black shapes.
Writes docs/brand/*.svg, renders docs/brand/png/* with Chromium (node and the
playwright package), makes favicon.ico (Pillow) and copies the app icons to
overlay/app/wings/brand/. Default mark: docs/brand/openwings-mark.svg."""
import re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BRAND = ROOT / "docs" / "brand"
src = Path(sys.argv[1]) if len(sys.argv) > 1 else BRAND / "openwings-mark.svg"
svg = src.read_text()
body = re.sub(r"^.*?<svg[^>]*>", "", svg, flags=re.S).rsplit("</svg>", 1)[0]
body = re.sub(r"<title.*?</title>", "", body, flags=re.S)
# drop the full size white background, keep the black shapes
body = re.sub(r'<path fill="#ffffff" d="M0 0H512V512H0Z"/>', "", body)
body = re.sub(r'<rect width="512" height="512" fill="#ffffff"/>', "", body).strip()
HEAD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="OpenWings">'

def write(name, background, scale):
    group = f'<g transform="translate(256 256) scale({scale}) translate(-256 -256)">{body}</g>'
    (BRAND / name).write_text(f"{HEAD}{background}{group}</svg>\n")

if src.resolve() != (BRAND / "openwings-mark.svg").resolve():
    write("openwings-mark.svg", '<rect width="512" height="512" fill="#ffffff"/>', 1)
write("openwings-tile.svg", '<rect width="512" height="512" rx="112" fill="#ffffff"/>', 0.84)
write("openwings-maskable.svg", '<rect width="512" height="512" fill="#ffffff"/>', 0.7)
write("openwings-wings.svg", "", 1)
(BRAND / "png").mkdir(exist_ok=True)
subprocess.run(["node", str(Path(__file__).with_name("render.js")), str(BRAND), str(BRAND / "png")], check=True)
from PIL import Image
imgs = [Image.open(BRAND / "png" / f"favicon-{s}.png").convert("RGBA") for s in (16, 32, 48)]
imgs[2].save(BRAND / "png" / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)], append_images=imgs[:2])
dest = ROOT / "overlay" / "app" / "wings" / "brand"
for f in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "favicon.ico"):
    shutil.copy2(BRAND / "png" / f, dest / f)
shutil.copy2(BRAND / "openwings-tile.svg", dest / "openwings-tile.svg")
print("brand files written from", src)
