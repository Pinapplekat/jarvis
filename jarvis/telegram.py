"""Minimal Telegram Bot API client. Polls for updates, so no public webhook is needed."""

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from jarvis import config

MAX_TEXT = 4000  # Telegram's limit is 4096


@dataclass
class Update:
    update_id: int
    sender_id: int
    sent_at: datetime
    text: str | None = None            # a typed message
    callback_id: str | None = None     # a button press...
    callback_data: str | None = None
    message_id: int | None = None      # ...on this message
    message_text: str | None = None
    voice_file_id: str | None = None   # a voice note (transcribed into `text` before handling)
    via_voice: bool = False
    location: dict | None = None       # a shared location or live-location update (jarvis.location point)


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str = config.TELEGRAM_BOT_TOKEN, chat_id: str = config.TELEGRAM_CHAT_ID):
        if not token:
            raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")
        self._base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.http = httpx.Client(timeout=httpx.Timeout(30, read=75))  # read > longest long-poll

    def _call(self, method: str, **payload) -> dict | list:
        # Errors are rebuilt without the request URL: it contains the bot token.
        try:
            r = self.http.post(f"{self._base}/{method}", json=payload)
        except httpx.HTTPError as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not body.get("ok"):
            raise TelegramError(f"{method} failed ({r.status_code}): {body.get('description', '')}")
        return body["result"]

    def me(self) -> dict:
        return self._call("getMe")

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> int:
        payload = {"chat_id": self.chat_id, "text": text[:MAX_TEXT]}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]
            }
        return self._call("sendMessage", **payload)["message_id"]

    def send_document(self, filename: str, data: bytes, caption: str = "", mime: str = "text/plain") -> None:
        try:
            r = self.http.post(f"{self._base}/sendDocument",
                               data={"chat_id": self.chat_id, "caption": caption[:1000]},
                               files={"document": (filename, data, mime)})
        except httpx.HTTPError as e:
            raise TelegramError(f"sendDocument: {type(e).__name__}") from None
        if not r.json().get("ok"):
            raise TelegramError(f"sendDocument failed ({r.status_code}): {r.json().get('description', '')}")

    def send_photo(self, png: bytes, caption: str = "") -> None:
        try:
            r = self.http.post(f"{self._base}/sendPhoto",
                               data={"chat_id": self.chat_id, "caption": caption[:1000]},
                               files={"photo": ("page.png", png, "image/png")})
        except httpx.HTTPError as e:
            raise TelegramError(f"sendPhoto: {type(e).__name__}") from None
        if not r.json().get("ok"):
            raise TelegramError(f"sendPhoto failed ({r.status_code}): {r.json().get('description', '')}")

    def download(self, file_id: str) -> bytes:
        path = self._call("getFile", file_id=file_id)["file_path"]
        try:  # the file URL contains the bot token, so errors are rebuilt without it
            r = self.http.get(f"{self._base.replace('/bot', '/file/bot', 1)}/{path}")
        except httpx.HTTPError as e:
            raise TelegramError(f"download: {type(e).__name__}") from None
        if r.status_code != 200:
            raise TelegramError(f"download failed ({r.status_code})")
        return r.content

    def send_voice(self, ogg: bytes, caption: str = "") -> None:
        try:
            r = self.http.post(f"{self._base}/sendVoice",
                               data={"chat_id": self.chat_id, "caption": caption[:1000]},
                               files={"voice": ("jarvis.ogg", ogg, "audio/ogg")})
        except httpx.HTTPError as e:
            raise TelegramError(f"sendVoice: {type(e).__name__}") from None
        if not r.json().get("ok"):
            raise TelegramError(f"sendVoice failed ({r.status_code}): {r.json().get('description', '')}")

    def action(self, kind: str) -> None:
        """Chat status like 'typing' or 'record_voice'."""
        self._call("sendChatAction", chat_id=self.chat_id, action=kind)

    def edit(self, message_id: int, text: str) -> None:
        """Replace a message's text (and drop its buttons)."""
        self._call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text[:MAX_TEXT])

    def answer(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def typing(self) -> None:
        self._call("sendChatAction", chat_id=self.chat_id, action="typing")

    def updates(self, offset: int | None, wait: int = 0) -> list[Update]:
        """wait > 0 long-polls: Telegram holds the request until a message arrives or wait seconds pass."""
        raw = self._call("getUpdates", offset=offset, timeout=wait,
                         allowed_updates=["message", "edited_message", "callback_query"])
        out = []
        for u in raw:
            edited = "edited_message" in u
            m = u.get("message") or u.get("edited_message") or {}
            if "location" in m:  # a share, or (edited) each update of a live share
                from jarvis.location import from_message
                if (point := from_message(m, edited)):
                    out.append(Update(u["update_id"], (m.get("from") or {}).get("id", 0), _ts(m["date"]),
                                      location=point))
                    continue
            if edited:
                out.append(Update(u["update_id"], 0, _ts(0)))  # edited text isn't re-answered
            elif "message" in u and "text" in u["message"]:
                m = u["message"]
                out.append(Update(u["update_id"], m["from"]["id"], _ts(m["date"]), text=m["text"]))
            elif "message" in u and "voice" in u["message"]:
                m = u["message"]
                out.append(Update(u["update_id"], m["from"]["id"], _ts(m["date"]),
                                  voice_file_id=m["voice"]["file_id"]))
            elif "callback_query" in u:
                q = u["callback_query"]
                msg = q.get("message") or {}
                out.append(Update(
                    u["update_id"], q["from"]["id"], _ts(msg.get("date", 0)),
                    callback_id=q["id"], callback_data=q.get("data"),
                    message_id=msg.get("message_id"), message_text=msg.get("text"),
                ))
            else:
                out.append(Update(u["update_id"], 0, _ts(0)))  # ignored, but advances the offset
        return out


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)
