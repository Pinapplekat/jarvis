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
                         allowed_updates=["message", "callback_query"])
        out = []
        for u in raw:
            if "message" in u and "text" in u["message"]:
                m = u["message"]
                out.append(Update(u["update_id"], m["from"]["id"], _ts(m["date"]), text=m["text"]))
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
