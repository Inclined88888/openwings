# OpenWings brand files

Two open black wings on a white background.

| File | Use |
| --- | --- |
| `openwings-mark.svg` | The logo on a white square (README, documents) |
| `openwings-tile.svg` | The logo on a white rounded tile (app icon, favicon, in app logo; shows on dark themes too) |
| `openwings-maskable.svg` | Android home screen icon (wings inside the safe circle) |
| `openwings-wings.svg` | The wings alone, transparent background |
| `png/openwings-social.png` | GitHub social preview, 1280 x 640 |
| `png/icon-192.png`, `png/icon-512.png`, `png/icon-maskable-512.png`, `png/favicon.ico`, `png/apple-touch-icon.png` | App icons; the image build installs the copies in `overlay/app/wings/brand/` |
| `png/openwings-mark.png` | The logo as a 512 x 512 PNG |

The PNG files are rendered from the SVG files with Chromium (wordmark in the
Inter typeface). To change the logo, edit `openwings-mark.svg` (the other SVG
files reuse its feather and wing shapes), render the PNG files again, and copy
the app icons into `overlay/app/wings/brand/`.

## GitHub

GitHub's own settings, which only the repository owner can change:

1. Settings, General, Repository name: `openwings`. GitHub redirects the old
   address, so existing clones keep working; update them with
   `git remote set-url origin https://github.com/<owner>/openwings.git`.
2. Settings, General, Social preview: upload `png/openwings-social.png`.
3. The About box on the repository page (gear icon): description
   "Your self hosted AI workspace", and optionally the website.
