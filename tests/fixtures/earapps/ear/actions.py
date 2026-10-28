from jarvis.contract.api import Result
from jarvis.protocol import messages as m

FRAMES, CTX = [], []


def start(ctx):
    CTX[:] = [ctx]
    ctx.on_host_frame(FRAMES.append)


def send(ctx, kind):
    frame = m.VoiceStart(5, "push").to_json() if kind == "json" else m.AudioFrame(5, 1, m.MIC, b"\x01\x02").to_bytes()
    ctx.send_to_host(frame)
    return Result("sent")


def heard(ctx):
    return Result(str(len(FRAMES)), data={"frames": list(FRAMES)})
