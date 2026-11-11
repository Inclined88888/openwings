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
packages (Chromium, Node, build tools, gosu). The OpenWings overlay was then built
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
| OpenWings | `WINGS_FORCE_EMAIL_CONFIRM=0` | identical failure set: no regressions |
| OpenWings | `WINGS_FORCE_EMAIL_CONFIRM=1` | the same 26 plus 30 more, all of them tests that assert an email is sent, scheduled, connection-tested, or staged for an agent without a person's session: intended |

The 26 baseline failures are environmental (no IPv6, README files excluded
from the image, no network for some fixtures).

**Full suite, 2026-11-11** (706 test files, 11,262 tests, both images, network
off): unpatched upstream 65 failed; OpenWings 68 failed. The first run had 10
failures only on OpenWings; 7 were real and are fixed (see "Second review pass"
below). The 3 left are intended: patch 0007 makes drafted skills wait for a
person instead of auto publishing, and upstream's tests assert the auto
publish default (`test_skill_audit_utility.py::test_audit_persists_saved_turn_estimate_from_baseline_comparison`,
`test_skill_index_toolset_gating.py::test_inconclusive_audit_hides_an_unapproved_skill`,
`test_skill_index_toolset_gating.py::test_unaudited_or_below_threshold_published_skill_is_not_injected`).
With that default off, a skill reaches prompts only once it is published,
which the agent can do only with the person's request or approval. Every
other failure is identical on both images (no bubblewrap, ffmpeg, node,
tmux or README in the image; no network).

## OpenWings unit tests

`./tests/unit/run.sh`: 171 tests (44 at first release) for the email gate (human vs agent loopback vs
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

## Browser automation (patch 0008, profile `browser`)

The browser container was built from the pinned Selenium Chromium image
(Chromium 156.0.8078.79) and run with all capabilities dropped and
`no-new-privileges`. Checked:

* **Isolation**: from a container named `odysseus` the guard admits DevTools
  and the noVNC bridge; from any other container on the same network, and
  from the Docker host through the bridge address, the connection is reset.
  Chromium's DevTools, VNC and noVNC listen on loopback only.
* **Driver, directly against the container** with a local test site
  (`tests/e2e/site/`): numbered outline with headings, text and controls;
  fill, dropdown, checkbox, textarea, submit; elements inside an iframe;
  a `target=_blank` link opens a tab the chat follows; tabs and switching;
  a `confirm()` dialog is reported and answered (the click returns at once
  instead of hanging); `find`, `read` with parts, `scroll`, `screenshot`;
  unknown refs are refused; password and card fields refused;
  "Place order" refused by click and by Enter in its form; `file:` URLs
  refused; `evaluate` refused. Downloads: see the end to end list.
* **Through the chat API** (`tests/e2e/`, `WINGS_E2E_ONLY=browser`, mock
  model, web switch off as in the UI), last run after the security fixes:
  19 PASS (login, endpoint registration and key never returned, live view
  page and noVNC client served only with a
  session, WebSocket streams the screen and refuses a foreign Origin or no
  session, web task wording offered the browser, `file:` refused, page
  JavaScript refused, open, fill, dropdown, checkbox, card number handed to
  the person, order not placed), 3 SKIP (turns whose model call never
  reached the mock: password handoff, submit, "Place order"), 1 FAIL (the
  download click, because the preceding "open" turn never reached the model
  and the page was still the checkout), then the run hit its 28 minute cap.
  An earlier chat API run, before the security fixes, completed open, fill
  and submit through to the thanks page. Everything skipped there was
  verified directly against the backend (next bullet).
* **Driver, directly against the container, after the security fixes**: the
  functional script (form, dropdown, checkbox, submit, iframe, pop up tab,
  dialog, screenshot, refusals) behaved as before, and 20 of 20 hostile page
  checks passed: password field by single key and by typing; paste; a button
  named "Place order" only through aria-labelledby; the icon inside a "Buy
  now" button; Space on "Place your order"; Control+Enter in a form whose
  button is "Send"; a card field named through aria-labelledby; a card
  field focused inside an iframe, by typing and by a single key; forged
  page state markers; page supplied refs; download reported, read, and a
  path escape refused; a second account neither sees nor selects the first
  account's tab.
* **The model connection fault**: during these runs, new connections from
  the app's model client to the mock sometimes never reached it (the mock
  logged no connection), even with the mock inside the app container's own
  network namespace, while separate clients in the same container always
  connected. It was not traced to its cause; it is outside the browser code
  (it hits plain chat turns too). Rerun if you see "Model request failed"
  in e2e on your server.
