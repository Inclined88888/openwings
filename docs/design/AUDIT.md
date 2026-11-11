# Design audit: before and after Lichen

This is the audit behind the Lichen look (theme key `ow-sage`, see
[README.md](README.md)). It follows the Impeccable audit method: a score on
five dimensions (accessibility, performance, theming, responsive design,
implementation integrity), a check for generic "AI look" patterns, Nielsen's
ten heuristics, five personas (a power user, a first-timer, an accessibility
user, a stress tester and a phone user) and a motion review.

The first audit compared Lichen as first built with Duet (the default until
now) and upstream's original theme, in the running app at 1440, 720 (200%
zoom), 390 and 320 pixels wide, and right to left. It found 21 problems. All
of them were fixed except the parts listed under "Still open". The final state
was then checked again in the real app (see "How the final state was
checked").

## Score

| Dimension | First audit | Now | What changed |
| --- | --- | --- | --- |
| Accessibility | 2 / 4 | 3 / 4 | AA contrast on every window, tab and Settings panel (0 failures across 75 desktop and 38 phone views), no text under 12px in the windows scanned, a 2px focus edge on the message box, readable menu shortcuts, theme swatches usable from the keyboard. Not 4: loading spinners on the stop button still turn under reduced motion. |
| Performance | 3 / 4 | 3 / 4 | Only transform and opacity animate; motion.js measures in one frame pass (opening a window costs about 6ms). Not 4: the looks stylesheet (about 310 KB for three looks) blocks the first paint. |
| Theming | 2 / 4 | 4 / 4 | One token file; Lichen reaches every page (Live browser, Workspace files, web search, the sign-in result page and the not-found page included); no upstream colour, font or spinner shows through. |
| Responsive | 3 / 4 | 3 / 4 | Long model names wrap, the chat drops to one column in narrow areas, nothing scrolls sideways at 320px. Not 4: number columns are left-aligned and wide tables have no scroll cue on phones. |
| Implementation integrity | 2 / 4 | 4 / 4 | Lichen is the default and in the picker, motion.js is loaded, the looks tests cover one-mode looks (272 unit tests pass). |
| **Total** | **12 / 20** (acceptable) | **17 / 20** (good) | The "now" column is our own re-assessment against the same rubric. |

## What was generic, what replaced it, and why

**Colour.**
Before: Duet put white paper (L* 99) on a bright mist page (L* 92). Upstream's
theme was cyan and pink text on near black (L* 18 and 5), the typical AI look.
Now: a sage page at L* 82, paper one step up (L* 87), side panels one step down
(L* 79), moss ink at 9.6:1. One ochre accent, used only for the main action,
the current choice, focus and state. Green outweighs red everywhere, so the
page never reads as cream, and nothing is purple.
Why: the owner asked for no AI look and nothing bright or dark: a calm middle
range that still reads at AA all day.

**Type.**
Before: upstream used a monospace font (Fira Code) for everything, as a
costume, with 11-12px navigation. Duet had one rounded sans and no display
voice.
Now: Sofia Sans for reading and controls (16px, 1.55 line height, 68 characters
per line), its Extra Condensed width for focal titles only, Red Hat Mono for
code only. Upstream's fonts are no longer even downloaded under a look.
Why: monospace is for code; a clear display step gives each screen a focal
point; a comfortable measure and leading for long answers.

**Welcome screen.**
Before: Duet put a logo tile above the name inside a rounded white card, with
the tip and Incognito in a footer strip (the icon-tile-stack pattern).
Upstream had a pink monospace title and faded text under 3:1, and the line
"A voyage begins."
Now: one focal point: the wings mark beside a 96px condensed wordmark, a thin
rule the width of the column, then the tagline, the tip and the message box,
all on the same column edges. No card, no tile.
Why: one clear focal point per screen; no nested cards.

**Chat.**
Before: Duet used big rounded white answer cards and a pink bubble for your
words. Upstream had bordered bubbles left and right, purple table headers and
cyan text.
Now: a ledger: who spoke and when in a 128px column (small caps), the words in
a reading column, a rule between turns. Answers sit on the page; your words sit
on a thin paper slip. When the chat area is narrow (beside Notes, on phones) it
drops to one column.
Why: no generic rounded cards; hierarchy from a grid, type and rules.

**Tables and code.**
Before: tables in a rounded box (Duet) or a fully boxed grid with purple
headers (upstream); Duet showed Run, Edit and Copy only on hover.
Now: rules above and below, hairlines between rows, small-caps headers, figures
of equal width. Code sits on paper under a strip that names the language and
always shows Run, Edit and Copy, each with a focus ring.
Why: tabular figures; rules instead of boxes; controls that touch screens can
reach.

**Message box.**
Before: Duet had a large rounded pill, a teal pill toggle and a round send
button with a glow. Upstream had a black box with a cyan border.
Now: a paper field with 4px corners and an edge at 4:1. Send is the only filled
control on screen. Agent / Chat is a ruled track with a raised paper tab for
the current mode. Focus shows a 2px ink edge. The tool buttons keep their
labels (More tools, Web search, Shell access) on wide screens.
Why: one primary action per screen; one button shape; recognition rather
than recall.

**Login.**
Before: a rounded white card with a logo tile (Duet); a black card over a
starfield of sparkles (upstream).
Now: no card: the wings mark, a 72px wordmark, an ink rule that draws in once,
small-caps labels and square fields with a 2px ochre focus ring.
Why: a focal screen needs no container; no sparkle motifs.

**Settings and tool windows.**
Before: small "Settings" titles, every section boxed in its own card, 9-11px
navigation, descriptions and counts faded with opacity; a white card inside
each white window (Duet).
Now: every window and Settings share one frame: a condensed title (65px header,
down from 82px in the first build), tabs underlined in ochre, sunk fields,
outlined badges, sections divided by rules, task rows ruled rather than boxed.
Faded text is now full-strength muted ink at 12px or more. In Settings the
labels sit in one 128px column so every field starts on the same line, and
there is one filled button per section.
Why: one control vocabulary; contrast; alignment on a grid.

**Menus.**
Before: a white menu with a thin border and a wide shadow (Duet), a black menu
with a cyan border (upstream).
Now: a paper menu with one soft shadow, no border and 4px corners; items at
14.5px; shortcut hints in muted ink at full strength; Delete in oxblood.
Why: depth from one shadow, not a border plus a shadow; readable hints.

**Decoration.**
Before: background effects (starfield, sparkles, grid lines), a spinning
gradient edge on the Deep Research window, pulsing dots, a sparkle by task
names, sea-themed spinners (a whirlpool, a sine wave, a dot riding an ASCII
wave) and nautical wording.
Now: none of these under a look. Busy states show one neutral mark: three small
dots that fade in and out. The wording no longer names upstream or uses sea
words (patch 0024).
Why: no glows, orbs, sparkles or decorative motion; the product should not feel
like a renamed upstream.

**Motion.**
Before: upstream's welcome wiped in with a clip and slid with a bouncy
overshoot by animating its position, on every visit; hover only changed
colours; there was no press feedback.
Now: one staged entrance when the app loads (the title rises 16px over 400ms,
the rest 8px), which New Chat does not replay; new answers rise 4px; windows
grow in from 96%; tabs cross-fade with an 8px slide on clicks only; a chat's
title moves from the sidebar into the top bar; a window moves into its tile.
Hover grows clickable things to 102% (150ms), press shrinks them to 97%
(100ms), release takes 200ms. Only transform and opacity animate.
Why: motion should explain where things come from and go; quick, asymmetric
press and release; no animation on actions people repeat all day.

**Reduced motion.**
Before: upstream's wipe, slide and loops ignored the reduced-motion setting.
Now: every movement value is zero, entrances do not play, counts and line
drawing are off. A higher-contrast setting raises rules and muted text to full
ink.
Why: WCAG 2.3.3 and the owner's instruction.

## The 21 findings and what happened to each

Severity: P0 blocks the look, P1 breaks the brief or WCAG, P2 should be fixed,
P3 is polish. "Fix round" means it was fixed in the look's own files
(`overlay/app/wings/brand/`) and checked then; "0023" means patch 0023, checked
again in the final app.

| # | Finding (severity) | Fix | Status |
| --- | --- | --- | --- |
| 1 | Nobody could reach Lichen: not in the theme list, Duet still the default, motion.js not loaded, and the looks test required a night version (P0) | Lichen first in the picker and the default for everyone in one mode; motion.js loaded by the app page; the test knows one-mode looks | Fixed (0023) |
| 2 | Live browser and Workspace files stayed white with an electric blue accent (P1) | Both pages load the looks stylesheet and wear the chosen look | Fixed (0023) |
| 3 | Every tool window dimmed the whole page although windows are not modal; two windows took it to L* 46 (P1) | Windows float on a shadow and a 1px rule; only blocking layers dim the page | Fixed (fix round) |
| 4 | Chat actions menu: shortcut hints at 1.7-2.0:1 and 9.5px, items at 11px (P1, WCAG 1.4.3) | Items 14.5px, hints 12.5px muted ink at full strength, Delete in oxblood | Fixed (fix round) |
| 5 | New Chat replayed the whole welcome entrance (P1 motion) | The entrance is opt-in: motion.js marks the first load and clears the mark once it has played | Fixed (fix round and 0023) |
| 6 | About 30 kinds of text under 12px in windows (P2) | Labels and badges 12.5px, descriptions and empty states 14px upright muted ink | Fixed (fix round) |
| 7 | Message box focus was only a 1px darkening (P2) | A 2px ink edge | Fixed (fix round) |
| 8 | Search fields in windows showed upstream's pink magnifier (P2) | Redrawn in muted ink | Fixed (fix round) |
| 9 | Looping animations in Notes and the tour hints (P2) | No loops; tour hints play once | Fixed (fix round) |
| 10 | Sparkle by task names; ochre on decorative icons (P2) | Sparkle hidden; decorative icons in muted ink | Fixed (fix round) |
| 11 | Message box tool labels lost compared with Duet (P2) | Labels shown at 1180px and wider when no pane is docked | Fixed (fix round) |
| 12 | "Show all" in the sidebar was hidden under the Email row (P2) | Pinned at the foot of the list, with room before Email | Fixed (fix round) |
| 13 | Long model names cut off mid-letter in the 128px column (P2) | They wrap | Fixed (fix round) |
| 14 | Window headers 49px taller than Duet's; flat type steps inside (P2) | 65px header, 32px title, 19px section titles, 14px tabs | Fixed (fix round) |
| 15 | Press was slower than release (P2 motion) | Press 100ms, hover in 150ms, release 200ms | Fixed (fix round) |
| 16 | No view morphs (P2) | Chat title, window snap and list re-flow morphs; tab cross-fades in more windows | Fixed (fix round) |
| 17 | "01 / 02" section numbers in Settings implied an order (P3) | Removed | Fixed (fix round) |
| 18 | Settings labels misaligned; two filled buttons side by side (P3) | One 128px label column; secondary buttons ruled | Fixed (fix round) |
| 19 | Boxes inside windows (task rows, Calendar quick add) (P3) | Ruled rows; the extra frame removed | Fixed (fix round) |
| 20 | The looks stylesheet loads all three looks through serial imports (P3) | motion.js observers run only while Lichen is active | Partly fixed: the CSS is unchanged (see "Still open") |
| 21 | Data details: number columns, table cut-off at 320px, short model name, code strip in right-to-left pages (P3) | Model name shown in full, code strip stays left to right | Partly fixed: number alignment and table scroll cue are open |

## Beyond the first audit (patches 0023 and 0024)

* **The theme picker shows only the OpenWings looks**, Lichen first. Upstream's
  themes stay in the app for anyone who chose one before, and
  `WINGS_SHOW_CLASSIC_THEMES=1` lists them again.
* **No upstream font.** Each look sets the root font family, upstream's rules
  that name Fira Code or Inter directly are pointed at the look's own fonts,
  and the looks stylesheet now loads before the first layout. Upstream themes
  still get Fira Code.
* **No sea-themed busy graphics.** The whirlpool and sine-wave spinners and the
  ASCII waves (in the chat, compaction, Compare, Cookbook and the start-up
  loader) show the neutral three-dot mark under a look. Upstream themes keep
  upstream's spinners.
* **No upstream name or sea wording** in what people see or receive: the
  welcome, tours, calendar examples, Deep Research, email drafts and reminders,
  Cookbook, notifications, downloads (`openwings_backup_<time>.json`,
  `openwings-contacts.csv`), and what the model is told about itself (except
  the short prompts sent only to upstream's own odysseus-* fine-tuned models,
  which keep the wording those models were trained on). The
  built-in "Odysseus, king of Ithaca" persona is now "Strategist". Upstream's
  design test pages (a "ship on a wave" splash among them) are removed.
  Identifiers other software depends on stay as they are (mail and webhook
  header names, attachment references, Copilot headers, the companion's server
  name, `ODYSSEUS_*` settings).

## Second review

Two independent reviewers then went through the finished app (every window,
tab and Settings panel at desktop and phone widths, every theme, the other
pages) and the patches. Everything they found was fixed:

* **Web search page.** The results page a search link in chat opens was still
  upstream's black and red. It now wears the user's look like the other pages.
* **Selects.** Lichen's larger text was clipped in fixed-height selects
  (Compare, Agents > Group). Selects now grow with their text.
* **Leftover upstream colours.** Blind-mode orange, light-blue link hover,
  blue-grey search headers, a bright red "Delete everything", Dracula purple in
  Cookbook and upstream's calendar swatches now use each look's own inks and
  an 8-colour palette.
* **Contrast outside welcome and chat.** About 140 small labels failed AA
  because muted ink was faded a second time. They now use muted ink at full
  strength; switched-off Settings sections keep readable text and get a
  dashed edge instead of a fade.
* **Keyboard.** The theme swatches are a radio group: Tab lands on the current
  look, the arrow keys move and apply, and the focus ring shows.
* **Installed app.** The app manifest and the browser bar use Lichen's colour.
* **Duet and Hush on the own pages.** Live browser and Workspace files mixed
  look colours with the page's own light or dark text; both looks now colour
  them fully, by day and by night.
* **Small things.** The login footer shows the OpenWings version, keys show
  masked, `/favicon.ico` returns the OpenWings icon, a browser opening an
  unknown address gets an OpenWings "Page not found" page, "Remember me" is a
  labelled row, `/theme lichen` works, and task wording reads naturally
  ("Every 5 new chats").

## How the final state was checked

In the real app, on the new image, in a fresh browser with nothing saved:

* **Default look.** Login and app open in Lichen (page `rgb(199, 207, 193)`),
  also with a dark operating-system setting. motion.js is loaded (deferred) and
  active; the entrance plays six animations once; New Chat replays none.
* **Reduced motion.** No entrance animation plays, hover scale is 1.
* **Theme picker.** Lichen (active), Duet, Duet night, Hush, Hush night.
  A saved upstream theme still applies and stays listed while active; with the
  classic themes switched on, all 24 are listed.
* **Fonts.** No request for Fira Code or Inter while loading the login page and
  the app, opening all 13 Settings panels and nine tool windows.
* **Contrast** (`contrastscan.js`, WCAG formula, every visible text element):
  0 failures across 75 desktop views (welcome, chat, every tool window and
  its tabs, every Settings panel top and bottom, the slash menu) and 38 phone
  views. The own pages (Live browser, Workspace files, web search, sign-in
  result, not found) pass 4.5:1 under all five looks, with the system in
  light and in dark mode.
* **Wording.** A crawl of 70 surfaces (every sidebar window and its tabs, Chats,
  Email, every Settings panel, the welcome screen, a chat, the slash-command
  menu, menus, login, Live browser, Workspace files) found no "Odysseus" or sea
  word (voyage, sail, Ithaca, Penelope, crew, boat) in visible text, tooltips
  or labels, except in the raw server log under Settings > System (internal
  names, see below).
* **Chat.** A message sent from the message box gets the model's streamed
  reply; no page errors anywhere.

## Still open

* **CSS weight.** `skins.css` imports all three looks (about 310 KB) for every
  user, and it now holds the first paint (that is what keeps upstream fonts
  from loading). Loading only the active look's file would cut this; it needs
  the boot script and the looks test changed.
* **Numbers in tables** are left-aligned (detecting numeric columns needs a
  step in the markdown renderer), and wide tables on phones scroll sideways
  without a visual cue.
* **On purpose.** Calendar's "(Today)" marker is ochre as a state. Users'
  own calendar tag colours stay. With an upstream classic theme saved, the
  OpenWings pages fall back to a plain light or dark palette.
* **Spinners under reduced motion.** The stop button's loading spinners still
  turn, because they show progress.
* **Not re-scanned.** The Email window's less common views were not reached by
  the scripted text-size sweep, and the phone crawl could not open Settings or
  Files & Docs (both are covered by the desktop scan).
* **Internal names in the server log.** Settings > System shows the raw server
  log to admins; it contains internal names such as `odysseus_rag` (stored
  search collections) and `ODYSSEUS_*` setting names. Renaming them would
  orphan stored data or break existing configurations.
