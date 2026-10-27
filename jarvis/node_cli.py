"""Node commands: pairing and listing (host side), `node run` (device side), and the --with-node dev child."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys

from jarvis.core import db, nodes
from jarvis.core.settings import Settings


def _pair(settings: Settings, name: str) -> int:
    conn = db.connect(settings)
    try:
        key = nodes.pair(conn, name)
    except nodes.PairingError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Paired {name}. Put this in the node's .env (FALAK_NODE_DIR/.env). It is shown only once:\n")
    print(f"FALAK_NODE_KEY={key}")
    return 0


def _revoke(settings: Settings, name: str) -> int:
    conn = db.connect(settings)
    try:
        nodes.revoke(conn, name)
    except nodes.PairingError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Revoked {name}. It is refused the next time it connects.")
    return 0


def _list(settings: Settings, connected_only: bool) -> int:
    conn = db.connect(settings)
    try:
        rows = nodes.list_nodes(conn)
    finally:
        conn.close()
    if connected_only:
        rows = [r for r in rows if r["connected_at"]]
        if not rows:
            print("No node is connected (as of the last time jarvis serve ran).")
    for r in rows:
        caps = ", ".join(r["capabilities"]) or "none"
        if connected_only:
            print(f"{r['name']}: capabilities {caps}, connected since {r['connected_at']}")
        else:
            state = "revoked" if r["revoked_at"] else "connected" if r["connected_at"] else "offline"
            print(f"{r['name']} [{state}] capabilities {caps}, last seen {r['last_seen'] or 'never'}")
    return 0


def _run() -> int:
    from jarvis.node.client import NodeClient
    from jarvis.node.config import NodeConfigError, load_config
    from jarvis.node.log import setup_logging
    from jarvis.node.runtime import NodeRuntime

    try:
        config = load_config()
    except NodeConfigError as e:
        print(f"node config error: {e}", file=sys.stderr)
        return 2
    setup_logging(config)
    if not config.key:
        print(f"No key. Run `jarvis node pair {config.name}` on the host and put FALAK_NODE_KEY in "
              f"{config.node_dir / '.env'}.", file=sys.stderr)
        return 1
    runtime = NodeRuntime(config.node_dir, config.tmp_dir)
    client = NodeClient(config, runtime)

    async def main() -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, client.stop)
        try:
            await client.run()
        finally:
            runtime.close()

    asyncio.run(main())
    return 0


def _music_login(read=input) -> int:
    """Sign Falak's music app in to Spotify (PKCE) and keep its own refresh token in the Keychain ("Falak Spotify").
    The old JARVIS's token is never touched, so its sign-in keeps working."""
    import secrets as pysecrets

    from jarvis.apps.music import spotify
    from jarvis.contract.envfile import load_secrets
    from jarvis.contract.loader import DEFAULT_APPS_DIR
    from jarvis.contract.manifest import load_manifest
    from jarvis.node.config import node_dir

    folder = node_dir()
    found = load_secrets(folder)
    if not found.get("SPOTIFY_CLIENT_ID"):
        print(f"Put SPOTIFY_CLIENT_ID in {folder / '.env'} first (from the Spotify developer dashboard).",
              file=sys.stderr)
        return 1
    redirect = load_manifest(DEFAULT_APPS_DIR / "music" / "manifest.toml").settings["redirect_uri"]["default"]
    acct = spotify.Account(found["SPOTIFY_CLIENT_ID"])
    verifier, state = spotify.new_verifier(), pysecrets.token_urlsafe(16)
    print("1. Open this address and approve Falak:\n")
    print(acct.login_url(verifier, state, redirect))
    print(f"\n2. Your browser then lands on {redirect}?code=... and shows an error page. That's expected.")
    try:
        landed = read("3. Paste that whole address here: ")
    except EOFError:
        return 1
    spotify.forget()
    try:
        saved = acct.finish_login(landed, verifier, state, redirect)
    except spotify.SpotifyError as e:
        print(f"Spotify sign-in failed: {e}", file=sys.stderr)
        return 1
    print("Signed in. The token is in the Keychain as \"Falak Spotify\"." if saved else
          "Signed in, but the Keychain wouldn't take the token; it only lasts until this command ends.")
    print("Restart `jarvis node run` so the music app picks it up.")
    return 0 if saved else 1


def command(settings: Settings, args) -> int:
    if args.group == "nodes":
        return _list(settings, connected_only=True)
    if args.node_command == "pair":
        return _pair(settings, args.name)
    if args.node_command == "revoke":
        return _revoke(settings, args.name)
    if args.node_command == "list":
        return _list(settings, connected_only=False)
    if args.node_command == "run":
        return _run()
    if args.node_command == "music-login":
        return _music_login()
    print("usage: jarvis node pair|revoke|list|run|music-login", file=sys.stderr)
    return 2


def start_child(settings: Settings, name: str = "mac") -> subprocess.Popen:
    """For `serve --with-node`: pair a node on the first run, then start it as a separate process."""
    from jarvis.node.config import load_config, write_pairing

    config = load_config()
    if not config.key:
        conn = db.connect(settings)
        try:
            try:
                nodes.revoke(conn, name)  # an earlier pairing whose key never reached this node folder
            except nodes.PairingError:
                pass
            key = nodes.pair(conn, name)
        finally:
            conn.close()
        write_pairing(config.node_dir, name, f"ws://{settings.node_listen}", key)
    return subprocess.Popen([sys.executable, "-m", "jarvis.cli", "node", "run"])


def stop_child(child: subprocess.Popen) -> None:
    child.terminate()
    try:
        child.wait(10)
    except subprocess.TimeoutExpired:
        child.kill()
