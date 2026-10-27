from jarvis.contract.api import AppContext, Attachment, Result

NOTES: list[str] = []
SENT: list[tuple] = []
WIPED: list[str] = []
CONTEXT: str | Exception = ""


def context(ctx: AppContext) -> str:
    if isinstance(CONTEXT, Exception):
        raise CONTEXT
    return CONTEXT


def shout(ctx: AppContext, text: str) -> Result:
    return Result(text.upper())


def note(ctx: AppContext, text: str) -> Result:
    NOTES.append(text)
    return Result(f"Noted {text}", undo={"text": text})


def unnote(ctx: AppContext, undo: dict) -> Result:
    NOTES.remove(undo["text"])
    return Result("Un-noted")


def send(ctx: AppContext, text: str, to: str, times: int) -> Result:
    SENT.append((text, to, times))
    return Result(f"Sent {text} to {to}")


def wipe(ctx: AppContext, what: str) -> Result:
    WIPED.append(what)
    return Result(f"Wiped {what}")


def photo(ctx: AppContext) -> Result:
    path = ctx.temp_path(".jpg")
    path.write_bytes(b"jpeg")
    return Result("Took it", attachments=(Attachment("image", path, "image/jpeg"),))


def boom(ctx: AppContext) -> Result:
    raise RuntimeError("secret internal detail")


def project(ctx: AppContext, plan: str) -> Result:
    WIPED.append(f"project:{plan}")
    return Result("started")