* **Routing**: 13 realistic requests sent with the web switch off. Before the
  fix in `_looks_like_explicit_browser_interaction`, 5 web tasks, including
  "Use the browser to check the opening hours on the council website", were
  refused with "Web access is disabled for this turn". After it, every web
  task tried was offered `private_browser` ("Log into my supplier portal…",
  "Go to example.com…", "Book a table … at bistro.com", "Fill in the contact
  form on their website…", "Add the blue mug to my cart…", "Open my Stripe
  dashboard…", "Apply for the job on seek.com.au…", "Check my order status on
  amazon.com", "Renew my car registration online"); "What is the capital of
  France?" was not. "Sign in to LinkedIn…" was missed by the first version of
  the rule, fixed, and is covered by the unit tests; it was not run again
  through the chat API.
* **Live view in Chromium (Playwright)**: after signing in, the sidebar shows
  **Live browser**; `/wings/browser/` connects (status "Live") and shows the
  remote Chromium under the app's normal Content Security Policy.
* **Unit tests**: `tests/unit/test_wings_browser.py` (URL policy including DNS
  answers on private addresses, risky labels, sensitive fields, keys, users,
  outline format and retirement, routing wording, the enable switch, the
  authority grant).

* **Independent review and fixes**: a separate reviewer read the browser code
  with a hostile web page and a hostile container in mind. Confirmed issues,
  all fixed before release: the VNC bridge was reachable from pages on
  loopback (moved to unix sockets, guard now also needs a shared token);
  the noVNC client was served from the browser container onto the OpenWings
  origin (now vendored in the app image); downloads followed symlinks (now
  refused) and HTML downloads were stripped with a quadratic regex on the
  event loop (now a linear parser in a thread); private addresses were
  checked only for top level navigations (now every request goes through a
  filtering proxy, and every read path rechecks the URL); passwords and card
  numbers could be typed key by key, by paste or inside iframes, and
  pay/order/send could be triggered by Space, Control+Enter, child elements,
  aria-labelledby labels or dialogs (all now checked against the element
  that really has focus, across frames); a page could pre-stamp element refs
  (refs now use a random attribute per tab and cannot move between frames);
  tabs were not scoped per account; API tokens could drive the browser (now
  refused, as for email). Verified after the fixes: the guard returns 403
  without the token or with a wrong one and drops connections from other
  containers; the egress proxy refuses private, loopback, link-local
  (metadata), IPv4-mapped IPv6 and CONNECT targets; with the browser
  container set to refuse private addresses, Chromium's own request to the
  private test site was answered by the proxy's refusal even though the
  app-side check had been relaxed.

Not verified here (no internet in the test environment): any public website,
bot detection and CAPTCHAs, real logins, downloads, and the agent's decisions
with a real model. The mock model only replays the tool calls a test names.

## Patches 0009 to 0017 (2026-11-11)

All checks below ran against the same stand in image, with the mock model,
the code runner and both browser containers running.

* **Code runner (0009)**, through the real chat API with one approval:
  pandas and matplotlib ran, the total printed was right (358), the chart was
  shown in the chat, an `.xlsx` downloaded and opened with openpyxl. No
  secret looking variable was visible to the code, `/app/data` was not
  visible, and outbound network failed. A path escape and an unsigned
  request to the download route were refused. `tests/e2e/wings_e2e.py`
  `runner_checks`: 14 checks pass.
* **Upstream bug found on the way**: `RequestAuthority.intersect` marked a
  request's own authority as inherited, so `require_process_admission`
  refused every approved code run ("Producer operation has no request
  admission or exact claim"). Found by instrumenting the check; fixed in
  0009.
* **File transfer (0010)**: an upload by `fill` with `path=` was classified
  `external_side_effect` and `read_workspace` (so the approval gate asks), and
  the page saw the file's content; paths outside the workspace and the app
  database were refused; a download appeared in both the app's and the
  runner's workspace.
* **Pointer actions (0011)**: a canvas click landed at the computed point
  ("Canvas clicked at 25,41"); a drag from a draggable item onto a drop zone
  named by its visible text fired the page's HTML5 drop handler (the first
  version did not: Playwright needs a small first move to start an HTML5
  drag, now done); a pointer click on "Pay now" was handed to the person; a
  point outside the page was refused. `read` returned the page's table as a
  pipe table and its heading as `#`. Screenshots carry ref labels, removed
  before the next capture.
* **Plan continuation (0012)**, `tests/e2e/ui/plan_continuation.js` in
  Chromium with the chat stream answered by the script: Continue resends the
  approved plan with its ticks; the step limit continues on its own exactly
  3 times and then shows the normal "not finished" label; Stop here cancels
  it; an unrelated message, a finished plan and another chat's plan are
  never sent. PASS.
* **Token and spend caps (0013)**: with `WINGS_RUN_MAX_TOKENS=100` an agent
  turn stopped with "Run token limit reached (859 of 100 tokens)" before its
  tool ran; with `WINGS_DAY_MAX_TOKENS=1500` a turn stopped midway and the
  next message got HTTP 429 with the reason; the ledger recorded 1,718
  tokens for the day.
* **`#!bg` commands (0014)**: a marked command ran in the runner after
  approval, wrote its file and returned "Ran in the foreground in the code
  runner" (upstream returned "containment unavailable").
* **Renderer (0015)**: a page whose text exists only after its script runs
  came back with its table intact through the renderer, through
  `web_fetch` (with the note that it was read in a browser) and through the
  research `browser_read` fallback (which upstream answers with
  `page_unavailable`); a 404 and a `file:` URL were refused; no browser
  context was left open. Through the chat API the test site's private
  address is refused by upstream's fetcher before the renderer is reached,
  so `web_fetch` was driven directly with the fetcher stubbed to return an
  app shell.
* **Retries (0016)**: the mock model's `mock-ratelimited` answered 429 twice
  with "try again in 0.4s"; the app logged two retries with about 0.4 s
  waits and the reply arrived after 2.9 s.
* **MCP server notes (0017)**: a FastMCP stdio server with instructions,
  connected through upstream's MCP manager, had its notes (zero width
  character removed) shown above its tool in the prompt catalog.

