# Wings

A self hosted AI workspace for everyday knowledge work: chat, agents, deep research with cited sources, documents, files, notes, tasks, email, calendar and memory, in one web app on your own server.

Wings is a hardened deployment of [Odysseus](https://github.com/odysseus-dev/odysseus) (AGPL-3.0), pinned to an exact upstream build, plus a small, reviewable set of patches that close the gaps between upstream and the requirements below. Everything upstream does well is used as is; every Wings change lives in [`patches/`](patches) as a plain diff with its reason written at the top.

| Need | Where it lives |
| --- | --- |
| Chat with history, switch models mid conversation, streaming | Upstream |
| Agents with their own instructions **and tool access** | Upstream personas + Wings patch 0005 |
| Deep research with inline citations | Upstream |
| Documents: draft, rewrite, summarize, version history | Upstream |
| Files: upload, ask questions, searchable library | Upstream |
| Notes and tasks the AI can read and write | Upstream |
| Email with **a person's click required before any send** | Upstream + Wings patch 0004 |
| Calendar sync (CalDAV), schedule questions, proposed events | Upstream + optional bundled Radicale |
| Memory you can view, edit, delete | Upstream |
| Web search tool for chat and agents | Upstream + bundled SearXNG |
| **Browser automation**: the agent uses websites in a real browser; you watch live and take over | Upstream tool + Wings patches 0008, 0010, 0011 + optional browser container |
| Web fetch and deep research read pages that need JavaScript | Upstream + Wings patch 0015 + renderer container (with the browser) |
| **Code runner**: the agent runs Python and shell for data analysis, charts and office files, isolated from the app | Upstream tools + Wings patches 0009, 0014 + optional runner container |
| Approved plans followed through long tasks; hard token and spend limits | Upstream plans + Wings patches 0012, 0013 |
| Local and hosted models, configured in the UI | Upstream |
| Model comparison side by side | Upstream |
| Secrets encrypted at rest, key outside the data | Upstream + Wings patches 0001 to 0003 |
| One command start, named volumes, backup and restore | Wings |

## Contents

* [Install](#install)
* [First run](#first-run)
* [Models: local and hosted](#models)
* [Email](#email)
* [Calendar](#calendar)
* [Agents and tool access](#agents)
* [Browser automation](#browser)
* [Code runner and workspace files](#runner)
* [Long tasks: plans and limits](#long-tasks)
* [Where everything is in the app](#map)
* [Backup, restore, upgrade](#backup)
* [Security model](#security)
* [What Wings changes in upstream](#patches)
* [What was verified, and what you must verify](#verification)
* [Troubleshooting](#troubleshooting)

<a id="install"></a>
## Install

Requirements:

* Linux server, macOS or Windows with WSL2
* Docker Engine 24 or newer (Docker Desktop on macOS/Windows) with the Compose v2 plugin, version 2.24 or newer (`docker compose version`)
* RAM: the core stack measured about 0.6 GB idle in testing (before the embedding model loads); plan for 2 GB or more with hosted models. Local models need much more (see [Models](#models))
* Disk: several GB for images (the upstream image bundles Chromium and build tools), plus your data and any local models
* Outbound access to `ghcr.io` (the Odysseus image) and Docker Hub (sidecars)

```bash
git clone https://github.com/Inclined88888/wings.git
cd wings
./scripts/setup.sh
```

That is the whole install. `setup.sh`:

1. copies `.env.example` to `.env` (mode 600) and generates the encryption key, the admin password and the SearXNG secret;
2. builds the Wings image (the pinned upstream image plus the patch overlay) and starts the stack;
3. waits until the app is healthy and prints the URL, the user name and, on first run, the password.

Then open <http://127.0.0.1:7011>. After this first run, `docker compose up -d` starts everything.

Optional extras, all through the same script (safe to re-run; it never regenerates existing secrets):

| Flag | What it adds |
| --- | --- |
| `--with-ollama` | Bundled Ollama container for local models (CPU) |
| `--ollama-gpu nvidia` | GPU for the bundled Ollama (needs the NVIDIA Container Toolkit) |
| `--ollama-model qwen3:4b` | Pulls a model into the bundled Ollama after start |
| `--with-caldav` | Bundled Radicale CalDAV server: a private calendar your phone can sync |
| `--with-browser` | A real Chromium the agent drives for web tasks, with a live view you can take over (see [Browser automation](#browser)) |
| `--with-runner` | An isolated container where the agent runs Python and shell code (see [Code runner](#runner)) |
| `--domain ai.example.com --acme-email you@example.com` | Public HTTPS through Caddy with Let's Encrypt (the email is required) |
| `--bind 0.0.0.0 --port 7011` | Expose the UI on your LAN instead of only this machine |
| `--no-agents` | Skip adding the starter agents |

The stack:

| Service | Purpose | Reachable from the host |
| --- | --- | --- |
| `odysseus` | The app (Wings image) | `127.0.0.1:7011` |
| `searxng` | Private web search | internal only |
| `chromadb` | Vector index for files and memory | internal only |
| `ntfy` | Push notifications | `127.0.0.1:8091` |
| `ollama` (profile `ollama`) | Local models | internal only |
| `radicale` (profile `caldav`) | CalDAV calendars | `127.0.0.1:5232` |
| `caddy` (profile `https`) | HTTPS front door | `80`, `443` |
| `browser` (profile `browser`) | Chromium for the agent; live view through the app at `/wings/browser` | none (reachable only from `odysseus`) |
| `renderer` (profile `browser`) | Stateless Chromium that reads pages needing JavaScript for web fetch and deep research; no logins | none (reachable only from `odysseus`) |
| `runner` (profile `runner`) | Runs the agent's Python and shell code; no network at all | none (reachable only from `odysseus`) |

All state lives in named Docker volumes (`odysseus-data`, `chromadb-data`, `radicale-data`, `ollama-models` and others listed at the end of `docker-compose.yml`). `docker compose down`, upgrades and container rebuilds never touch them; only `docker compose down -v` deletes data.

<a id="first-run"></a>
## First run

1. Sign in with the user and password `setup.sh` printed (also in `.env` as `ODYSSEUS_ADMIN_PASSWORD`).
2. Add a model: **Settings → Add Models**. See [Models](#models). Typing `/setup` in the chat box starts upstream's guided setup instead.
3. **Settings → Account**: turn on two factor authentication, then change the password. The env password is used only to create the admin on the very first start; later changes made in the app stick.
4. Copy `.env` somewhere safe and offline (password manager or encrypted USB). It holds `ODYSSEUS_APP_KEY`, the only key that decrypts stored credentials. Backups deliberately do not contain it.
5. Optional: set `TZ` in `.env` (for example `TZ=Europe/London`) so reminders and scheduled tasks use your time zone, then `docker compose up -d`.

<a id="models"></a>
## Models: local and hosted

Everything is configured in the app: **Settings → Add Models**. Keys are stored encrypted. **Settings → Added Models** lists them; **Settings → AI Defaults** picks the default chat, utility and research models. Switch models at any time with the model button in the message box; the next reply uses the new model and the conversation history stays.

**Hosted providers** (card "Add API Models"): OpenAI, Anthropic (called through its native Messages API), OpenRouter, Groq, DeepSeek, Together, Gemini, xAI and any OpenAI compatible endpoint by URL. Choose the provider, paste the key, press Test, then Add.

> A consumer subscription (ChatGPT Plus, Claude Pro) is not an API key. You need a key from the provider's developer console, or an OpenRouter key, which reaches many providers with one balance.

**Local models** (card "Add Local Models"):

| Where Ollama runs | URL to add |
| --- | --- |
| Bundled container (`setup.sh --with-ollama`) | `http://ollama:11434/v1` |
| Natively on the Docker host (recommended on macOS: containers there get no Apple GPU) | `http://host.docker.internal:11434/v1` (start Ollama with `OLLAMA_HOST=0.0.0.0`) |
| Another machine on your LAN | `http://192.168.x.y:11434/v1` |

llama.cpp `llama-server`, vLLM, LM Studio and other OpenAI compatible servers work the same way: add their `/v1` base URL. The "Scan" button probes common ports on `LLM_HOST`/`LLM_HOSTS`.

Rough sizing for local chat models: a 4B model at 4 bit needs about 4 GB of free RAM or VRAM; an 8B model about 6 to 8 GB. On CPU only, expect a few tokens per second. For agents (tool calling), research and email drafting, a hosted model or a local model of 14B or more works far better than small ones.

**Rate limits**: when a hosted provider answers "rate limited" (429) or "overloaded" (503, 529) before a reply starts, Wings waits and retries up to 3 times (patch 0016), as long as the provider asks or 2, 4 and 8 seconds, never more than 20 seconds per wait; upstream showed an error instead. When the provider asks for longer (an hourly or daily quota), Wings does not retry at all. `WINGS_MODEL_RETRIES=0` turns this off. With more than one model set up for a chat, upstream's switch to the next model still applies.

**Compare models**: sidebar **Compare**. Sends one prompt to up to 8 models side by side, blind by default, with voting and a scoreboard.

**Embeddings** (file search and memory) use a built in FastEmbed model, downloaded once on first start. To use Ollama instead, set `EMBEDDING_URL=http://ollama:11434/v1/embeddings` and `EMBEDDING_MODEL=nomic-embed-text` in `.env` (pull that model first).

<a id="email"></a>
## Email

Add accounts in **Settings → Email** (provider presets for common services). The Email view (sidebar) reads, searches, summarizes and drafts replies with AI; agents can do the same through tools.

**Gmail** (simplest, no Google Cloud project):

1. Turn on 2 Step Verification on the Google account.
2. Create an app password at <https://myaccount.google.com/apppasswords>.
3. In Wings use the Gmail preset, or: IMAP `imap.gmail.com` port 993 SSL; SMTP `smtp.gmail.com` port 465 SSL; user = your full address; password = the app password.

Gmail through "Connect with Google" (OAuth) needs your own Google Cloud OAuth client; set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and `GOOGLE_OAUTH_REDIRECT_URI` in `.env` (the button appears under the "Google Workspace / .edu" preset). Note: upstream's agent email tools cannot use OAuth accounts, only password accounts.

**Outlook.com and Microsoft 365**: upstream has no Microsoft sign in, and Microsoft no longer accepts plain passwords for these mailboxes. Workaround: run a local OAuth proxy such as [DavMail](https://davmail.sourceforge.net/) or [email-oauth2-proxy](https://github.com/simonrob/email-oauth2-proxy) on the host, point the account at it, and set `EMAIL_ALLOW_PRIVATE_IPS=true`. Not tested in Wings.

**Proton Mail**: use Proton Mail Bridge on the host and set `EMAIL_ALLOW_PRIVATE_IPS=true`.

### No email leaves without your click

With `WINGS_FORCE_EMAIL_CONFIRM=1` (the default) the rule is enforced on the server:

* **You** send by clicking Send (or Ctrl+Enter) in an email draft, or by scheduling a send. Those work as normal.
* **An agent** that "sends" or "replies" creates a draft document instead and tells you; open it, review it, click Send. The agent's mail process cannot open SMTP at all.
* The agent's internal API access, API tokens and plugins get HTTP 403 on every send route, cannot approve their own drafts, and cannot change or test email account servers (which would leak the stored password).
* Scheduled sends go out only if a person created or approved them.
* Away auto replies are off.
* Task results and reminders can be emailed only to the addresses you list in `WINGS_EMAIL_SELF_ADDRESSES` in `.env`.

The switch lives only in `.env`; nothing inside the app, including an agent with admin tools, can turn it off.

<a id="calendar"></a>
## Calendar

Sidebar **Calendar** (Ctrl+Alt+C): week, month, year and agenda views, .ics import and export. CalDAV accounts are added in **Settings → Integrations** (Test, then Save). The AI answers schedule questions and creates events through the `manage_calendar` tool.

| Calendar | How |
| --- | --- |
| iCloud | URL `https://caldav.icloud.com`, Apple ID email, an app specific password from your Apple account page |
| Fastmail | URL `https://caldav.fastmail.com/dav/`, an app password |
| Nextcloud | URL `https://your.cloud/remote.php/dav` |
| Bundled Radicale (`setup.sh --with-caldav`) | In Wings: URL `http://radicale:5232/<user>/`, user and password from `.env` (`RADICALE_USER`, `RADICALE_PASSWORD`). On your phone: `http://<server-lan-ip>:5232/` after setting `RADICALE_BIND` to that IP, or `https://<domain>/dav/` with the HTTPS profile |
| Google Calendar | Not supported for sync: Google's CalDAV requires OAuth, which upstream does not implement. Use iCloud or Radicale as the shared calendar, or import .ics files |
| Microsoft 365 / Outlook.com | Not supported for sync (Microsoft has no CalDAV). Same options as Google |

Know before you rely on it (upstream behaviour, not changed by Wings): sync pulls when you open the calendar or press "Sync now" (there is no background polling); local changes are pushed immediately, last write wins; recurring series with one edited instance can collapse. `ODYSSEUS_ALLOW_PRIVATE_CALDAV=1` is needed for CalDAV servers on private addresses (`setup.sh --with-caldav` sets it).

<a id="agents"></a>
## Agents and tool access

Sidebar **Agents** opens the persona editor. An agent has a name, instructions (system prompt), sampling settings, its own continuity memory and, with Wings, its own **tool access**:

* **Limit this agent's tools** off: the agent can use every tool your account can.
* On: tick the tool families it may use (web, notes, tasks, calendar, email, documents, memory, research, files, images, shell, admin, your own MCP servers). Everything else is removed server side for chats with that agent, not just hidden in the prompt.

Press **Start Persona** to activate it; saved agents appear in the editor's drop down. Limits also apply to each agent taking part in a group chat. The agent can always ask you questions, update its plan and open panels in your UI. Use the message box's **Agent** switch for tool use; **Chat** mode answers without tools.

Five starter agents are added on first install (`./scripts/seed-agents.sh` re-adds any you delete; it never overwrites edits):

| Agent | Tool families | Behaviour |
| --- | --- | --- |
| Inbox Triage | email, notes, tasks, calendar, contacts | Groups new mail by what needs action, treats mail content as untrusted, drafts replies for you to send, offers tasks for deadlines |
| Researcher | web, research, documents, memory | Reads several sources, cites only pages it opened with numbered inline links, flags uncertainty |
| Writer | documents, memory, files | Drafts and edits in your voice, never invents facts, lists its changes |
| Planner | calendar, notes, tasks | Answers schedule questions, confirms before creating events, turns plans into tasks |
| Data Analyst | shell, files, documents | Analyses CSV, Excel and JSON with Python in the code runner (needs the runner), computes every number from the full file, charts with stated trends and outliers, writes cleaned data to new files with download links |

**Scheduled agents**: sidebar **Tasks** runs an agent prompt on a schedule, a cron expression or an event (for example a morning briefing). Results go to a chat, or by email to yourself only.

**Self improvement**: memory facts are extracted from conversations automatically (every few messages; toggle in the Memory panel), and the system drafts reusable "skills" from long agent runs. Wings changes one default: drafted skills wait in **Skills** for your approval instead of auto publishing into future prompts, because they can be learned from untrusted web or email text. Turn "Auto approve skills" on in the Memory panel if you prefer full autonomy.

**Browser and code**: browser automation works with `--with-browser` ([Browser automation](#browser)) and code execution with `--with-runner` ([Code runner](#runner)). Without the runner, upstream's `bash` and `python` tools refuse to run: they need a sandbox (bubblewrap) that Docker does not allow, and Wings does not loosen that; the runner gives them a separate locked down container instead. Web search, page reading, PDFs and YouTube transcripts work without either.

<a id="browser"></a>
## Browser automation

Turn it on once:

```bash
./scripts/setup.sh --with-browser     # or add "browser" to COMPOSE_PROFILES in .env, then docker compose up -d
```

This adds the `browser` container: a real, headful Chromium on a virtual screen. The agent drives it through upstream's `private_browser` tool, which Wings connects to that container (patch 0008). It works the way OpenManus and browser-use do: after every step the agent gets the page as a numbered outline of headings, text and controls, for example

```text
# Contact us
[e6] textbox "Full name" required
[e8] textbox "Password" type=password [password field: the person types this in the live view]
[e9] select "Country" selected="Choose…" options: Choose… | Australia | New Zealand
[e12] button "Continue"
```

and acts by number: open, read, find, click, fill (text, dropdowns, checkboxes), type, press keys, scroll, wait, tabs, back and forward, and screenshots for vision models. Because the outline is text, it works with local and hosted models alike. Each chat gets its own tab, and pop up windows are followed automatically.

* **Reading** returns headings, lists and tables as structured text (tables stay tables), so the agent can pull figures out of a page reliably. It never returns what was typed into a field (a card number, a code or a password you typed in the live view stays out of the chat); fields show only their label.
* **Canvas apps and drag and drop** (patch 0011): canvas areas and draggable items appear in the outline with their position, and the agent can click at a point (`x`, `y`) or drag from one element or point to another, including onto a drop zone named by its visible text. The pay and send guard below applies to pointer clicks and to both ends of a drag: the pointer first moves to the point and what is under it is checked after the page reacts (rows that reveal a Delete button on hover), web components are looked into, and a component whose label cannot be read is handed to you. A drop never types into a field, and both ends of a drag must be on screen at once.
* **Repetitive work in one step** (patch 0018): `batch` runs up to 20 browser actions in one call, optionally once per item of a list (open each invoice by name) or on each page of a paged table (follow "Next" up to 25 pages). Every step is checked exactly as if it were called alone, and the batch stops at the first step that needs you. Without it, a 24 statement download took a model round per click. `hover` opens menus that appear only under the pointer.
* **Pages that need JavaScript** (patch 0015): web fetch and deep research read them in the `renderer` container, a stateless copy of the browser with no logins, cookies or downloads and a fresh context per page, so research works on single page apps and script heavy news, shop and dashboard sites without exposing your signed in browser to them. Upstream's own research fallback for such pages no longer worked. The renderer never opens private or local addresses, whatever the browser is allowed, each read has a hard time limit, and a bot check page ("Just a moment...") is reported as one, never as the page.
* **Files** (patch 0010): downloads land in the agent workspace under `downloads/`, where the agent can read them, analyse them with the code runner, and you can save them from **Workspace files** or the live view's **Downloads**. The agent can also upload a workspace file into a web form (`fill` with `path=`); every upload asks for your approval first, because it sends your file to a website.

**Ask in plain words**, in Agent mode: "Log into the supplier portal and download last month's invoice", "Fill in the contact form on example.com with my details", "Check my order status on the shop site", "Go to the council website and find the bin collection days". Naming a site, a portal, a form or the browser is enough; the web search switch does not need to be on.

**Live view and take over**: sidebar **Live browser** (or `<your Wings address>/wings/browser`) shows the browser screen in your own browser and lets you click and type in it. It needs your Wings sign in; nothing about it is exposed on a separate port. Use it to:

* **sign in** to sites once: the agent never types passwords, one time codes or card numbers. It stops with *PAUSED, needs the person*, you sign in through the live view (the *Type or paste text* box sends text from your password manager), then tell the agent to continue. Logins persist in the `browser-profile` volume across restarts;
* **approve committing steps**: the agent does not click buttons such as Pay, Place order, Buy now, Send, Post, Publish, Submit application, Book now, Subscribe or Delete account, and does not press Enter in a form whose button is one of those. It stops and you click it yourself. This mirrors the email rule: nothing leaves on your behalf without your click;
* **solve CAPTCHAs** and anything else the agent gets stuck on, or simply watch (tick *Watch only* to avoid stray clicks).

**Guardrails** enforced by the browser backend itself (`overlay/app/wings/browser_policy.py`), regardless of what a page or a prompt says:

| Rule | Switch in `.env` |
| --- | --- |
| Password, one time code and card fields are typed only by you, also key by key, by paste or inside iframes | always on |
| Pay, order, send, publish, apply, delete style clicks are handed to you, also by Enter or Space and in confirmation dialogs | `WINGS_BROWSER_CONFIRM_RISKY=0` turns it off (not recommended) |
| Only `http`/`https`; no `file:`, `chrome:`, `data:`, `javascript:` pages | always on |
| No private or local addresses (your router, LAN devices, cloud metadata, the other Wings containers), for every request the browser makes: pages, redirects, iframes, scripts, form posts and WebSockets all pass through a filtering proxy inside the browser container | `WINGS_BROWSER_ALLOW_PRIVATE=1` allows them |
| The agent cannot run its own JavaScript in pages | `WINGS_BROWSER_ALLOW_EVAL=1` allows it |
| Optional allow list of sites | `WINGS_BROWSER_ALLOWED_DOMAINS=gov.au,example.com` |
| One browser profile holds one person's logins, so only the admin account may use it | `WINGS_BROWSER_USERS=alice,bob` (or `*`) |

Other settings: `WINGS_BROWSER_SCREEN=1440x900`, `WINGS_BROWSER_LANG=en-US` (websites also see your `TZ`), `WINGS_BROWSER_MEMORY=3g`, `WINGS_BROWSER_MAX_TABS=10`. New installs allow 40 agent steps per message (upstream: 20) because web tasks take many small steps; change it in **Settings → Agent**.

**Limits, honestly**:

* Sites with strong bot detection or CAPTCHAs on every action (some banks, airlines, ticketing, social networks) may block or slow automation. The browser is a normal headful Chromium with a persistent profile, which helps, but there is no stealth or CAPTCHA solving. Take over in the live view when that happens.
* Uploads come only from the agent workspace (you cannot upload from your own computer through the live view: it shows the server's browser). Downloads are not added to Wings **Files** (the searchable library) automatically; upload them there if you want them indexed.
* One browser and one action at a time: chats and scheduled agents share it, each in its own tab.
* Quality depends on the model. Small local models (under about 14B parameters) often lose track over long web tasks; a strong hosted model or a large local one does far better.
* Resource use: the browser container measured about 0.26 GB with a few simple test pages open. Real websites with heavy scripts take far more per tab, so the container is capped at 3 GB (`WINGS_BROWSER_MEMORY`) and at 10 tabs (`WINGS_BROWSER_MAX_TABS`, least recently used tabs close first).

How this compares with OpenManus, capability by capability, and what Wings still lacks: [docs/openmanus_comparison.md](docs/openmanus_comparison.md).

<a id="runner"></a>
## Code runner and workspace files

Turn it on once:

```bash
./scripts/setup.sh --with-runner      # or add "runner" to COMPOSE_PROFILES in .env, then docker compose up -d
```

The agent's `python` and `bash` tools then run in the `runner` container (patch 0009), with numpy, pandas, scipy, matplotlib, Pillow, openpyxl, XlsxWriter, python-docx, python-pptx, pypdf and tabulate installed. Ask in plain words, in Agent mode: "Analyse this CSV and chart sales by month", "Clean this spreadsheet and give me an Excel file", "Turn these notes into a Word document". The **Data Analyst** starter agent is set up for this kind of work.

* **What you see**: charts the code saves appear in the chat; every file the code writes is listed with a download link, and sidebar **Workspace files** lists everything in the workspace (newest first) for download. Files are never opened on the app's own address, only downloaded.
* **Approval**: running code is an action on your behalf, so upstream's approval gate asks before the first run in a task once the chat has seen web or email content ("Allow for this task", "Allow for this chat", "Deny").
* **Isolation** (`docker-compose.yml`, `docker/runner/runner_server.py`): no network at all (not even the internet), no secrets in its environment, no access to the app's data or keys, read only system files, all capabilities dropped, no privilege escalation, and limits on CPU time, memory, file size and processes. Only the agent workspace (`agent-workspaces` volume, mounted at `/workspaces`) is writable. The app reaches it with a shared secret (`WINGS_RUNNER_TOKEN`) from its own address only.
* **Limits**: each run stops after `WINGS_RUNNER_MAX_TIMEOUT` seconds (default 300). Upstream starts long commands as detached background jobs, which Docker cannot contain; Wings runs them in the runner at the full time limit instead and tells the agent so (patch 0014). Nothing keeps running between calls, and a run cannot install packages from the internet.
* **Where files live**: chats work in `/workspaces/default` (`ODYSSEUS_WORKSPACE_DEFAULT`). The browser's downloads appear inside it as `downloads/` (read only there). Backups include the workspace.

Settings: `WINGS_RUNNER_MAX_TIMEOUT=300`, `WINGS_RUNNER_MEMORY_MB=2048` (per run), `WINGS_RUNNER_MEMORY=3g` and `WINGS_RUNNER_CPUS=2` (whole container).

<a id="long-tasks"></a>
## Long tasks: plans and limits

**Plans**: press Tab in the message box (on a phone, swipe sideways on the message bar) to turn on **Plan mode**: the agent researches and proposes a checklist without changing anything, and you review it and press **Execute** under its reply. While it works it ticks steps off. Wings keeps the approved plan for that chat for 12 hours (patch 0012): when the run that carries it reaches its step limit with steps left, the chat continues on its own, once the reply has finished, up to 3 times per approval (press **Stop here** to prevent that), and a **Continue** or a typed "continue" carries the plan and its ticks forward instead of starting from memory. Only the plan you approved is ever sent back: the agent can tick its steps, but if it adds, removes or rewords steps the plan is dropped until you approve it again in the plan panel. Approvals, questions and the stop button still pause it. The step limit per message is in **Settings → Agent** (40 by default in Wings, up to 200).

**Token and spend limits** (patch 0013, all off until you set them in `.env`):

| Setting | Stops |
| --- | --- |
| `WINGS_RUN_MAX_TOKENS` | one agent reply, including all its tool rounds, after this many input plus output tokens |
| `WINGS_RUN_MAX_USD` | one agent reply after about this many US dollars |
| `WINGS_DAY_MAX_TOKENS` | each person's chats, agents, research and scheduled tasks for the rest of the day |
| `WINGS_DAY_MAX_USD` | the same, in estimated US dollars |

Every model call counts, where it reaches the provider: chat, agent rounds and the summaries after them, tools that ask another model, the `/api/chat` API, deep research and helper calls such as chat titles. When a limit is reached the agent stops before its next tool or round and says which limit it hit; a used up daily limit refuses new messages and research until midnight (in `TZ`). Token counts are the provider's when it reports them, otherwise upstream's estimate. Dollars are the provider's reported cost when it sends one (OpenRouter does), otherwise upstream's price table; models it does not know, and local models, count as zero dollars but still count tokens. A run can overshoot by the model round in flight and its closing summary.

<a id="map"></a>
## Where everything is in the app

| Feature | Location |
| --- | --- |
| Chats | Sidebar **Chats**; search past chats with Ctrl/Cmd+K; rename, archive, delete from a chat's menu |
| Deep research | Sidebar **Deep Research** (rounds, history, visual report), or the research switch in the message box |
| Web search in chat | Magnifier button in the message box |
| Documents and files | Sidebar **Files & Docs** (library, import, export); attach files to a chat with **+ → Attach** |
| Notes and to dos | Sidebar **Notes** (to do lists, due dates, reminders) |
| Scheduled agent tasks | Sidebar **Tasks** |
| Calendar | Sidebar **Calendar** |
| Email | Sidebar **Email** |
| Memory | Sidebar **Memory** (view, edit, delete, auto memory switch) |
| Agents | Sidebar **Agents** |
| Compare models | Sidebar **Compare** |
| Theme | Sidebar **Theme**; follows your system light/dark setting until you pick one |
| Settings | Gear at the bottom of the sidebar |

Keyboard: Ctrl/Cmd+K search chats, Ctrl/Cmd+Alt+N new chat, Ctrl/Cmd+/ focus the message box, Ctrl/Cmd+Alt+C calendar, Ctrl/Cmd+Alt+B sidebar, Ctrl/Cmd+, last tool window, Escape stops a reply, Ctrl/Cmd+Enter sends an email draft, Up/Down in an empty message box recalls earlier prompts. All are rebindable in **Settings → Shortcuts**.

<a id="backup"></a>
## Backup, restore, upgrade

```bash
./scripts/backup.sh                      # → backups/wings-<timestamp>/
./scripts/backup.sh --out /mnt/nas       # somewhere else
./scripts/backup.sh --include-models     # also downloaded models (large)
```

A backup is a folder with one archive per volume, the CalDAV accounts file, a manifest and SHA-256 checksums. SQLite databases are copied with SQLite's backup API, so it is safe while the app runs; ChromaDB and Radicale pause for the few seconds their archive takes. Archives are mode 600.

**The backup does not contain `.env`.** Keep `.env` separately; without its `ODYSSEUS_APP_KEY`, a restore brings back all data except stored credentials (re-enter API keys and mail/calendar passwords).

```bash
./scripts/restore.sh backups/wings-20261110-030000 --yes
```

Restore verifies checksums and archive safety, takes a fresh safety backup of the current state first, stops the stack, replaces each volume's contents (keeping file owners), starts the stack and waits until healthy. If `.env` holds a different key than the backup was made with, it stops before changing anything. Lost the matching `.env`? `./scripts/restore.sh <folder> --yes --accept-key-loss` restores everything and starts with stored credentials unreadable (it sets `WINGS_ACCEPT_KEY_LOSS=1`); re-enter API keys and passwords in Settings, then remove that line from `.env`.

Nightly backups with cron (`crontab -e`):

```cron
15 3 * * * cd /path/to/wings && ./scripts/backup.sh --out /mnt/nas/wings >> /var/log/wings-backup.log 2>&1
```

**Upgrade**: `./scripts/upgrade.sh` takes a backup, `git pull`s this repository, creates any helper container secret a newer version added (the page renderer's, for one), rebuilds and restarts, and tells you how to roll back if the new version is unhealthy. Wings pins upstream by image digest; moving to a newer Odysseus is a Wings change (bump `upstream.lock`, run `scripts/dev/rebase-overlay.sh`, rebuild, run the tests). The image build refuses to run if any patched upstream file changed underneath the overlay.

**Moving an existing Odysseus install** into Wings: restore its `data/` into the `odysseus-data` volume, then run `./scripts/adopt-key.sh --yes` to move its key file into `.env`.

<a id="security"></a>
## Security model

* **Encryption at rest**: provider and endpoint API keys, email passwords and OAuth tokens, CalDAV passwords, MCP server environment and OAuth tokens, and the per chat copies of endpoint keys are Fernet encrypted in the database. The key comes from `ODYSSEUS_APP_KEY` in `.env`, never from the data volume, so a stolen volume or backup cannot be decrypted. A wrong or mismatched key stops the container at start (exit code 78) with a clear message instead of silently losing credentials.
* **Child processes**: the server is started by `wings.serve`, which moves the key into memory and drops the admin password before anything runs, so no child process inherits either. Third party MCP servers, Cookbook engines, pip installs and terminal sessions additionally get an environment without any secret looking variable (provider keys, OAuth client secrets, tokens). A process that lacks the key refuses to create a new key file.
* **Network**: the app listens on loopback by default; SearXNG and ChromaDB have no host ports; HTTPS through Caddy with the proxy pinned to one trusted address (`FORWARDED_ALLOW_IPS`) so login rate limits see real client addresses.
* **Email**: see [No email leaves without your click](#no-email-leaves-without-your-click).
* **Pinned supply chain**: every image is pinned by digest; Python packages Wings adds (the browser driver) are pinned by SHA-256; Wings disables upstream's start time download of an unpinned npm browser package.
* **Browser container**: no host ports and no access to your data or keys. Chromium's DevTools listens on loopback and the screen (VNC and its WebSocket bridge) on unix sockets, so no web page in that browser can reach them. The only way in is a guard that requires both the `odysseus` container's address and a shared secret (`WINGS_BROWSER_TOKEN`, generated by setup.sh and held only in memory by the app). The app relays the screen only to a signed in person allowed to use the browser, over a same origin WebSocket that closes within 30 seconds of the session ending; the viewer's code ships in the Wings image, not from the browser container. Every request Chromium makes goes through a proxy that refuses private and local addresses after resolving them itself. All capabilities are dropped and privilege escalation is blocked. Chromium runs without its own sandbox (Docker's default seccomp profile blocks the namespaces it needs), so the container is the isolation boundary. Downloads are read without following links. Its cookies are live sessions: treat the `browser-profile` volume like a password store. Backups leave it out on purpose.

* **Page renderer container**: the browser image without a profile volume (its profile lives in memory and is gone on restart), on its own network with its own secret (`WINGS_RENDERER_TOKEN`), so a page that took it over would find no logins and could not reach the signed in browser. The same URL rules and egress proxy as the browser apply.
* **Code runner container**: no network of any kind (its only network is internal and shared with the app alone), no host ports, no secrets in its environment, no access to the app's data volume or keys, read only root filesystem, all capabilities dropped, privilege escalation blocked, CPU, memory, file size and process limits per run, and the run's process group killed when it ends. It accepts work only from the `odysseus` container's address with the shared secret `WINGS_RUNNER_TOKEN` (generated by setup.sh, held only in memory by the app). The agent workspace it writes to is shared with the app's file tools and downloadable only by a signed in admin, never rendered on the app's address. Code the agent runs can read and change everything in the workspace except `downloads/`, which is read only in both the app and the runner.
* **Token and spend limits**: optional hard caps per reply and per person per day ([Long tasks](#long-tasks)).

Not covered (upstream behaviour, documented rather than changed): web search API keys typed into **Settings → Search** are stored in plaintext `settings.json` (put them in `.env` instead: `DATA_BRAVE_API_KEY`, `TAVILY_API_KEY`, `SERPER_API_KEY`); two factor seeds and server side session tokens sit unencrypted in the data volume; the agent approval gate is taint based (it asks after untrusted web or mail content has entered a run, not before every action). Do not expose the app to the internet without HTTPS, two factor authentication and a strong password.

<a id="patches"></a>
## What Wings changes in upstream

| Patch | Change |
| --- | --- |
| [0001](patches/0001-encryption-key-from-env.patch) | Encryption key from `ODYSSEUS_APP_KEY`; never written to the data volume; fail loudly on a mismatch |
| [0002](patches/0002-scrub-secrets-from-child-processes.patch) | Secrets removed from the environment of third party MCP servers and Cookbook processes |
| [0003](patches/0003-encrypt-session-headers-and-mcp-env.patch) | Encrypts endpoint keys that upstream copies in plaintext into each chat's headers, and MCP server env; stops logging an OAuth client secret |
| [0004](patches/0004-email-send-requires-a-person.patch) | `WINGS_FORCE_EMAIL_CONFIRM`: server side "a person clicked Send" rule for every outbound email |
| [0005](patches/0005-per-agent-tool-access.patch) | Per agent tool access, enforced server side, with an editor UI |
| [0006](patches/0006-sidebar-agents-files-and-os-theme.patch) | Agents and Files entries in the sidebar; theme follows the OS |
| [0007](patches/0007-safer-defaults-and-caldav-fix.patch) | Document auto delete job ships paused; learned skills need approval; CalDAV writeback crash fix |
| [0008](patches/0008-browser-automation.patch) | `private_browser` page actions run in the Wings browser container; web task wording routes to it; old page outlines leave the context; live view routes and sidebar entry |
| [0009](patches/0009-code-runner.patch) | `python` and `bash` run in the Wings runner container; Workspace files page and sidebar entry; fixes an upstream bug that refused every approved code run (`RequestAuthority.intersect` marked the request's own authority as inherited) |
| [0010](patches/0010-browser-file-transfer.patch) | Browser uploads from the workspace (classified as an external action, so the approval gate asks); downloads land in the workspace |
| [0011](patches/0011-browser-pointer-actions.patch) | Browser clicks at screen points and drag and drop, with the same guards as clicks by element |
| [0012](patches/0012-plan-continuation.patch) | An approved plan stays pinned on Continue and is followed automatically, a bounded number of times, past the step limit |
| [0013](patches/0013-run-budget.patch) | Hard token and spend caps per reply and per person per day |
| [0014](patches/0014-runner-background-marker.patch) | Long commands upstream would start as detached jobs (which always failed in Docker) run in the runner instead |
| [0015](patches/0015-render-javascript-pages.patch) | Web fetch and deep research read pages that need JavaScript in the stateless renderer |
| [0016](patches/0016-model-retry-backoff.patch) | Model rate limits and overloads are retried with backoff before a reply starts |
| [0017](patches/0017-mcp-server-instructions.patch) | The usage notes MCP servers send when they connect reach the model, labelled as the server's words, on turns where that server's tools are offered |
| [0018](patches/0018-browser-batch-and-hover.patch) | Several browser actions in one call (per item or per page), each guarded as if alone; hover for menus |

Configuration level fixes (in `docker-compose.yml`): agent tools inside Docker called the wrong internal port (`ODYSSEUS_INTERNAL_BASE`); SearXNG crash looped on hosts without IPv6; Radicale and Caddy hardening; scheduled tasks no longer stall while a browser tab is open.

Patches form a series: each is the diff of one commit on top of the pinned upstream commit, so later patches may touch files earlier ones changed (`scripts/dev/export-patches.sh` writes them from such a checkout).

How the overlay works: `docker/odysseus/Dockerfile` starts `FROM` the upstream image pinned by digest, checks that each file it replaces is byte identical to the upstream version the patches were written for (`overlay/UPSTREAM_SHA256SUMS`), copies `overlay/app/` over `/app`, and runs a preflight (`overlay/app/wings/preflight.py`) before the upstream entrypoint. `patches/` is the source of truth; `scripts/dev/rebase-overlay.sh` regenerates `overlay/` from it.

<a id="verification"></a>
## What was verified, and what you must verify

See [docs/verification.md](docs/verification.md) for the full record. Summary:

Verified by running the real stack (with a stand in for the upstream image, built from the same commit, because the build environment could not download from GitHub's registry):

* `setup.sh` fresh install, idempotent re-run, health, login; named volumes; backup → delete → restore round trip; key adoption from an existing volume; the fail loud key check
* 4,998 upstream tests over every area the patches touch: no new failures with the email gate off; with it on, only the 30 tests that assert sends happen without a person's session fail, by design
* 44 Wings unit tests; the end to end suite in `tests/e2e/` (streaming chat, model switch, per agent tool access A/B, human send delivered, agent and token sends refused, unapproved scheduled mail refused, CalDAV round trip with Radicale, no plaintext secret anywhere in the data volume or logs)
* the UI changes in Chromium: Agents entry, tool access editor saving and restoring, OS light/dark
* browser automation against local test pages. Through the real chat API with the web switch off, as in the UI: web task wording is offered the browser; open, fill, dropdown, checkbox and submit; a card field handed to the person and the order not placed; `file:` URLs and page JavaScript refused; the live view needs a session and a same origin WebSocket. Directly against the browser backend: iframes, pop up tabs, dialogs, screenshots, downloads, and 20 hostile page cases (passwords and card numbers by single keys, paste or inside iframes; pay and send by Space, Control+Enter, hidden labels and dialogs; forged refs and markers; account scoping), all handled; the egress proxy refused Chromium's own request to a private address. Some chat turns in the test run never reached the mock model (a test environment fault, see the verification record); those are reported as skipped, not passed

Not verifiable here, verify on your server:

1. **The real upstream image** (`ghcr.io/odysseus-dev/odysseus:1.0.3-9d0d372`): run `./scripts/setup.sh`; the build must pass the checksum guard. Then `./tests/unit/run.sh`.
2. **Real model providers**: add one in Settings, chat, switch models, run a Deep Research question and check that the citations open the cited pages.
3. **Gmail** with an app password: read, summarize, draft; send one test mail to yourself by clicking Send; ask an agent to "email X" and confirm you get a draft, not a sent message.
4. **iCloud (or your CalDAV) sync**: add the account, create an event in each direction.
5. **First start downloads**: the embedding model (FastEmbed from Hugging Face) and Ollama models need internet access from the server.
6. **HTTPS**: `setup.sh --domain` needs DNS pointing at the server and ports 80/443 open.
7. **NVIDIA GPU** for Ollama: `docker run --rm --gpus all ubuntu nvidia-smi` must work first.
8. **Browser on real websites** with your model: the test environment had no internet, so no public site was tried. Sign in to one site through the live view, then give the agent a small task there and watch it in the live view.

To run the end to end suite on a **test** install (it creates test data): `./tests/e2e/run.sh`. It needs `EMAIL_ALLOW_PRIVATE_IPS=true` for its local test mail server.

<a id="troubleshooting"></a>
## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ODYSSEUS_APP_KEY is not set` | Run `./scripts/setup.sh` |
| Container restarts with "configuration error: .app_key exists and differs" | Data came from elsewhere: `./scripts/adopt-key.sh --yes` |
| Container restarts with "does not decrypt data already stored" | `.env` is from another install: put back the matching `.env` (or see `--accept-key-loss` under Backup) |
| Login works only on `localhost` | Add the exact URL you use to `ALLOWED_ORIGINS` in `.env` |
| Mail account on Proton Bridge, DavMail or a LAN server cannot send | `EMAIL_ALLOW_PRIVATE_IPS=true` |
| An agent says it cannot send email | Intended: open the draft it made and click Send |
| Task or reminder emails are not delivered | List your address in `WINGS_EMAIL_SELF_ADDRESSES` |
| Scheduled emails did not go out after a restart | Open the Email view once; upstream starts its scheduled-send worker there |
| Local CalDAV server rejected | `ODYSSEUS_ALLOW_PRIVATE_CALDAV=1` |
| No **Live browser** entry in the sidebar | Browser not enabled (`./scripts/setup.sh --with-browser`) or your account is not in `WINGS_BROWSER_USERS` |
| Agent says "WINGS_BROWSER_TOKEN is not set" | Run `./scripts/setup.sh` once more (it adds the missing secret), then `docker compose up -d` |
| Agent says "The browser service is not running" | `docker compose ps browser`; `docker compose logs --tail 50 browser` |
| Agent stops with "PAUSED, needs the person" | Intended: do that step in the live view, then tell the agent to continue |
| A site on your LAN is refused | `WINGS_BROWSER_ALLOW_PRIVATE=1` in `.env`, then `docker compose up -d` |
| Slow or no answers from a local model | Use a smaller model, raise `OLLAMA_CONTEXT_LENGTH` carefully, or add a hosted provider |
| Agent says "The code runner is not running" | `./scripts/setup.sh --with-runner`, or `docker compose ps runner` and `docker compose logs --tail 50 runner` |
| Agent says "Process creation requires a sealed workspace" | The chat has no workspace: check `ODYSSEUS_WORKSPACE_DEFAULT` in `.env` (default `/workspaces/default`) and restart |
| Code run "timed out" | Raise `WINGS_RUNNER_MAX_TIMEOUT` in `.env` (seconds), then `docker compose up -d` |
| "Daily token limit reached" or "Run spend limit reached" | Intended: raise or empty the matching `WINGS_RUN_MAX_*` or `WINGS_DAY_MAX_*` setting in `.env`, then `docker compose up -d` |
| Anything else | `docker compose ps`, `docker compose logs --tail 100 odysseus` |

## License

Upstream Odysseus is AGPL-3.0-or-later; the patches and the overlay are derived from it and carry the same license. See [NOTICE.md](NOTICE.md).
