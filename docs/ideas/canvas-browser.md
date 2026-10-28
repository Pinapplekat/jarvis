# Idea: a Canvas browser for the school app

**Status:** moved into M10, Falak's browser (decided 2026-10-28); see `docs/briefs/M10.md`. Kept for the
background and the options that were rejected.

## The problem

- Seattle Colleges turned off personal Canvas access tokens ("Your Canvas administrators have chosen to limit
  your ability to generate your own access token"). `CANVAS_TOKEN` can't be made.
- That leaves the calendar feed (`CANVAS_ICS_URL`). It has Canvas due dates, but no submission status, and it
  misses most of the real work in two classes:
  - **ECON 100:** the deadlines are in module titles ("Module 2: Chapters 3 & 4 (DUE OCT 11)"), and extensions
    come in announcements.
  - **ENGL 101:** the work lives on the World-Building wiki, Discord and the weekly checklists, not on Canvas.
  - **CMST** also has Sunday homework behind a placeholder assignment.
- The `/weekly` skill reads the Canvas API anyway. It opens `canvas.seattlecolleges.edu/api/v1/...` in Elijah's
  signed-in Chrome, where the session cookie authorizes the requests, then pushes the deadlines in with
  `jarvis deadline import`.

## Ideas considered

- **Pasting the browser's `canvas_session` cookie into `.env`.** Works for `GET /api/v1/...`, but it's the
  whole login (it can submit work, send messages and change settings), it expires every so often, and each
  expiry means copying a new one by hand. Rejected.
- **Reading cookies out of Elijah's own Chrome.** That means Falak handling his college login. Rejected.

## The idea: Falak's own signed-in browser

1. **Sign in once, by hand.** A node app opens a real browser window (Playwright, persistent profile) with its
   own profile in the node's folder, not Elijah's Chrome. He signs in to Canvas the usual way, college SSO and
   2FA included. Falak never sees the password, only the profile.
2. **Read through that browser, with no window.** Every couple of hours the school sync runs it headless and
   makes the same requests `/weekly` makes:
   - assignments with submission status
   - modules (for ECON's dates in module titles)
   - announcements (for extensions)
   Requests are `GET` only, enforced in code.
3. **Most expirations fix themselves.** When the Canvas session lapses, Canvas redirects to the college SSO.
   That usually remembers the browser much longer than Canvas does, so the session renews without Elijah.
4. **When it really needs him, it says so.** If SSO or 2FA wants him, one Discord message ("Canvas wants you to
   sign in again"), the sign-in window opens next time he's at the Mac, and the sync falls back to the feed
   until then.

## Shape in Falak

- A node app (for example `canvas_browser`, `runs_on = "node:browser"`), because signing in needs a screen.
  - Actions: `sign_in` (opens the window), `fetch` (read-only API requests, returns JSON).
- The host's school app calls it during `school.sync`, falls back to the feed when it says "signed out", and
  keeps taking ENGL deadlines from `/weekly` through `deadline import`.
- The profile folder is a live login: owner-only permissions (0700), never in the repo, never sent anywhere,
  handled like the camera's temp files.

## Costs and unknowns

- New dependency: Playwright and its Chromium, about 150–300 MB on disk. It only runs for a few seconds per
  sync, which is fine on 8 GB of RAM.
- Unknown: how long the college SSO remembers the browser (days or months). Find out by running it.
- The college turned tokens off on purpose. Reading your own data through your own signed-in session is
  Elijah's call, but that is what this does.