## Second review pass (patches 0001 to 0017, 2026-11-11)

Three independent reviewers read patches 0011 to 0017 and the new OpenWings
modules; every serious finding went to a separate skeptic told to refute
it. 16 were confirmed, 1 was rejected (already fixed in the tree under
review), and all 16 plus the low ones are fixed. The full upstream suite
found 7 more. Fixed, each where the defect was introduced:

* **Approvals (0009)**: the code runner fix had stopped marking child runs
  as inherited, so a child's exact approval could restore a record, an MCP
  backend or a folder its parent excluded (4 upstream security tests
  failed). intersect() is upstream's again; only tool_execution's per call
  rebinding of the running request's own authority keeps its flag.
* **Documentation tests (0001, 0008)**: ODYSSEUS_APP_KEY is in upstream's
  environment reference; upstream's isolated distribution check copies the
  wings package.
* **Spend caps (0013)**: they missed the compact agent runtime, the
  non-streaming `/api/chat`, deep research, model calling tools, the
  summaries after an agent loop and providers' reported cost. Counting
  moved to where calls reach the provider (llm_core and the compact
  runtime), research checks the day cap, and runs share totals across
  their child runs.
* **Plans (0012)**: text the agent wrote through update_plan was sent back
  as the person's approved plan; plans never expired, were kept for one
  chat only, and the automatic continue count reset on reload.
* **Code runner (0014)**: the `#!bg` marker was split on carriage returns
  and Unicode separators, which could run text bash and the person saw as
  a comment. It is split on newlines only, as upstream and bash do.
* **Renderer (0015)**: no overall time limit (a page that never yields held
  a slot forever), it followed the browser's private address switch, it
  returned bot check pages as content, it resolved DNS on the event loop,
  and upgrades never created its token.
* **Browser (0011)**: `read` returned values typed into fields (card
  numbers, codes, revealed passwords); pointer clicks did not see into
  shadow DOM or embedded objects and checked before hovering; a drag could
  drop text into a code or card field and could start at a point the page
  had scrolled away from.
* **Retries (0016)**: hints in minutes, hours or days were read as seconds,
  long waits were retried anyway, upstream's own connection errors were
  backed off, and the wait held the failed stream and local model slot.
* **MCP notes (0017)**: shown even when none of that server's tools were
  offered.

Checked after the fixes, on the rebuilt image:

* Unit tests: 171 pass. Full upstream suite: as above.
* Spend caps through the real API with the mock model: a plain chat turn,
  an agent turn and `POST /api/chat` each add to the ledger (the last was
  never counted before); with `WINGS_RUN_MAX_TOKENS=100` an agent turn
  stopped before its tool ran; with the day used up, chat and `/api/chat`
  got 429 and `/api/research/start` did not start a job. A compact runtime
  turn (mock model set to compact tool mode) was counted. Its per round
  stop could not be driven by the mock (the compact preview refuses file
  tools without a sealed workspace, so no second round happens); the
  check it uses (`current_stop` under the run's request, through the real
  authority binding) returned the run limit message in process.
