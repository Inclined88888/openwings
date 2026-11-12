# Notice

OpenWings is a modified version of Odysseus (https://github.com/odysseus-dev/odysseus),
copyright its contributors, licensed under the GNU Affero General Public License
v3.0 or later. OpenWings is distributed under the same license; the full text is
in [LICENSE](LICENSE). Modified by the OpenWings project, 2026: the changes are
the patches in `patches/`, each with its date and reason.

Files in this repository derived from the upstream program, and therefore under
AGPL-3.0-or-later:

* `patches/*.patch` (diffs against the upstream source)
* `overlay/app/**` (upstream files with those patches applied, plus OpenWings
  modules that run inside the app process)
* `config/searxng/settings.yml` and `scripts/vendor/migrate_searxng_settings.py`
  (vendored unchanged from upstream commit 9d0d3720f4ca181d87bef94da826d730acc6d483)

AGPL section 13: if you let other people use OpenWings, modified or not, over a
network, you must offer them its corresponding source. This repository is that
source for an OpenWings deployment (the sign-in page links to it).

Third party container images used by `docker-compose.yml` keep their own
licenses: ChromaDB (Apache-2.0), SearXNG (AGPL-3.0), ntfy (Apache-2.0 /
GPL-2.0), Ollama (MIT), Radicale (GPL-3.0) via tomsquest/docker-radicale,
Caddy (Apache-2.0). The optional browser container is built on
selenium/standalone-chromium (Apache-2.0; Chromium BSD-3-Clause and others,
noVNC MPL-2.0, x11vnc GPL-2.0). The OpenWings image adds Playwright for Python
(Apache-2.0), greenlet (MIT/PSF), pyee (MIT) and websockets (BSD-3-Clause).
The e2e tests start GreenMail (Apache-2.0).
