# Verification record

What was tested before this revision was published, how, and what still needs
your own server to confirm. Dates are UTC, 2026-11-10.

## Test environment, and its one big limitation

The build environment could reach Docker Hub and PyPI but not GitHub's
container registry blobs or Debian package mirrors. So the official image
`ghcr.io/odysseus-dev/odysseus:1.0.3-9d0d372` could be neither pulled nor
rebuilt there. Every runtime test ran on a stand in image built from the same
upstream commit (`9d0d372`): the same Python 3.14.9 base, the same
`requirements.txt`, the same source tree and entrypoint, but without the apt
packages (Chromium, Node, build tools, gosu). The Wings overlay was then built
on top of it through the real `docker/odysseus/Dockerfile`, with
`ODYSSEUS_BASE_IMAGE` overridden. Consequences:

* Anything that depends on those apt packages (Cookbook engine builds, the
  bundled npx browser server, agent-browser) was not exercised.
* The checksum guard was exercised against the stand in image, which carries
  byte identical source files; on the real image it must pass the same way.

## Upstream regression tests

The upstream test suite was run inside the built images over the 170 test
files that touch any patched module (4,998 tests), three times, after the
review fixes:

| Image | Email gate | Result |
| --- | --- | --- |
| Unpatched upstream | n/a | 26 failed, 4742 passed |
| Wings | `WINGS_FORCE_EMAIL_CONFIRM=0` | identical failure set: no regressions |
| Wings | `WINGS_FORCE_EMAIL_CONFIRM=1` | the same 26 plus 30 more, all of them tests that assert an email is sent, scheduled, connection-tested, or staged for an agent without a person's session: intended |

The 26 baseline failures are environmental (no IPv6, README files excluded
from the image, no network for some fixtures).

## Wings unit tests

`./tests/unit/run.sh`: 44 tests for the email gate (human vs agent loopback vs
API token vs auth disabled; self address allowlist; the SMTP chokepoint),
environment scrubbing, per agent tool allowlists (including MCP tools and fail
closed handling), the encryption key patch (env key used, no file written,
malformed or mismatched key refused, upstream behaviour without the variable)
and the preflight (exit 78 cases; stored-ciphertext key check; stray key
quarantine; the WINGS_ACCEPT_KEY_LOSS override; one-time approval of
schedules migrated from upstream; imports nothing from the app).

## Adversarial review

An independent review (four reviewers, each finding then checked by a
separate skeptic told to refute it) ran over the patches, scripts and docs.
Every confirmed issue was fixed in the commit "Fix issues found by
adversarial review": Cookbook and terminal launches on Linux inherited
secrets; the key marker was itself scrubbed; adopt-key could lose webhook
secrets or the key; the email config route let an agent repoint mail
servers; group chats ignored agent tool limits; restore left files root
owned; Caddy needed an ACME email; restoring with a different key had no
safe path. Ten of the skeptic checks did not run (usage limit); their
findings were assessed and fixed directly.

## End to end, on a fresh install made by `scripts/setup.sh`

`./tests/e2e/run.sh` against a stack created with
`./scripts/setup.sh --with-ollama --with-caldav`, using a deterministic mock
model and a GreenMail SMTP/IMAP server on the internal network. First run on
the fresh stack: 0 failures. Checks:

* login; register an OpenAI compatible endpoint; the API never returns its key
* streamed chat; switch model mid conversation; history keeps both turns
* session headers encrypted at rest; no key file in the data volume
* agent limited to Calendar is offered `manage_calendar` but no web, shell or
  file tools; the same agent unrestricted is offered calendar and web
  (A/B control); re-posting the agent without the field keeps the limit
* a person's Send is delivered (with `EMAIL_ALLOW_PRIVATE_IPS=true` for the
  local test mail server); an API token gets 403; the agent's email tool
  creates a draft document and its process cannot open SMTP; nothing an agent
  wrote reaches the recipient; a scheduled row no person approved is refused
  by the poller and not delivered
* a user added MCP server does not receive `ODYSSEUS_APP_KEY` or the admin
  password but does receive its own configured env; its env is encrypted at
  rest
* CalDAV with the bundled Radicale: a phone style client creates a calendar
  and an event; Odysseus pulls it; an event created in Odysseus is written
  back; the CalDAV password is not stored in plaintext
* no plaintext endpoint key, mail password, MCP env value or CalDAV password
  anywhere under `/app/data` or `/app/logs`

Two checks depend on upstream's per turn tool routing (it offers only the
tool families a message's intent selects) and report SKIP when the router
does not offer the tool: the agent's loopback `app_api` call to the send route
(covered deterministically by the unit tests) and sending through a chat turn
(covered by calling the agent's email tool directly).

Known test environment artefact: repeated e2e runs against the same long
running stack in this sandbox showed sporadic TCP connect timeouts between
containers (the app's request never reached the mock server; raw sockets and
plain httpx from the same container succeeded). The first run on every fresh
stack passed. The harness retries one model call after the app's 20 second
host cooldown. If you see `Model request failed` in an e2e run on your server,
rerun before investigating.

## Operations

* `setup.sh`: fresh `.env`, secrets generated, mode 600, CalDAV user bcrypt
  hashed through stdin; second run is idempotent (key unchanged).
* Backup → delete a note → restore → the note is back, stack healthy.
* A data volume holding an old key file makes the container stop with exit
  code 78 and an explicit message; `scripts/adopt-key.sh --yes` moves the key
  into `.env`, removes the key files, and a credential stored under the old
  key still decrypts afterwards.
* SearXNG crash loop on hosts without IPv6 reproduced and fixed
  (`GRANIAN_HOST`).
* Radicale under dropped capabilities: the image's default recursive chown
  failed; fixed with `TAKE_FILE_OWNERSHIP=false`.
* Agent loopback port bug reproduced: inside the container port 7011 refuses,
  7000 answers; fixed with `ODYSSEUS_INTERNAL_BASE`.

## UI (Chromium, Playwright)

Sidebar shows **Agents** (keyboard focusable) and **Files & Docs**; the agent
editor's Tool access section renders 15 families; ticking two and pressing
Start Persona stores exactly those on the active agent and its saved template,
and reopening the editor restores them; with no saved theme the app opens
light under a light OS setting and dark under a dark one; no page errors.

## Not verified: please check on your server

1. Build on the real upstream image: `./scripts/setup.sh`, then
   `./tests/unit/run.sh`.
2. Real hosted models (OpenAI, Anthropic, OpenRouter) and a real local model
   (Ollama could not download models here).
3. Deep Research quality and citation links with a real model and live web.
4. Gmail with an app password; Google OAuth; Outlook through DavMail or
   email-oauth2-proxy.
5. iCloud, Fastmail or Nextcloud CalDAV.
6. HTTPS through Caddy and Let's Encrypt (needs a public DNS name).
7. NVIDIA GPU passthrough for Ollama.
8. The FastEmbed embedding model download on first start (Hugging Face was
   unreachable here, so file search and memory ran on keyword fallback).
