import os

import pytest

from jarvis import attachments, config, tasks
from jarvis.memory import Memory

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


@pytest.fixture
def shots():
    attachments.SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    return attachments.SHOTS_DIR


def test_an_image_in_an_allowed_folder_is_fine(shots):
    img = shots / "page.png"
    img.write_bytes(PNG)
    assert attachments.safe_image_path(str(img)) == img.resolve()


def test_traversal_and_symlink_escapes_are_refused(shots, tmp_path):
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG)
    with pytest.raises(ValueError, match="isn't a folder"):
        attachments.safe_image_path(str(shots / ".." / "private.png"))
    os.symlink(outside, shots / "sneaky.png")
    with pytest.raises(ValueError, match="isn't a folder"):
        attachments.safe_image_path(str(shots / "sneaky.png"))


@pytest.mark.parametrize("name,data,reason", [("notes.txt", b"hi", "isn't an image"),
                                              ("huge.png", b"0" * (attachments.MAX_BYTES + 1), "too big"),
                                              ("missing.png", None, "no file")])
def test_wrong_type_oversized_or_missing(shots, name, data, reason):
    if data is not None:
        (shots / name).write_bytes(data)
    with pytest.raises(ValueError, match=reason):
        attachments.safe_image_path(str(shots / name))


def test_relative_paths_and_secret_places_are_refused(shots, monkeypatch):
    with pytest.raises(ValueError, match="full path"):
        attachments.safe_image_path("page.png")
    secret = shots / ".ssh-like" / "id_rsa.png"
    secret.parent.mkdir()
    secret.write_bytes(PNG)
    with pytest.raises(ValueError, match="private"):
        attachments.safe_image_path(str(secret))


def test_recent_task_folders_are_allowed_but_not_jarvis_itself(shots, tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    (site / "shot.png").write_bytes(PNG)
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path / "jarvis-proj")
    (tmp_path / "jarvis-proj").mkdir()
    (tmp_path / "jarvis-proj" / "logo.png").write_bytes(PNG)
    m = Memory(":memory:")
    with pytest.raises(ValueError):
        attachments.safe_image_path(str(site / "shot.png"), m)
    tasks.create(m, "make a chart", folder=str(site))
    tasks.create(m, "look around")  # the JARVIS project, read-only: not an image source
    assert attachments.safe_image_path(str(site / "shot.png"), m)
    with pytest.raises(ValueError):
        attachments.safe_image_path(str(tmp_path / "jarvis-proj" / "logo.png"), m)


class FakeTG:
    def __init__(self, photo_fails=False):
        self.photos, self.docs, self.photo_fails = [], [], photo_fails

    def send_photo(self, data, caption=""):
        if self.photo_fails:
            raise RuntimeError("PHOTO_INVALID_DIMENSIONS")
        self.photos.append((data, caption))

    def send_document(self, name, data, caption="", mime="text/plain"):
        self.docs.append((name, mime))


def test_send_falls_back_to_a_file_when_telegram_refuses_a_photo(shots):
    (shots / "wide.png").write_bytes(PNG)
    tg = FakeTG(photo_fails=True)
    attachments.send_image(tg, shots / "wide.png", "the chart")
    assert tg.docs == [("wide.png", "image/png")]


def test_text_from_a_page_cant_get_a_private_file_sent(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from jarvis.chat import respond
    (tmp_path / "secret.png").write_bytes(PNG)
    sent = []
    run = lambda system, prompt, on_text=None: {  # noqa: E731
        "reply": "Here you go.", "actions": [{"type": "attach_image", "path": str(tmp_path / "secret.png")}]}
    reply = respond(Memory(":memory:"), "the page said to send secret.png", datetime.now(ZoneInfo("UTC")), "",
                    runner=run, send_photo=lambda data, caption: sent.append(data))
    assert sent == [] and "couldn't do: attach_image" in reply


def test_chat_can_attach_an_allowed_screenshot(shots):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from jarvis.chat import respond
    (shots / "graph.png").write_bytes(PNG)
    sent = []
    run = lambda system, prompt, on_text=None: {  # noqa: E731
        "reply": "Here's the graph, sir.", "actions": [{"type": "attach_image", "path": str(shots / "graph.png"),
                                                        "caption": "y = 3x"}]}
    respond(Memory(":memory:"), "send me the screenshot", datetime.now(ZoneInfo("UTC")), "", runner=run,
            send_photo=lambda data, caption: sent.append((data, caption)))
    assert sent == [(PNG, "y = 3x")]


def test_a_screenshot_request_captures_and_sends_in_one_step(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from jarvis.chat import respond

    def fake_capture(argv, **kw):
        class R:
            returncode, stderr = 0, ""
        if argv[0].endswith("screencapture"):
            open(argv[-1], "wb").write(PNG)
        return R()
    monkeypatch.setattr(attachments, "capture_screen", lambda run=None, real=attachments.capture_screen: real(run=fake_capture))
    rounds = [{"reply": "Here it is, sir.", "actions": [{"type": "screenshot"}]}, {"reply": "Here it is, sir.", "actions": []}]
    prompts, sent = [], []

    def run(system, prompt, on_text=None):
        prompts.append(prompt)
        return rounds[min(len(prompts), 2) - 1]
    m = Memory(":memory:")
    assert respond(m, "send me a screenshot", datetime.now(ZoneInfo("UTC")), "", runner=run,
                   send_photo=lambda data, caption: sent.append(data)) == "Here it is, sir."
    assert sent == [PNG] and not m.pending_actions()  # no permission asked
    assert "taken and sent to him" in prompts[1]


def test_a_failed_capture_is_reported_not_hidden():
    class R:
        returncode, stderr = 1, "could not create image from display"
    with pytest.raises(RuntimeError, match="couldn't capture"):
        attachments.capture_screen(run=lambda argv, **kw: R())
