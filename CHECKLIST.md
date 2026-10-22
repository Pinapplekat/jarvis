# Manual checks

Things the tests can't reach (native UI, macOS permissions). Run them by hand after a change that touches them.

## JARVIS Camera (live preview)

Setup, once:
1. Optional but recommended, so rebuilds keep the Camera permission: in Keychain Access → Certificate Assistant → Create a Certificate…, name it `JARVIS Camera Signing`, Identity Type "Self Signed Root", Certificate Type "Code Signing". Then add `CAMERA_SIGN_IDENTITY=JARVIS Camera Signing` to `.env`.
2. `uv run jarvis schedule install` (builds `~/Applications/JARVIS Camera.app`).

Checks (from Telegram, the inbox or the HUD, with `AGENT_LOOP=1`):
1. **First permission prompt:** ask "what am I holding?". macOS asks whether **JARVIS Camera** may use the camera (not Terminal or Python). Allow it.
2. **Preview:** a small floating window appears at the top right with the live feed, for about 1.5 s. It doesn't take focus from what you're typing in.
3. **Flash:** the window flashes white once as the picture is taken.
4. **Frozen hold:** the frame stays frozen for about 2 s.
5. **Close:** the window closes and the app quits (it isn't left in the Dock or the menu bar, and the camera light goes off).
6. **Nothing kept:** `~/Library/Application Support/JARVIS/tmp/camera/` is empty after the reply; nothing new in Pictures or Downloads.
7. **Step trace:** in the inbox, the reply's camera row says "one picture taken and deleted (with the preview)". Unfolding it shows no description of what was seen.
8. **Fallback:** rename the app temporarily (or set `CAMERA_PREVIEW=0` and restart `jarvis listen`), ask again: no preview, the picture still works through imagesnap, and the camera row says the preview was skipped. Put it back.
9. **Denied:** turn JARVIS Camera off in System Settings → Privacy & Security → Camera and ask again: JARVIS says the camera isn't allowed; no window lingers. Turn it back on.

**If the permission prompt comes back after a rebuild:** the app's signature changed (ad-hoc signing changes with every build). Either set up the stable identity above and run `uv run jarvis schedule install` again, or just allow JARVIS Camera again in System Settings → Privacy & Security → Camera (remove the old entry with − if there are two). If it still asks every time, `tccutil reset Camera com.jarvis.camera` and allow it once more.

## Step trace (inbox)

1. Type "what's in my notes folder?" in the inbox. While it works, rows appear under the reply (Claude round, decision with p=…, gate, tool run) and fill in.
2. Click a row: it unfolds to Input and Output; click again to fold. Other replies' rows stay folded.
3. Ask for something that starts a task: a "waiting on helper" row appears; when the task finishes, its result row is added to that same reply.
4. Telegram shows the replies only, no trace.
