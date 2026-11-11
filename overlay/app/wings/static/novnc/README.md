# noVNC client (vendored)

`core/` and `vendor/` are copied unchanged from noVNC 1.6.0 as shipped in
`selenium/standalone-chromium@sha256:b211549a4c35f11826bc1a7559b9f23ff055198517f8cb7d49ef74df01c5e38d`
(`/opt/bin/noVNC`). The OpenWings live view (`../live.js`) imports `core/rfb.js`.

They are served by the OpenWings app from its own image, not fetched from the
browser container, so a compromised browser container cannot place script on
the OpenWings origin.

License: noVNC core is MPL-2.0 (`LICENSE.txt`, `AUTHORS`); pako is MIT
(`vendor/pako/LICENSE`).