* Plans: `tests/e2e/ui/plan_continuation.js` PASS, now also checking that
  the automatic count survives a reload, ticks are copied, a plan the agent
  changed is dropped, and a plan older than 12 hours is not sent.
* Renderer, live: a JavaScript page read with its table; a "Just a
  moment..." page reported as a bot check; a page that never yields
  returned an error after 30.7 s and the next read worked; with the test
  switch off, the private test site was refused before any request.
* Browser page scripts in Chromium: `read` on a checkout page with a card
  number, a code, a revealed password and a typed note returned none of
  them; a point on a web component's inner "Place order" button was seen
  as that button; a closed component was flagged; an embedded object was
  refused.
* Retries: the rate limited mock answered after two 0.4 s waits (1.0 s in
  all, 2.9 s before the wait moved after the stream closed).
* Code runner: `runner_checks` 14 of 14 pass, including approved runs.

## Third review pass (patch 0018 and the reworked caps, 2026-11-11)

Two reviewers read the batch and hover actions and the reworked spend caps;
skeptics checked each serious finding (two checks did not run: the
reviewing agents hit their usage limit; those two findings were assessed
and fixed directly). Fixed:

* **Pointer guard (high)**: looking inside shadow DOM had lost the label of
  the button around an icon, so a click on the icon inside a labelled
  "Delete" button, or a labelled icon component, passed the guard, and a
  link's address was missed. The point check now walks the whole composed
  path (nearest button or link across shadow boundaries, labels of the
  shadow hosts) and also asks Chromium's own hit test, which sees inside
  closed shadow roots. Checked in Chromium on all five cases: an icon in a
  labelled Delete button, a labelled icon component and an icon in a link
  to a private address are all recognised, a closed shadow root's "Place
  order" button is read, and a canvas still has no label.
* **Drag**: a drag starting at a ref inside an iframe was refused; the
  start is now checked in its own frame. What is under the release point is
  checked after the glide (boards that show a Delete zone only while
  dragging); if it commits something the item is dropped back.
* **Batch**: any browser error now stops the batch with its log of what was
  done; a Next control that is covered, blocked or opens a dialog stops it
  with an error instead of "no more pages"; page checks have time limits
  and the outer limit leaves room for the step in flight; a batch whose
  reply would be too long stops and says where; downloads made by a step
  are listed, and each step waits only for its own downloads; upstream's
  checks on the addresses a batch opens now see its steps.
* **Spend caps**: a teacher run that takes over continues the request's
  totals (it started from zero); the chat route charges calls without a
  request authority (research mode, titles) to the caller; `/api/rewrite`
  and `/api/v1/chat` check the day cap and charge the caller; the cached
  owner of a chat expires after a minute.
* **Plans**: an automatic continue yields to anything the person sends or
  is typing, never continues a run the loop breaker stopped, ticks are kept
  while the chat runs in the background, and Execute under a plan approves
  that plan, not the last one stored for any chat.

Not changed: a deep research job the agent starts counts toward the
person's day but not toward the run (it is a separate request); the day cap
bounds it.

Unit tests: 181 pass.

Live, on the rebuilt image, `tests/e2e/browser_actions.py` (browser actions
called directly in the app container against the browser container and the
test site): 13 of 13 pass. A batch read all three pages of a paged table in
one call, repeated open, click and read for two items, stopped with PAUSED
at "Place order" (the order was not placed) and refused page JavaScript;
hover opened a menu shown only under the pointer; pointer clicks on an icon
inside a labelled Delete button, on a labelled icon component and on a
"Place order" button inside a closed shadow root were all handed to the
person (nothing was deleted or ordered); a canvas click registered at the
point asked for (within the 1 pixel the outline rounds to); drag onto a drop
zone and the Pay now handoff still work. Through the chat, "batch reads
every page" passed earlier; the chat driven browser suite could not be
completed in this sandbox, where upstream's tool index retries an embedding
model download every 30 seconds without internet access (490 attempts in an
hour), which made about one model turn in five fail and some turns offer no
browser tool. That upstream behaviour affects only installs that cannot
reach HuggingFace.

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
and reopening the editor restores them; no page errors. (Theme defaults are
now covered under "OpenWings looks" below.)

## OpenWings looks (patches 0019 and 0020, 2026-11-11)

