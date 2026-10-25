# Manual checks

Things the tests can't reach (native UI, macOS permissions). Run them by hand after a change that touches them.

## JARVIS Camera (live preview)

Setup, once:
1. Optional but recommended, so rebuilds keep the Camera permission: in Keychain Access → Certificate Assistant → Create a Certificate…, name it `JARVIS Camera Signing`, Identity Type "Self Signed Root", Certificate Type "Code Signing". Then add `CAMERA_SIGN_IDENTITY=JARVIS Camera Signing` to `.env`.
2. `uv run jarvis schedule install` (builds `~/Applications/JARVIS Camera.app`).

Checks (from Telegram, the inbox or the HUD, with `AGENT_LOOP=1`):
1. **First permission prompt:** ask "what am I holding?". macOS asks whether **JARVIS Camera** may use the camera (not Terminal or Python). Allow it.
2. **Preview:** a small floating window marked ● LIVE appears at the top right with the live feed, for about 1 s. It doesn't take focus from what you're typing in.
3. **Flash:** the window flashes white once as the picture is taken.
4. **Stays up:** the frame freezes for a moment, then the live feed comes back and the window stays. The reply says the preview is still up.
5. **Reuse:** ask "look at me" again: the same window takes a second picture (no second window).
6. **Close:** say "close the camera": the window closes and the app quits (not left in the Dock or the menu bar, camera light off). Ask again, then close it with its ✕; once more, click it and press Esc.
7. **Nothing kept:** `~/Library/Application Support/JARVIS/tmp/camera/` is empty after the reply; nothing new in Pictures or Downloads.
8. **Step trace:** in the inbox, the reply's camera row says "one picture taken and deleted (with the preview)". Unfolding it shows no description of what was seen.
9. **Fallback:** rename the app temporarily (or set `CAMERA_PREVIEW=0` and restart `jarvis listen`), ask again: no preview, the picture still works through imagesnap, the reply says the preview didn't open and why, and the camera row says it was skipped. Put it back.
10. **Denied:** turn JARVIS Camera off in System Settings → Privacy & Security → Camera and ask again: no window lingers, and JARVIS either says the camera isn't allowed or (if imagesnap has its own permission) says the preview didn't open and answers from an imagesnap picture. Turn it back on.

**If the permission prompt comes back after a rebuild:** the app's signature changed (ad-hoc signing changes with every build). Either set up the stable identity above and run `uv run jarvis schedule install` again, or just allow JARVIS Camera again in System Settings → Privacy & Security → Camera (remove the old entry with − if there are two). If it still asks every time, `tccutil reset Camera com.jarvis.camera` and allow it once more.

## Step trace (inbox)

1. Type "what's in my notes folder?" in the inbox. While it works, rows appear under the reply (Claude round, decision with p=…, gate, tool run) and fill in.
2. Click a row: it unfolds to Input and Output; click again to fold. Other replies' rows stay folded.
3. Ask for something that starts a task: a "waiting on helper" row appears; when the task finishes, its result row is added to that same reply.
4. Telegram shows the replies only, no trace.
