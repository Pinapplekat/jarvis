# Training "Hey Falak"

Until you do this, the desk listens for the pretrained **"hey jarvis"** model (a stand-in), and the push-to-talk key
(⌥Space) always works. Training takes about an hour on Colab and costs nothing. Nothing here touches the Mac.

openWakeWord trains a small model from **synthetic speech**: it makes thousands of fake recordings of the phrase with
text-to-speech, mixes in noise and room echo, and trains on them. You don't record anything.

## Steps

1. Open the openWakeWord training notebook, `automatic_model_training.ipynb`, from
   <https://github.com/dscripka/openWakeWord> (in `notebooks/`; the README links a "Open in Colab" button). Pick a GPU
   runtime (Runtime > Change runtime type > T4).
2. In the notebook's configuration cell set:
   - `target_phrase = ["hey falak"]`
   - `model_name = "hey_falak"`
   - leave the sample counts at their defaults for a first run (around 20 000 positive samples, 5 000 validation).
3. **Pronunciation.** The speech engine that makes the training clips reads text, so "falak" may come out wrong. Say
   the name out loud: *FAH-lahk*. If the first model misses you, retrain with respellings added to the phrase list,
   such as `["hey falak", "hey fah lock", "hey fall ack"]`, and keep the model that wakes most reliably for you.
4. Run all cells. It downloads about 2 GB of background audio and room-response data the first time, generates the
   clips, trains for roughly 30 to 60 minutes, and ends with a `hey_falak.onnx` file (the `.tflite` next to it isn't
   used here).
5. Download `hey_falak.onnx`.

## Put it on the Mac

Copy it into the node's folder, next to the other desk files:

```bash
cp ~/Downloads/hey_falak.onnx "$HOME/Library/Application Support/Falak-node/desk/hey_falak.onnx"
```

Restart `falak start`. The desk log (`Falak-node/logs`) says `listening for the wake word with hey_falak.onnx`. The
file is looked for in `FALAK_NODE_DIR/desk/`, which is `~/Library/Application Support/Falak-node/desk/` by default.

## Checking it

- Say "Hey Falak" at a normal distance from the desk, with music playing and without: you should hear the soft chime.
- Talk for a few minutes about something else, or leave a podcast on. It should stay quiet. The threshold is 0.5 in
  `jarvis/apps/desk/wake.py` (`THRESHOLD`): raise it a little if it wakes by accident, lower it if it misses you.
- If a model is too trigger-happy or too deaf, retrain with more negative examples: the notebook has a setting for the
  amount of adversarial text it generates.

## If you don't want a wake word

Set `wake_word = false` under `[apps.desk]` in `settings.toml`. Only the push-to-talk key then starts a conversation.
