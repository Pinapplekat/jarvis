import pytest

from jarvis import browser, config, introspect, terminal, web


@pytest.fixture
def sites(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "SITES", tmp_path / "browser-sites.json")
    monkeypatch.setattr(browser, "LOCK", tmp_path / "browser.lock")
    return tmp_path


@pytest.mark.parametrize("url", ["https://www.chase.com/login", "https://paypal.com", "https://online.becu.org/x",
                                 "https://bank.example.com", "https://venmo.com/u/x"])
def test_banking_and_payment_sites_are_blocked_everywhere(sites, url):
    assert web.blocked_site(url)
    shot = browser.browse(url, screenshot=True)  # refused before any browser starts
    assert shot.page.error and shot.png is None
    assert web.fetch_page(url).error
    with pytest.raises(RuntimeError):
        browser.login(url)


def test_extra_blocked_domains_from_settings(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_BLOCKLIST", "mybroker.com, other.org")
    assert web.blocked_site("https://app.mybroker.com/") and not web.blocked_site("https://docs.python.org")


def test_private_network_is_still_blocked(sites):
    assert browser.browse("http://127.0.0.1:8787/").page.error


def test_only_sites_he_logged_in_to_use_the_profile(sites):
    browser._save_sites(["canvas.seattlecolleges.edu"])
    assert browser.allowed("https://canvas.seattlecolleges.edu/courses/1")
    assert browser.allowed("https://sub.canvas.seattlecolleges.edu/")
    assert not browser.allowed("https://evilcanvas.seattlecolleges.edu.attacker.com/")
    assert not browser.allowed("https://google.com")
    assert browser.forget_site("canvas.seattlecolleges.edu") and browser.sites() == []


def test_the_profile_is_a_secret_to_commands_and_tasks():
    profile = config.BROWSER_PROFILE
    assert introspect.is_secret_path(profile / "Default" / "Cookies")
    import shlex
    assert terminal.classify(f"ls {shlex.quote(str(profile))}").kind == "reject"
    assert terminal.classify(["cat", str(profile / "Default" / "Cookies")]).kind == "reject"
    assert str(profile) in introspect.SANDBOX  # the OS-level guard denies reading it too


def test_profile_lives_outside_the_repo_and_is_ignored():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    assert not config.BROWSER_PROFILE.resolve().is_relative_to(repo)
    assert "browser-profile/" in (repo / ".gitignore").read_text()


def test_chat_browse_returns_text_and_shares_a_screenshot(sites):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from jarvis.chat import respond
    from jarvis.memory import Memory
    rounds = [{"reply": "checking", "actions": [{"type": "browse", "url": "https://example.org", "screenshot": True}]},
              {"reply": "It says hello, sir.", "actions": []}]
    prompts, photos = [], []

    def run(system, prompt, on_text=None):
        prompts.append(prompt)
        return rounds[min(len(prompts), 2) - 1]

    def fake_browse(url, screenshot=False):
        return browser.Shot(web.Page(url, title="Example", text="hello there"), b"PNG" if screenshot else None)
    reply = respond(Memory(":memory:"), "what's on example.org?", datetime.now(ZoneInfo("UTC")), "", runner=run,
                    browser=fake_browse, send_photo=lambda png, caption: photos.append((png, caption)))
    assert reply == "It says hello, sir." and photos == [(b"PNG", "Example")]
    assert "hello there" in prompts[1] and "Untrusted page content" in prompts[1]
