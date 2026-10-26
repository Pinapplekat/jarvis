"""Phone calls through Twilio (voice only, so no A2P 10DLC registration): urgent calls to
Elijah, and pointing the number at the webhook he calls JARVIS on (jarvis/phone.py)."""

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

    def point_number_at(self, base_url: str) -> None:
        """Send calls to the Twilio number to JARVIS's webhook (and tell it when they end)."""
        r = self.http.get("/IncomingPhoneNumbers.json", params={"PhoneNumber": config.TWILIO_FROM_NUMBER})
        found = r.json().get("incoming_phone_numbers", []) if r.is_success else []
        if not found:
            raise RuntimeError(f"couldn't find {config.TWILIO_FROM_NUMBER} in this Twilio account ({r.status_code})")
        r = self.http.post(f"/IncomingPhoneNumbers/{found[0]['sid']}.json", data={
            "VoiceUrl": f"{base_url}/voice/incoming", "VoiceMethod": "POST",
            "StatusCallback": f"{base_url}/voice/status", "StatusCallbackMethod": "POST",
        })
        if not r.is_success:
            raise RuntimeError(f"Twilio update failed ({r.status_code}): {r.text[:300]}")


def twilio_configured() -> bool:
    return all([config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN,
                config.TWILIO_FROM_NUMBER, config.MY_PHONE_NUMBER])