Two looks ship, each with a night version: **Duet** (the default) and
**Hush**. Three were designed, built in the running app and judged side by
side by a separate art director pass; the third (Fieldbook) was dropped because
it read as an existing product. Both kept looks then had a polish round
against the art director's must fix list.

Checked in the rebuilt image, in Chromium:

* **First run** (fresh browser, nothing saved, no server theme): the login
  page and the app open in Duet under a light OS setting and Duet night under
  a dark one, with the look's colours, not upstream's dark defaults.
* **Theme picker**: picking Hush applies the Hush look; picking an upstream
  theme (midnight) removes the look; Reset to Default returns to Duet from
  either, look and colours together.
* **Customize tab**: while a look is active, colour, colour harmony and font
  controls are locked behind a note, so one colour edit cannot silently drop
  the look; with an upstream theme they work as before.
* **Wording**: the welcome line reads "Your private workspace. Ready when you
  are."; upstream's "Nobody" mode is labelled Incognito with a plain tooltip.
* **Send button**: an empty box keeps a quiet send arrow; Enter or a click on
  an empty box does nothing (upstream opened a new chat); typing switches to
  send; the accessible name follows the button's action.
* **Incognito and Tidy**: an Incognito chat answered, survived the sidebar
  Tidy (`/api/sessions/auto-sort?skip_llm=true`, 200), and answered again in
  the same session.
* **Contrast**: every visible text element on the welcome screen and the
  sample chat was measured against its real background in all four themes
  (31 and 66 to 69 elements per view). All meet WCAG AA except one reading
  the scanner cannot see correctly: Duet's active Chat pill, whose background
  is a separate sliding element. Measured from pixels it is 7.73:1 (Duet) and
  8.63:1 (Duet night).
* **Fonts**: all self hosted (Atkinson Hyperlegible Next and Mono, Bricolage
  Grotesque, Figtree, Readex Pro, Red Hat Mono), each with its OFL licence;
  nothing is fetched from another site. `tests/unit/test_wings_looks.py`
  checks this, and that the colour table, first paint colours, look files,
  display names and boot defaults agree.

An independent review of the patch's JavaScript found 8 issues; 6 were
confirmed and all 8 are fixed: Reset to Default left the old look, `/theme
save` fell back to upstream dark, the assistant's theme tool did not know the
looks, a colour edit dropped the look, Enter on an empty box still submitted,
the send button kept the accessible name "New chat", and renaming the stored
Incognito session would have let upstream's Tidy delete a chat in use (the
stored name stays `Nobody`; only the labels changed).

Upstream tests over the 61 test files touching the changed code: unpatched
14 failed, OpenWings 15 failed, the same set plus
`test_clean_agent_preview.py::test_v3_browser_schema_does_not_offer_batch_or_current_tab_authority`.
That one is intended and comes from patch 0018: upstream asserts the browser
tool has no `batch` action, and OpenWings adds it.

## One command installer (`install.sh`, 2026-11-11)

Run here under bash 3.2.57 (the version macOS ships; every script it calls
ran under 3.2 too) and bash 5, shellcheck clean:

* `curl ... | bash` from GitHub: downloads the latest commit and runs setup.
* Fresh install with git; rerun updates and the branch tracks GitHub; a
  checkout made with `git clone -b` updates the same way.
* No working git (as on a Mac without Apple's command line tools): installs
  from the GitHub archive; a rerun removes files deleted upstream and keeps
  `.env` (same key) and `config/`.
* Refuses a folder holding something else, another project's git checkout,
  a file at the path, and local edits to OpenWings' own files; an empty
  folder is fine.
* Docker unreachable: tries to start it, waits, then stops with a clear
  message.
* Full start under bash 3.2: build, start, healthy, sign in details printed,
  login page served.
* Linux user outside the docker group: detected at once, added to the group,
  setup run inside the group with no stray argument, stack healthy. The user
  change and group switch were simulated with stand ins here (creating a real
  test user with sudo was not allowed in this environment).

An independent review found 7 confirmed defects (the docker group check could
never match, so such installs waited five minutes and failed; an empty
argument on the group path; WSL not starting an installed Docker Desktop;
archive updates leaving removed files; git updates not tracking GitHub) and
several likely ones (Docker's script offered on systems it does not support,
no Compose v2 check, rootless, snap and Docker Desktop for Linux not started,
the macOS developer tools pop up, other git folders accepted). All are fixed.

Not run here: a real Mac, WSL, and distributions other than Ubuntu.

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
