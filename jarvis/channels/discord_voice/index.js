'use strict';
// Falak's Discord voice add-on. Started and stopped by the host (channels/discord_call.py); it talks to the host over
// a local unix socket and does nothing else. It never writes audio to disk.
//
// Socket frames, both ways: one type byte (1 audio, 2 JSON line), a 4-byte big-endian length, the payload.
//   to the host:  audio = Elijah's voice, 16 kHz mono s16le, a 30 ms frame every 30 ms (zeros when he is silent, so
//                 the host's speech detector always sees time pass); JSON events: ready, owner_voice, joined, left,
//                 played, idle, error
//   from the host: audio = Falak's voice, 24 kHz mono s16le; JSON commands: join, leave, stop, end
// Only the owner's audio is ever subscribed to: nobody else in the channel is heard or transcribed.

const net = require('net');
const { Client, GatewayIntentBits, Events } = require('discord.js');
const {
  joinVoiceChannel, createAudioPlayer, createAudioResource, StreamType, EndBehaviorType,
  VoiceConnectionStatus, AudioPlayerStatus, NoSubscriberBehavior, entersState,
} = require('@discordjs/voice');
const OpusScript = require('opusscript');
const { PassThrough } = require('stream');

const TOKEN = process.env.DISCORD_BOT_TOKEN;
const OWNER = process.env.FALAK_OWNER_ID;
const SOCKET = process.env.FALAK_VOICE_SOCKET;
if (!TOKEN || !OWNER || !SOCKET) {
  console.error('DISCORD_BOT_TOKEN, FALAK_OWNER_ID and FALAK_VOICE_SOCKET are required');
  process.exit(2);
}

const AUDIO = 1, JSON_FRAME = 2;
const FRAME_SAMPLES = 480; // 30 ms at 16 kHz
const PACE_MS = 20;
const OUT_BYTES = 960 * 4; // 20 ms of 48 kHz stereo s16le

let sock = null;
function send(kind, payload) {
  if (!sock || sock.destroyed) return;
  const head = Buffer.alloc(5);
  head.writeUInt8(kind, 0);
  head.writeUInt32BE(payload.length, 1);
  sock.write(Buffer.concat([head, payload]));
}
const event = (obj) => send(JSON_FRAME, Buffer.from(JSON.stringify(obj) + '\n'));

// ---- hearing Elijah: opus (48 kHz stereo) -> 16 kHz mono, paced ---------------------------------------------------

const decoder = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
let heard = [];     // 16 kHz samples not yet sent
let listening = null;

function downmix(pcm48stereo) { // 3 stereo frames of 48 kHz -> 1 mono sample of 16 kHz (an average)
  const samples = new Int16Array(pcm48stereo.buffer, pcm48stereo.byteOffset, pcm48stereo.length / 2);
  const out = [];
  for (let i = 0; i + 5 < samples.length; i += 6) {
    out.push(Math.round((samples[i] + samples[i + 1] + samples[i + 2] + samples[i + 3] + samples[i + 4] + samples[i + 5]) / 6));
  }
  return out;
}

setInterval(() => { // every 30 ms, one frame to the host: his voice if there is any, else silence
  if (!listening) return;
  const frame = Buffer.alloc(FRAME_SAMPLES * 2);
  const take = heard.splice(0, FRAME_SAMPLES);
  take.forEach((s, i) => frame.writeInt16LE(s, i * 2));
  if (heard.length > 16000) heard = heard.slice(-3200); // never fall seconds behind
  send(AUDIO, frame);
}, 30);

function listenTo(connection) {
  const opus = connection.receiver.subscribe(OWNER, { end: { behavior: EndBehaviorType.Manual } });
  opus.on('data', (packet) => {
    try { heard.push(...downmix(decoder.decode(packet))); } catch (e) { /* a bad packet is just skipped */ }
  });
  listening = opus;
}

// ---- speaking: 24 kHz mono -> 48 kHz stereo, paced so the player never starves between sentences --------------------

const player = createAudioPlayer({ behaviors: { noSubscriber: NoSubscriberBehavior.Play } });
let queue = Buffer.alloc(0);
let stream = null;
let ending = false;
let playedBytes = 0;
let pacer = null;

