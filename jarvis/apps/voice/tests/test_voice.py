import json

from jarvis.core import actions

CLI = actions.Origin("cli")


def do(reg, action, **args):
    return actions.run(reg, f"voice__{action}", args, CLI)


def current(reg):
    out = do(reg, "current_voice")
    return out.result.data["engine"], out.result.data["profile"]


def test_it_starts_on_the_default_british_voice(real_apps):
    assert current(real_apps) == ("kokoro", {"voice": "bm_george", "speed": 1.0})


def test_set_voice_merges_answers_with_what_changed_and_undo_restores(real_apps):
    out = do(real_apps, "set_voice", attributes={"voice": "bm_george,am_onyx"})
    assert isinstance(out, actions.Ran) and "voice: bm_george to bm_george,am_onyx" in out.result.text
    assert do(real_apps, "set_voice", attributes={"speed": 0.9}).result.data["profile"] == {
        "voice": "bm_george,am_onyx", "speed": 0.9}  # only speed changed; the blend stays
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert current(real_apps)[1] == {"voice": "bm_george,am_onyx", "speed": 1.0}
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert current(real_apps)[1] == {"voice": "bm_george", "speed": 1.0}  # "undo that", twice, is the start again
    rows = real_apps.conn.execute("SELECT source FROM voice_profiles ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["set_voice", "set_voice", "undo", "undo"]  # history is kept


def test_something_the_engine_cannot_do_says_what_it_can(real_apps):
    out = do(real_apps, "set_voice", attributes={"accent": "french accent"})  # "give it a French accent"
    assert isinstance(out, actions.Failed) and "can't change accent" in out.message
    assert "bm_george" in out.message and "speed" in out.message
    assert "I don't have a voice called" in do(real_apps, "set_voice", attributes={"voice": "bm_nobody"}).message
    assert isinstance(do(real_apps, "set_voice", attributes={"speed": 3}), actions.Failed)
    assert "Say what to change" in do(real_apps, "set_voice", attributes={}).message
    assert current(real_apps)[1] == {"voice": "bm_george", "speed": 1.0}  # nothing was saved


def test_setting_the_same_thing_changes_nothing_and_is_not_undoable(real_apps):
    out = do(real_apps, "set_voice", attributes={"voice": "bm_george"})
    assert "Nothing changed" in out.result.text and out.result.undo is None
    assert real_apps.conn.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0] == 0


def test_omnivoice_takes_attributes_and_has_its_own_defaults(settings, data_dir):
    from jarvis.core.apps import load_apps

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.toml").write_text('[apps.voice]\nengine = "omnivoice"\n')
    from jarvis.core.settings import load_settings

    reg = load_apps(load_settings())
    try:
        assert current(reg) == ("omnivoice", {"gender": "male", "pitch": "low pitch", "accent": "british accent"})
        out = do(reg, "set_voice", attributes={"pitch": "very low pitch", "age": "middle-aged"})
        assert out.result.data["profile"]["age"] == "middle-aged"
        assert "can't change voice" in do(reg, "set_voice", attributes={"voice": "bm_lewis"}).message
        french = do(reg, "set_voice", attributes={"accent": "French accent"}).message
        assert "I don't have a French accent" in french and "british accent" in french
        from jarvis.speech import VoiceProfile

        assert VoiceProfile.from_dict(current(reg)[1]).instruct() == "male, middle-aged, very low pitch, british accent"
    finally:
        reg.close()


def test_a_profile_made_for_another_engine_is_not_used(real_apps):
    real_apps.conn.execute("INSERT INTO voice_profiles (created_at, engine, profile_json, source) VALUES "
                           "('2026-10-28T00:00:00+00:00', 'omnivoice', ?, 'set_voice')", (json.dumps({"age": "elderly"}),))
    assert current(real_apps) == ("kokoro", {"voice": "bm_george", "speed": 1.0})
