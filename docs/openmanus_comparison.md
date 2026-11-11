# Wings and OpenManus: what each does, and what Wings still lacks

Compared against OpenManus at commit `3309bf4` (github.com/FoundationAgents/OpenManus),
read in full for the agent loop, tools, browser and sandbox code. OpenManus is an
open source reimplementation of the Manus product idea, not Manus itself; the hosted
Manus service is closed source and was not examined.

## Facts: how OpenManus works

* **Loop**: `ReActAgent` → `ToolCallAgent` → `Manus`. Each step the model either calls
  tools or answers; the run stops at `max_steps` (20 for `Manus`) or when it calls
  `terminate`. Tool output is cut at `max_observe` (10,000 characters).
* **Stuck detection**: if the last assistant message repeats an earlier one twice
  (`duplicate_threshold = 2`), it adds "Observed duplicate responses. Consider new
  strategies…" to the prompt.
* **Tools of the main `Manus` agent**: `PythonExecute` (runs Python), `StrReplaceEditor`
  (view, create and edit files), `AskHuman`, `Terminate`, plus any MCP servers.
* **Browser** in the current main branch: an MCP server started as
  `browser-use --cli-mcp` (Browser Use CLI 3.0) with two tools, `browser_exec` (the
  model writes Python that drives the browser) and `browser_screenshot`.
  The older built in `BrowserUseTool` (still in the history) gave the model numbered
  interactive elements and a screenshot after every step, with actions such as
  go_to_url, click_element, input_text, scroll, send_keys, select_dropdown_option,
  switch_tab and `extract_content` (a second model call that pulls a stated goal out
  of the page as markdown).
* **Sandbox agent** (`SandboxManus`): runs tools inside a Daytona cloud sandbox with a
  VNC live view, a shell, file tools, a vision tool, a browser tool and a desktop
  `computer_use_tool` (move, click, type, press, scroll, mouse down/up, drag).
* **Planning**: `PlanningFlow` asks a model for a step list, then runs agents step by
  step and marks progress.
* **Safety**: no built in rules about passwords, payments, private networks or human
  approval; `AskHuman` exists but the model decides when to use it.

## Facts: what Wings has now

| Capability | OpenManus | Wings |
| --- | --- | --- |
| Agent loop with tools, step cap | yes, 20 steps | yes (upstream Odysseus), 40 steps by default, Settings → Agent |
| Loop and repeat detection | duplicate message check | upstream loop breaker; seen in testing blocking an exact repeated browser call |
| Ask the person mid task | `AskHuman` | `ask_user` |
| Plans | `PlanningFlow` | `update_plan` and task lists in the agent loop |
| Browser: numbered elements, act by number | old `BrowserUseTool` | yes (`private_browser`, patch 0008) |
| Browser: forms, dropdowns, checkboxes, keys, scroll, tabs, popups, iframes, dialogs | yes | yes |
| Browser: screenshots for vision models | yes | yes (`screenshot`) |
| Browser: downloads the agent can read | not built in | yes, text and PDF |
| Live view and taking over | VNC in the Daytona sandbox | yes, inside the Wings app, behind your sign in |
| Logins that persist | per sandbox | yes, browser profile volume |
| Passwords, codes, card numbers never typed by the agent | no | yes, always |
| Pay, order, send, publish, delete clicks handed to the person | no | yes, by default |
| Private network and non web URLs blocked | no | yes, by default |
| Works with text only local models | partly (screenshots assumed) | yes, the outline is text; screenshots optional |
| Email, calendar, notes, documents, memory, research in the same agent | no | yes (upstream) |
| Run Python or shell for the user | yes (`PythonExecute`, sandbox shell) | **no** |
| Edit files in a workspace | yes (`StrReplaceEditor`) | upstream has workspace file tools; not tested in this build. Documents (draft, rewrite, version history) work |
| Desktop control outside the browser | yes, sandbox `computer_use_tool` | **no** |
| Click by screen coordinates, drag and drop | sandbox tools | **no** |
| Goal directed page extraction with a second model call | old `extract_content` | no; `read` returns the page text in parts to the same model |
| Upload a file into a web form | possible through Python | **no** |
| Many browsers in parallel | one sandbox per task | one browser; actions run one at a time, one tab per chat |

## What Wings is still missing, in order of impact on everyday work

1. **Code execution** (Python and shell). Needed for spreadsheet clean up, file
   conversion, data analysis, charts and anything a script does faster than clicks.
   Upstream's `bash` and `python` tools refuse to run without bubblewrap, which the
   upstream image does not ship. Wings deliberately did not weaken that.
2. **File uploads into web forms** (attach a CV, upload a receipt to an expense
   portal). The agent cannot do it, and neither can the person through the live view,
   because the live view shows the server's browser, not their computer.
3. **Desktop and canvas control**: coordinate clicks and drag and drop for canvas
   based apps (maps, whiteboards, some spreadsheet and design tools).
4. **Parallel browsing**: one browser serves every chat and scheduled agent in turn.
5. **Cheaper reading of long pages**: OpenManus's old `extract_content` asked a second
   model call to pull out only what the goal needs; Wings sends page text in parts to
   the main model, which costs more context on very long pages.

## Assumptions

* The person runs one Wings install for themselves; one shared browser profile is
  acceptable. Several people would each need their own profile (not built).
* Most of the person's manual web work is portal sign ins, forms, downloads and look
  ups, which the browser now covers; heavy data work would need item 1.

## Risks

* **Prompt injection through web pages**: a page can contain text written to steer
  the agent. Mitigations in place: the agent cannot type secrets (by fill, by single
  keys, by paste or inside iframes), cannot commit payments or messages (by click,
  Enter, Space or dialogs), cannot reach private addresses (every browser request
  passes a filtering proxy) or run page JavaScript, and upstream's taint gate asks
  before effectful tools run after untrusted content.
  Residual: the agent can still be steered into reading or clicking ordinary things
  on public sites, and into filling non secret fields wrongly. Watch the live view
  for anything that matters.
* **Session theft**: the browser profile holds live logins. It is not in backups, has
  no host port, and is reachable only by the app with a shared token; anyone with
  Docker access on the server can still read it.
* **Browser exploits**: Chromium runs without its own sandbox inside the container;
  a renderer exploit gets the container (and its logins) but not your Wings data,
  keys or the app's origin. Keep the browser image updated.
* **Scheduled agents** are not API token calls, so the browser's API token refusal
  does not apply to them: a scheduled agent may use the browser like a chat does.
  How upstream runs webhook triggered agents was not checked; treat them the same.
* **Bot detection**: some sites will block or challenge automation; nothing in Wings
  evades that.
* **Model quality**: small local models lose track over long web tasks.

## Recommendations

1. Next build: a separate **code runner container** (same pattern as the browser
   container: no secrets, no data volumes except a scratch workspace, capped CPU and
   memory, network off by default) that upstream's `python` and `bash` tools call, so
   the bubblewrap requirement is met by container isolation instead of being disabled.
2. Then: **uploads from Wings Files** into a page's file field, limited to files the
   person attaches to that chat.
3. Keep the handoff rules on. They are what make it safe to let the agent use your
   logged in accounts.
