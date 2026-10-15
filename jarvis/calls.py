"""Urgent phone calls through Twilio (voice only, so no A2P 10DLC registration)."""

from xml.sax.saxutils import escape

import httpx

from jarvis import config

API = "https://api.twilio.com/2010-04-01"


def call_twiml(message: str) -> str:
    said = escape(f"This is JARVIS with an urgent alert. {message}")
    return (
        f'<Response><Pause length="1"/><Say voice="Polly.Matthew">{said}</Say>'
        f'<Pause length="1"/><Say voice="Polly.Matthew">Again. {escape(message)}</Say></Response>'
    )


class TwilioCaller:
    def __init__(self):
        missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                               "MY_PHONE_NUMBER") if not getattr(config, k)]
        if missing:
            raise SystemExit(f"Set {', '.join(missing)} in .env")
        self.http = httpx.Client(
            base_url=f"{API}/Accounts/{config.TWILIO_ACCOUNT_SID}",
            auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
            timeout=30,
        )

    def call(self, message: str) -> None:
        r = self.http.post("/Calls.json", data={
            "From": config.TWILIO_FROM_NUMBER, "To": config.MY_PHONE_NUMBER,
            "Twiml": call_twiml(message),
        })
        if not r.is_success:
            raise RuntimeError(f"Twilio call failed ({r.status_code}): {r.text[:300]}")


def twilio_configured() -> bool:
    return all([config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN,
                config.TWILIO_FROM_NUMBER, config.MY_PHONE_NUMBER])
