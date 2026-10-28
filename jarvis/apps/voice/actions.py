import json
from datetime import datetime, timezone

from jarvis.contract.api import ActionError, AppContext, Result

DEFAULTS = {
    "kokoro": {"voice": "bm_george", "speed": 1.0},
    "omnivoice": {"gender": "male", "pitch": "low pitch", "accent": "british accent"},
    "say": {"voice": "Daniel", "speed": 1.0},
}
# What each engine can change. The manifest's enums already limit the values; this decides which attributes apply.
SUPPORTED = {
    "kokoro": ("voice", "speed"),
    "omnivoice": ("gender", "age", "pitch", "accent", "whisper", "speed"),
    "say": ("voice", "speed"),
}
KOKORO_VOICES = {
    "bm_george", "bm_lewis", "bm_daniel", "bm_fable", "bf_emma", "bf_isabella", "bf_alice", "bf_lily",
    "am_onyx", "am_michael", "am_adam", "am_eric", "am_liam", "am_echo", "am_fenrir", "am_puck",
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "af_nova", "af_kore", "af_alloy", "af_aoede",
    "af_jessica", "af_river",
}
ACCENTS = {"american accent", "australian accent", "british accent", "canadian accent", "indian accent",
           "chinese accent", "japanese accent", "korean accent", "portuguese accent", "russian accent"}
SAY_VOICES = {"Daniel", "Oliver", "Arthur", "Kate", "Serena", "Samantha", "Alex"}


def _engine(ctx: AppContext) -> str:
    return str(ctx.settings["engine"])


def _latest(ctx: AppContext) -> dict | None:
    row = ctx.db.execute("SELECT * FROM voice_profiles WHERE engine = ? ORDER BY id DESC LIMIT 1",
                         (_engine(ctx),)).fetchone()
    return dict(row) if row else None


def _current(ctx: AppContext) -> dict:
    row = _latest(ctx)
    return json.loads(row["profile_json"]) if row else dict(DEFAULTS[_engine(ctx)])


def _save(ctx: AppContext, profile: dict, source: str) -> None:
    ctx.db.execute("INSERT INTO voice_profiles (created_at, engine, profile_json, source) VALUES (?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(timespec="seconds"), _engine(ctx),
                    json.dumps(profile, sort_keys=True), source))


def _possible(engine: str) -> str:
    if engine == "omnivoice":
        return ("gender (male, female), age (child to elderly), pitch (very low to very high), an accent "
                f"({', '.join(sorted(ACCENTS))}), whisper, and speed")
    voices = SAY_VOICES if engine == "say" else KOKORO_VOICES
    return (f"the voice (one of {', '.join(sorted(voices))}; two joined by a comma blend them, so a deeper or warmer "
            "voice is a different pick or a blend) and the speed from 0.7 to 1.3")


def _check(engine: str, attributes: dict) -> None:
    unsupported = [k for k in attributes if k not in SUPPORTED[engine]]
    if unsupported:
        raise ActionError(f"The {engine} voice can't change {', '.join(unsupported)}. What I can change is: "
                          f"{_possible(engine)}.")
    if "accent" in attributes and attributes["accent"].strip().lower() not in ACCENTS:
        raise ActionError(f"I don't have a {attributes['accent']}. The accents I can do are: {', '.join(sorted(ACCENTS))}.")
    if "voice" in attributes:
        known = SAY_VOICES if engine == "say" else KOKORO_VOICES
        unknown = [v for v in str(attributes["voice"]).split(",") if v.strip() not in known]
        if unknown or len(str(attributes["voice"]).split(",")) > 2:
            raise ActionError(f"I don't have a voice called {attributes['voice']}. What I can change is: "
                              f"{_possible(engine)}.")


def _describe(before: dict, after: dict) -> str:
    changes = [f"{k}: {before.get(k, 'default')} to {v}" for k, v in sorted(after.items()) if before.get(k) != v]
    return "Changed " + "; ".join(changes) + "." if changes else "Nothing changed; it already sounds like that."


def set_voice(ctx: AppContext, attributes: dict) -> Result:
    engine = _engine(ctx)
    if not attributes:
        raise ActionError(f"Say what to change. I can change: {_possible(engine)}.")
    _check(engine, attributes)
    before = _current(ctx)
    after = {**before, **{k: (v.replace(" ", "") if k == "voice" else v.strip().lower() if k == "accent" else v)
                          for k, v in attributes.items()}}
    if after == before:
        return Result(_describe(before, after), data={"engine": engine, "profile": after})
    _save(ctx, after, "set_voice")
    return Result(_describe(before, after), data={"engine": engine, "profile": after}, undo={"previous": before},
                  subject="voice")


def undo_set_voice(ctx: AppContext, undo: dict) -> Result:
    _save(ctx, undo["previous"], "undo")
    return Result("Your voice is back the way it was.")


def current_voice(ctx: AppContext) -> Result:
    engine, profile = _engine(ctx), _current(ctx)
    return Result(f"The {engine} voice: " + ", ".join(f"{k} {v}" for k, v in sorted(profile.items())) + ".",
                  data={"engine": engine, "profile": profile})
