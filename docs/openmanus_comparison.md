# Wings and OpenManus: what each does better, and what Wings took

Compared against OpenManus at commit `3309bf4` (github.com/FoundationAgents/OpenManus)
and Wings at patch 0018. OpenManus is an open source reimplementation of the Manus
product idea, not Manus itself; the hosted Manus service is closed source and was
not examined.

## How the comparison was done

Nine areas were compared subsystem by subsystem, by reading both code bases
end to end: the agent loop, planning, code execution, data analysis and charts,
search and crawling, the sandbox and computer use, MCP and multi agent work, the
model layer, and the browser features that still differed after patch 0011.
Every claim that OpenManus is better, or has something Wings lacks, was given to
two separate reviewers told to refute it. 94 agents ran in all.

Result: 114 capabilities judged. Wings was better or alone in 68, they were equal
in 4, 13 OpenManus advantages were confirmed, and 29 claimed OpenManus advantages
were refuted. In 3 of the refuted ones the reviewers still found OpenManus ahead
on part of the claim (statistics on charts, hover and right click, desktop
control); they are listed below with the confirmed gaps. Several confirmed gaps
were closed while the comparison ran (patches 0009 to 0018), and the reviewers
judged some of those features already better in Wings.

## Facts: how OpenManus works

* **Loop**: `ReActAgent` → `ToolCallAgent` → `Manus`. Each step the model either calls
  tools or answers; the run stops at `max_steps` (20 for `Manus`) or when it calls
  `terminate`. Tool output is cut at `max_observe` (10,000 characters).
* **Tools of the main `Manus` agent**: `PythonExecute`, `StrReplaceEditor` (view,
  create and edit files, with undo), `AskHuman`, `Terminate`, plus MCP servers.
* **Browser**: by default an MCP server, `browser-use --cli-mcp` (Browser Use CLI
  3.0), whose `browser_exec` runs Python the model writes against a persistent
  browser session (loops, pagination, bulk extraction in one step), plus
  `browser_screenshot`. No guardrails for passwords, payments or private networks.
* **Sandbox agent** (`SandboxManus`): Daytona cloud sandboxes with a VNC view,
  shell sessions that can run in the background, file and vision tools and a
  desktop `computer_use_tool`.
* **Planning**: `PlanningFlow` asks a model for a step list, then runs an agent
  once per step with the plan status re-injected, and can route steps to
  different agents.
* **Model layer**: OpenAI compatible APIs plus Azure OpenAI and AWS Bedrock; retries with
  backoff; an optional cap on cumulative input tokens.
* **Safety**: no rules about passwords, payments, private networks or approval;
  `AskHuman` exists but the model decides when to use it.

## What Wings took from OpenManus, the Wings way

| OpenManus advantage (confirmed) | Wings now | Patch |
| --- | --- | --- |
| Runs the model's Python and shell | Code runner container: no network, no secrets, only the workspace writable, one approval per task; charts shown in the chat, files downloadable | 0009 |
| Files move between browser, code and the person | Downloads land in the workspace; uploads into web forms from the workspace, each approved | 0010 |
| Click at screen points, drag and drop | Pointer clicks and drags with the same pay and send guard as clicks by element, hover checked first, shadow DOM read, drops never type into fields | 0011 |
| Plan driven execution | The approved plan stays pinned per chat and continues on its own past the step limit, a bounded number of times; only the person's approved text is ever resent | 0012 |
| Cap on cumulative tokens (input only, off by default) | Run and daily caps on tokens and dollars, per person, counted on every model call where it reaches the provider | 0013 |
| Background shell jobs | `#!bg` commands run in the code runner with its full time limit (foreground) | 0014 |
| Reads pages that need JavaScript | A stateless renderer container (no logins, public addresses only, time limited, bot checks reported) for web fetch and research | 0015 |
| Retries with backoff | 429 and overload retries before a reply starts; no retry when the provider asks for longer than 20 seconds | 0016 |
| MCP server instructions reach the model | Labelled as the server's words, only on turns where its tools are offered | 0017 |
| Scripted multi step browser work in one call | `batch`: up to 20 of Wings' own actions, repeated per item or per page, each checked as if alone; no model written code against the signed in browser | 0018 |
| Hover | `hover` by ref, text or point, so hover menus appear in the outline | 0018 |
| Data analyst agent | A seeded Data Analyst agent that computes every figure with code and states trends and outliers | scripts/seed-agents.sh |

