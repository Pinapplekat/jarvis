import msal
import httpx

from jarvis import config

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["User.Read", "Mail.ReadWrite", "Mail.Send", "Calendars.Read"]

# Entra error codes worth translating for a student account
KNOWN_ERRORS = {
    "AADSTS65001": "Consent not granted. Your school may require admin approval for this app.",
    "AADSTS90094": "Admin consent required: the school tenant blocks users from consenting to apps.",
    "AADSTS900941": "Admin consent required: the school tenant blocks users from consenting to apps.",
    "AADSTS50020": "Account isn't in the app's tenant. Set the app to multi-tenant and GRAPH_TENANT=organizations.",
    "AADSTS50105":"Your account isn't assigned to this app in the school tenant.",
    "AADSTS53003": "Blocked by a Conditional Access policy (device or location rules).",
    "AADSTS700016": "App ID not found. Check GRAPH_CLIENT_ID and that the app is multi-tenant.",
    "AADSTS7000218": "Public client flows are off. Enable 'Allow public client flows' on the app.",
}


def explain(error_text: str) -> str:
    for code, msg in KNOWN_ERRORS.items():
        if code in error_text:
            return f"{code}: {msg}"
    return error_text


class GraphClient:
    def __init__(self, client_id: str = config.GRAPH_CLIENT_ID, tenant: str = config.GRAPH_TENANT):
        if not client_id:
            raise SystemExit("GRAPH_CLIENT_ID is not set. Copy .env.example to .env and fill it in.")
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.cache = msal.SerializableTokenCache()
        if config.TOKEN_CACHE_PATH.exists():
            self.cache.deserialize(config.TOKEN_CACHE_PATH.read_text())
        self.app = msal.PublicClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant}",
            token_cache=self.cache,
        )

    def _save_cache(self) -> None:
        if self.cache.has_state_changed:
            config.TOKEN_CACHE_PATH.write_text(self.cache.serialize())
            config.TOKEN_CACHE_PATH.chmod(0o600)

    def token(self) -> str:
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        if not result:
            flow = self.app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(explain(str(flow)))
            print(flow["message"], flush=True)
            result = self.app.acquire_token_by_device_flow(flow)
        self._save_cache()
        if "access_token" not in result:
            raise RuntimeError(explain(result.get("error_description", str(result))))
        return result["access_token"]

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token()}"}
        return httpx.request(method, f"{GRAPH}{path}", headers=headers, timeout=30, **kwargs)

    def send_mail(self, to: list[str], subject: str, body: str) -> None:
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        }
        r = self.request("POST", "/me/sendMail", json={"message": message})
        if r.status_code != 202:
            raise RuntimeError(f"sendMail failed ({r.status_code}): {r.text}")


def run_access_test(send: bool = False) -> bool:
    """Walk through each capability JARVIS needs and report which ones the school allows."""
    client = GraphClient()
    print("Signing in...")
    try:
        client.token()
    except RuntimeError as e:
        print(f"FAIL sign-in: {e}")
        return False
    print("PASS sign-in")

    me = client.request("GET", "/me").json()
    address = me.get("mail") or me.get("userPrincipalName")
    print(f"PASS profile: {me.get('displayName')} <{address}>")

    checks = [
        ("read inbox", "GET", "/me/mailFolders/inbox/messages?$top=1&$select=subject", None),
        ("read calendar", "GET", "/me/calendarView?startDateTime=2026-01-01T00:00:00Z"
         "&endDateTime=2026-12-31T00:00:00Z&$top=1&$select=subject", None),
        ("create draft", "POST", "/me/messages", {
            "subject": "JARVIS access test (safe to delete)",
            "body": {"contentType": "Text", "content": "Draft created by the JARVIS Graph test."},
            "toRecipients": [{"emailAddress": {"address": address}}],
        }),
    ]
    ok = True
    for name, method, path, body in checks:
        r = client.request(method, path, json=body)
        if r.is_success:
            print(f"PASS {name}")
            if name == "create draft":
                client.request("DELETE", f"/me/messages/{r.json()['id']}")
        else:
            ok = False
            print(f"FAIL {name} ({r.status_code}): {r.text[:300]}")

    if send:
        try:
            client.send_mail([address], "JARVIS send test", "If you're reading this, sending works.")
            print(f"PASS send (check {address})")
        except RuntimeError as e:
            ok = False
            print(f"FAIL send: {e}")
    else:
        print("SKIP send (rerun with --send to email yourself a test message)")
    return ok