function upsample(mono24) { // each 24 kHz sample becomes 2 stereo frames at 48 kHz
  const samples = new Int16Array(mono24.buffer, mono24.byteOffset, Math.floor(mono24.length / 2));
  const out = Buffer.alloc(samples.length * 8);
  samples.forEach((s, i) => { for (let k = 0; k < 4; k++) out.writeInt16LE(s, i * 8 + k * 2); });
  return out;
}

function startSpeaking() {
  if (stream) return;
  stream = new PassThrough();
  ending = false;
  playedBytes = 0;
  player.play(createAudioResource(stream, { inputType: StreamType.Raw }));
  pacer = setInterval(() => {
    if (queue.length >= OUT_BYTES) {
      stream.write(queue.subarray(0, OUT_BYTES));
      queue = queue.subarray(OUT_BYTES);
      playedBytes += OUT_BYTES;
    } else if (ending && queue.length === 0) {
      finishSpeaking();
    } else if (queue.length > 0 && ending) {
      stream.write(Buffer.concat([queue, Buffer.alloc(OUT_BYTES - queue.length)]));
      playedBytes += queue.length;
      queue = Buffer.alloc(0);
    } else {
      stream.write(Buffer.alloc(OUT_BYTES)); // starved between sentences: keep the player alive with silence
    }
  }, PACE_MS);
}

function playedMs() { return Math.round(playedBytes / 4 / 48); }

function finishSpeaking() {
  clearInterval(pacer);
  const s = stream;
  stream = null;
  if (s) s.end();
  event({ event: 'idle', ms: playedMs() });
}

function stopSpeaking() {
  clearInterval(pacer);
  queue = Buffer.alloc(0);
  const ms = playedMs();
  if (stream) { stream.destroy(); stream = null; }
  player.stop(true);
  event({ event: 'idle', ms });
}

setInterval(() => { if (stream) event({ event: 'played', ms: playedMs() }); }, 100);

// ---- Discord -----------------------------------------------------------------------------------------------------

const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates] });
let connection = null;

client.once(Events.ClientReady, () => event({ event: 'ready', user: client.user.tag }));
client.on(Events.VoiceStateUpdate, (before, after) => {
  if (after.id !== OWNER && before.id !== OWNER) return;
  event({ event: 'owner_voice', guild: after.guild.id, channel: after.channelId });
});

async function join(guildId, channelId) {
  if (connection) leave();
  const guild = await client.guilds.fetch(guildId);
  connection = joinVoiceChannel({ channelId, guildId, adapterCreator: guild.voiceAdapterCreator, selfDeaf: false, selfMute: false });
  await entersState(connection, VoiceConnectionStatus.Ready, 20_000);
  connection.subscribe(player);
  listenTo(connection);
  event({ event: 'joined', guild: guildId, channel: channelId });
}

function leave() {
  stopSpeaking();
  if (listening) { listening.destroy(); listening = null; }
  heard = [];
  if (connection) { connection.destroy(); connection = null; }
  event({ event: 'left' });
}

function command(cmd) {
  switch (cmd.cmd) {
    case 'join': join(cmd.guild, cmd.channel).catch((e) => event({ event: 'error', what: `couldn't join: ${e.message}` })); break;
    case 'leave': leave(); break;
    case 'stop': stopSpeaking(); break;
    case 'end': ending = true; break;
    default: break;
  }
}

// ---- the socket to the host -------------------------------------------------------------------------------------

let inbox = Buffer.alloc(0);
function onData(chunk) {
  inbox = Buffer.concat([inbox, chunk]);
  while (inbox.length >= 5) {
    const kind = inbox.readUInt8(0), length = inbox.readUInt32BE(1);
    if (inbox.length < 5 + length) return;
    const payload = inbox.subarray(5, 5 + length);
    inbox = inbox.subarray(5 + length);
    if (kind === AUDIO) { queue = Buffer.concat([queue, upsample(payload)]); startSpeaking(); }
    else if (kind === JSON_FRAME) { try { command(JSON.parse(payload.toString())); } catch (e) { event({ event: 'error', what: String(e) }); } }
  }
}

sock = net.createConnection(SOCKET);
sock.on('data', onData);
sock.on('error', (e) => { console.error(`host socket: ${e.message}`); process.exit(1); });
sock.on('close', () => process.exit(0)); // the host is gone: leave the call and quit
sock.on('connect', () => client.login(TOKEN).catch((e) => { event({ event: 'error', what: `login failed: ${e.message}` }); process.exit(1); }));
process.on('SIGTERM', () => { leave(); process.exit(0); });
