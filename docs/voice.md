# Voice: setting it up

Falak talks at the desk (the Mac's microphone and speakers) and on a Discord call. The host does the listening and the
speaking; the node (`Falak Ears.app`) does the microphone, speakers, echo cancellation, wake word and push-to-talk key.
The measurements behind these choices are in [voice-measurements.md](voice-measurements.md).

## At the desk

1. **Install the extras.** The host needs the speech engines, the node needs the wake word:
   ```bash
   uv sync --extra dev --extra voice --extra desk
   ```
2. **Install espeak-ng** so names and rare words are spoken instead of skipped (Emily, Spotify, Falak):
   ```bash
   brew install espeak-ng
   ```
3. **Get the models** into Falak's own folder (copies Whisper from the old JARVIS folder, read only, and downloads
   Kokoro, about 330 MB; it stops if that would leave under 2 GiB free):
   ```bash
   jarvis voice setup
   jarvis voice status
   ```
4. **Start Falak** (`falak start`). The node builds and signs `Falak Ears.app` the first time (it needs Xcode's command
   line tools) under `~/Library/Application Support/Falak-node/desk/`, downloads the wake-word models there, and opens
   the app.
5. **Allow the Microphone for Falak Ears.** macOS asks the first time; if you said no, System Settings > Privacy &
   Security > Microphone. Without a signing identity macOS asks again after each rebuild: make a code-signing
   certificate in Keychain Access, then set `signing_identity = "Its Name"` under `[apps.desk]`.
6. Say **"Hey Falak"** (until you train it, "hey jarvis" works as a stand-in: [wake-word.md](wake-word.md)), or press
   **⌥Space**: hold to talk, or tap to start a conversation. After Falak answers you have 15 seconds to reply without the
   wake word. Talk over it and it stops.

Settings (all optional, under `[apps.desk]`): `push_to_talk`, `wake_word`, `end_silence_ms`, `barge_in_ms`,
`follow_up_seconds`, `chimes`, `signing_identity`. Under `[channels.desk]`: `voice_model` (default `haiku`) and
`voice_max_turns` (default 6). Under `[apps.voice]`: `engine` (`kokoro`, `omnivoice` or `say`) and `idle_minutes`.

Change how it sounds by asking: "make your voice deeper and a bit older", then "undo that".

From Discord, "say good morning out loud" speaks at the desk (the speakers or your headphones), as long as the Mac's
node is connected.

## On a Discord call

Off until you turn it on. Falak joins the voice channel when you do; "call me" sends you a link, because bots can't ring.

1. **Node 22.12 or newer** (this Mac has 20): `brew install node`, then
   `cd jarvis/channels/discord_voice && npm install`.
2. **The speech detector** for calls: `jarvis voice setup` downloads it (2 MB).
3. **In the Discord developer portal**, for the bot you already use: the privileged intents aren't needed; the
   add-on uses the `Guilds` and `GuildVoiceStates` intents, which are on by default. Invite the bot to your private
   server with the **Connect** and **Speak** permissions on the voice channel (edit the channel's permissions for the
   bot role if it was invited without them). Falak doesn't touch your Discord settings.
4. **Turn on Developer Mode** (Discord settings > Advanced), then right-click the server and the voice channel and
   *Copy ID*.
5. **settings.toml**:
   ```toml
   [channels.discord_call]
   enabled = true
   guild_id = 123456789012345678
   voice_channel_id = 123456789012345678   # optional; leave out to join whichever channel you join on that server
   ```
   `[channels.discord]` needs `owner_id` and `.env` needs `DISCORD_BOT_TOKEN`, as for text.

Only your audio is transcribed; nobody else in the channel is heard. Discord cancels echo itself, so interruptions on a
call come from the speech detector alone.