## Where Wings is ahead (judged by the reviewers)

Agent loop: step cap behaviour, stuck and loop detection, completion
verification, context compaction and overflow recovery, tool failure handling,
asking, stopping and resuming. Planning: plan review and approval before anything
runs, progress view, revising plans, scheduled and chained jobs, iterative deep
research. Code and files: workspace confinement, faithful results and exit codes,
secret and network isolation of agent code. Search: several engines with
fallback, fetched content, batch crawl, protection against requests to internal
addresses, untrusted content containment, PDFs and YouTube. Sandbox: OCR,
transcription, vision on files, live view and take over. MCP: local and remote
servers with sign in, approval gate on MCP tools, scoped API tokens, personas,
group chat, second opinion and teacher models. Model layer: provider coverage,
fallback chains, usage accounting, image input, models without native tool
calling, reasoning parameters, per role models, side by side comparison.
Browser: tabs, core actions, live view and take over, downloads, and every
guardrail (passwords, codes, cards, pay and send buttons, private addresses).

## What OpenManus still does better (confirmed, not taken)

| Gap | Impact | Why not now |
| --- | --- | --- |
| A plan runner that starts one agent run per step, with the plan re-injected each time | medium (reviewers split: one found its main benefit false, since OpenManus keeps one growing memory across steps) | 0012 covers the failure people hit (plans lost at the step limit). A server side runner needs pause, cancel and retry semantics and per step approval; worth doing as its own piece of work |
| Routing plan steps to specialist agents | medium | Needs a nested agent run under the exact approval model; personas already exist and the agent can ask another model |
| Per task sandbox lifecycle (fresh sandbox per task, auto archive, parallel sandboxes) | medium | One shared runner with one workspace volume is simpler to secure and back up; per task sandboxes need a container manager with Docker access, which Wings keeps away from the app |
| Detached long running shell sessions (start, check later, kill; named sessions) | low to medium | Upstream's detached jobs need bubblewrap, which Docker's default profile blocks; 0014 runs them in the foreground instead |
| Azure OpenAI and AWS Bedrock as native providers | medium | Both work today through an OpenAI compatible gateway (LiteLLM and similar); native adapters are upstream's provider layer |
| Undo of the agent's file edits | low | Workspace files are in a backed up volume; no per edit history yet |
| Refreshing an MCP server's tool list during a run | low | Reconnecting the server refreshes it |
| Desktop control outside the browser | low | No desktop in Wings' containers; the browser covers web work |
| Several browser backends (attach to another browser, cloud browsers, upstream proxy) | low | One hardened browser holds the person's logins; more backends multiply what must be guarded |
| Automatic statistics on charts (trend, outliers, correlation) as a tool | low | The Data Analyst agent is instructed to compute and state them with code |
| Right click | low | Pages rarely need it; Chromium's own menu is not part of the page |

## Assumptions

* OpenManus was read at one commit; later commits may change any line above.
* "Better" means more capable for a person using it, not more code. Reviewers
  were told to refute; a claim survived only when both could not.
* Wings features were verified on a stand in image of upstream (same source),
  as described in `docs/verification.md`, not on the published image.

## Risks

* `batch` runs many page actions per approval of the browser task. Each step
  passes the same guards as a single call, and anything that commits (pay,
  send, order, delete, passwords, codes, cards) still stops for the person, but
  a batch does more before the person next looks. The caps (20 steps, 100 runs,
  25 pages, 5 minutes) bound it.
* The spend caps count estimated tokens when a provider does not report usage,
  and price only models in upstream's table or with provider reported cost.
  They limit spend; they are not a bill.
* The plan continuation and batch features make long unattended work more
  likely. Set the token and spend caps before relying on them with paid models.

## Recommendations

1. Set `WINGS_RUN_MAX_USD` and `WINGS_DAY_MAX_USD` if any paid model is
   configured.
2. Next build, if wanted: the server side plan runner (one run per step, pause,
   cancel, retry, per step approval), then native Azure and Bedrock adapters.
3. Leave desktop control and multiple browser backends out unless a concrete
   task needs them; each widens what has to be guarded.
