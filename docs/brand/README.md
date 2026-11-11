# OpenWings brand files

Two open black wings on a white background; their inner edges cradle an open
"O" (for OpenWings). Chosen from five candidates drawn for a softer, more
premium mark.

| File | Use |
| --- | --- |
| `openwings-mark.svg` | The logo on a white square (README, documents) |
| `openwings-tile.svg` | The logo on a white rounded tile (app icon, favicon, in app logo; shows on dark themes too) |
| `openwings-maskable.svg` | Android home screen icon (wings inside the safe circle) |
| `openwings-wings.svg` | The wings alone, transparent background |
| `png/openwings-social.png` | GitHub social preview, 1280 x 640 |
| `png/icon-192.png`, `png/icon-512.png`, `png/icon-maskable-512.png`, `png/favicon.ico`, `png/apple-touch-icon.png` | App icons; the image build installs the copies in `overlay/app/wings/brand/` |
| `png/openwings-mark.png` | The logo as a 512 x 512 PNG |

Everything is built from `openwings-mark.svg` (a 512 x 512 SVG: a white square
plus black shapes) by one command, which writes the other SVG files, renders
the PNG files with Chromium (wordmark in the Inter typeface), makes
`favicon.ico` and copies the app icons into `overlay/app/wings/brand/`:

    python3 scripts/dev/brand/build.py [new-mark.svg]

It needs node with the playwright package, and Pillow.

## GitHub

GitHub's own settings, which only the repository owner can change:

1. Settings, General, Repository name: `openwings`. GitHub redirects the old
   address, so existing clones keep working; update them with
   `git remote set-url origin https://github.com/<owner>/openwings.git`.
2. Settings, General, Social preview: upload `png/openwings-social.png`.
3. The About box on the repository page (gear icon): description
   "Your self hosted AI workspace", and optionally the website.
