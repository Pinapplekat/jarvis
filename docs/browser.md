# Falak's browser (M10)

Falak has a real browser of its own on the Mac node: Chromium through Playwright, with a **profile that is Falak's, not
your Chrome**. It reads pages that need JavaScript, reads sites you signed in to (first of all Canvas), shows you a page in
a window, and fills in forms only after you say yes.

## Set up

```sh
falak browser setup     # installs Chromium into ~/Library/Application Support/Falak-node/browser/chromium
falak browser status    # whether Chromium is there, and which sites are signed in
```

`bin/falak` adds the `browser` extra (`uv sync --extra browser`, which installs Playwright), so `falak start` has it.
`setup` checks for 2 GB free first and puts everything under the node's folder (`FALAK_NODE_DIR`), about 570 MB.
With no Chromium installed the browser says "run `falak browser setup`" and `school` keeps using the calendar feed.

## Signing in

Say **"sign in to Canvas"** (any site works: "sign in to canvas.seattlecolleges.edu") when you're at the Mac. A visible
window opens at the site; sign in yourself, college single sign-on and the two-step code included. Falak never sees,
types or stores a password. Close the window, or say "I'm done" (it runs `close_window`); it closes by itself after 10
minutes. The site is then on the list in `browser/sites.json`, and only listed sites open with the profile.

- "Which sites am I signed in to?" lists them. "Forget Canvas" takes one off (undoable); its cookies stay in the profile
  until `falak browser reset`.
- `falak browser reset` deletes the whole profile and the list. It asks you to type `reset`; it is a command for you,
  not something Falak can do from chat.
- The profile folder is `browser/profile`, mode 0700. It is a live login: never in the repo, never logged, never sent
  anywhere, and the terminal app's secret guard denies reading it.

## What Falak can do

| You say | It runs | Notes |
|---|---|---|
| "read that page" (when `web__read_page` says it needs a browser) | `browser__read` | title, text (15,000 characters), 60 links, like `read_page`. A listed site opens signed in; any other opens in a fresh empty context |
| "send me a screenshot of it" | `read` with `screenshot`, then `falak__send_image` | the picture is in memory, goes to the model, and is deleted |
| "show me" | `browser__show` | the same page in a visible window (the headless context is closed and the profile reopened with a window, because Playwright can't make a headless one visible). Closes after 10 minutes or when you say so |
| "look around on that site" | `browser__browse_start`, `_look`, `_stop`, `follow` | following a link is a read; buttons and forms never happen here |
| "fill in the form" | `browser__act` | asks first, see below |

`web__read_page` stays the fast first try; Google Docs, Drive folders and wiki pages still use M9's plain views, which
need no browser.

### Acting on a page

`act` always asks, with the exact steps: *On https://example.com/form: fill "Your name" with "Elijah"; check "Send me a
copy"; press "Send". It's to ask about billing. Go?* The steps are strings in four forms, and the text you are asked
about is the text that runs:

```
fill "Field" with "text"      select "Field" as "Option"      check "Box"      press "Button"
```

- A field or button is found by the label or text visible on the page, matched exactly (case and spacing aside), never
  by a selector. Every step is checked against the page before anything runs. If one doesn't match, or matches two
  things, nothing runs and the answer lists what the page has.
- A `press` is always the last step: it can change the page, and later steps were never checked against what comes next.
- Never a password, a one-time code, a card number or a file field (by type, `autocomplete`, or label). Those are
  yours to fill in yourself, in a window from `show`.
- What was typed is not kept in the action log.
- When a page or an email steered the turn, even a learned "always allow" can't make `act` run on its own.

### Everything the browser reads is data

A page's words are never instructions. A turn that read a page asks before it sends data anywhere (a search, another
URL), as `web` does: `read`, `show`, `follow` and `sign_in` carry `sends_data`, because the address they open leaves the
Mac.

## What it will not open

Every request is checked, not only the address you gave: the page's scripts, images, frames and redirects too
(`page.route("**/*")`). Banking and payment sites, this Mac (`127.0.0.1`, `localhost`), the local network (`192.168.x.x`,
`10.x.x.x`), link-local and reserved addresses, and any name that *resolves* to one of those are aborted. The checks are
a port of `web`'s (apps can't import each other), and `test_browser_guard.py` fails if the two copies disagree.
Downloads, uploads and non-http(s) addresses are refused. Known limit: the browser resolves a name itself after the
check, so a server that changes its answer between the two lookups could get through; the sub-request check narrows
that but doesn't remove it.

## Canvas

`school` reads Canvas through the profile: `browser.fetch_json` is a GET with your cookies, listed sites only, and the
same API requests `/weekly` made.

- Assignments with submission status for every current course. These rows replace the calendar feed's copy of the same
  assignment, and the 3-hours-before "still unsubmitted" nudge works for them.
- Modules, for ECON's dates in the titles ("Module 2: Chapters 3 & 4 (DUE OCT 11)"). A title's date becomes a deadline
  unless an assignment of that class on that day already covers it.
- Announcements from the last 14 days. One that looks like it moves a date ("extended", "rescheduled", "now due"...) is
  kept and shown in your **next check-in** with its words. Falak asks before changing any deadline; nothing is applied
  silently.
- Signed out, or no node: the sync falls back to the calendar feed and says once on Discord, "Canvas wants you to sign
  in again; say 'sign in to Canvas' at your Mac." It says nothing more until Canvas has worked again.
- A `CANVAS_TOKEN`, if you ever have one, still wins.

## Memory and timing

Measured on the 8 GB M-series Mac this was built on, headless Chromium (the headless shell Playwright installs), a public
JavaScript-only page (`quotes.toscrape.com/js`) over home Wi-Fi, three runs:

| | |
|---|---|
| RAM of the browser (Chromium and Playwright's driver) after reading a page | 447 MB (475 MB with a screenshot) |
| RAM after the page is closed, browser still open | 345 MB |
| Cold: launch and read a JavaScript page | 2.2 s (6.8 s the very first time, with the disk cache cold) |
| Warm: read the same page again, with a screenshot | 2.0 s |
| Warm: read a plain page (`example.com`) | 0.7 s |

The browser closes after `idle_minutes` (default 5) without use, so the memory is held only while it is being used. The
node logs the figure after every read; `read` also returns it as `memory_mb`. Most of a JavaScript page's two seconds is
waiting for the network to go quiet (capped at `load_seconds`, default 15).

## Settings

`[apps.browser]` in `settings.toml`: `enabled`, `idle_minutes` (5), `load_seconds` (15), `blocklist` (extra domains that
are never opened).

## Tests

`uv run pytest` needs no Chromium, network or window: the browser sits behind a small interface and the tests use a
scripted fake. One opt-in test file drives real Chromium in a scratch profile:

```sh
FALAK_NODE_DIR=<scratch folder with browser/chromium from `falak browser setup`> FALAK_BROWSER_LIVE=1 \
  uv run pytest jarvis/apps/browser/tests/test_browser_live.py
```

It reads a JavaScript-only page, checks that a page asking for `127.0.0.1` and `192.168.1.1` is refused inside
Chromium, and fills, selects, checks and presses a form for real while refusing a password field.
