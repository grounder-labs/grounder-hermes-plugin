# Copyright 2026 Grounder Labs, Inc
# SPDX-License-Identifier: Apache-2.0

"""Grounder direct: ``/search dev`` and the hub's first row turn it on, on this Mac only; any other pick turns it off. While it is on, ``web_search``,
``web_extract`` and ``grounder_search`` reach no Grounder API and no search provider: they read only the addresses given, each straight from its site, and a
page that cannot be read says why with nothing tried in its place (no ``error`` for Hermes's own reader or keyless ring to act on).

The reads themselves are faked here, except in the one live test, which runs only with GROUNDER_DIRECT_LIVE=1 (it reads reddit.com from this Mac).
"""

import json
import os

import pytest
import yaml
from test_commands import Host

PAGE = "https://www.reddit.com/r/MechanicalKeyboards/"


def _dev(home):
    """The saved setting, as the dev builds wrote it: True by itself, "addon" as the add-on, False off."""
    path = home / "config.yaml"
    web = (yaml.safe_load(path.read_text()) or {}).get("web", {}) if path.exists() else {}
    section = web.get("grounder") or {}
    if "direct" in section:
        return {"only": True, "with-search": "addon", "off": False}[section["direct"]]
    return section.get("direct_dev")


@pytest.fixture
def dev_on(plugin, tmp_path):
    plugin.direct_dev.set_on(True)
    return tmp_path


def test_a_search_reads_only_the_address_it_names_straight_from_the_site(plugin, stub, hermes, dev_on, reads):
    result = plugin.GrounderWebSearchProvider().search(f"what is new on {PAGE}", limit=5)
    (row,) = result["data"]["web"]
    assert reads == [PAGE] and not stub.requests
    assert row["url"] == PAGE and row["description"].startswith("[Grounder direct: read directly from www.reddit.com on this Mac with Safari's user agent (Safari 27.2)")
    assert row["description"].endswith("\n\nposts")


def test_an_address_written_without_its_scheme_is_read_over_https(plugin, stub, hermes, dev_on, reads):
    plugin.GrounderWebSearchProvider().search("go to reddit.com/r/MechanicalKeyboards, not config.yaml", limit=5)
    assert reads == ["https://reddit.com/r/MechanicalKeyboards"]


def test_a_page_that_is_not_read_says_why_and_nothing_is_tried_in_its_place(plugin, stub, hermes, dev_on, reads):
    rows = plugin.GrounderWebSearchProvider().extract([PAGE, "https://www.reddit.com/search/?q=blocked"])
    assert reads == [PAGE, "https://www.reddit.com/search/?q=blocked"] and not stub.requests
    assert rows[0]["url"] == PAGE + "#grounder-direct" and "error" not in rows[0]  # marked, so Hermes caches no read
    assert "error" not in rows[1] and rows[1]["url"] == plugin.provider.GROUNDER_SITE  # filed away from the page, so Hermes caches no failure as its text
    assert "did not read https://www.reddit.com/search/?q=blocked" in rows[1]["content"] and "ask before making another search" in rows[1]["content"]


def test_an_address_that_must_not_leave_the_machine_is_not_read(plugin, stub, hermes, dev_on, reads):
    rows = plugin.GrounderWebSearchProvider().extract(["http://localhost:8080/admin"])
    assert reads == [] and "error" not in rows[0] and "did not read" in rows[0]["content"]


def test_grounder_search_searches_nothing_either(plugin, stub, hermes, dev_on, reads):
    answer = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "keyboards", "urls": [PAGE]}))
    assert reads == [PAGE] and not stub.requests and answer["data"]["web"][0]["url"] == PAGE


def test_off_by_default(plugin, stub, hermes):
    assert plugin.direct_dev.is_on() is False


@pytest.mark.skipif(os.environ.get("GROUNDER_DIRECT_LIVE") != "1", reason="reads reddit.com from this Mac; set GROUNDER_DIRECT_LIVE=1")
def test_live_reads_a_reddit_community_and_stops_at_reddits_search_block(plugin, stub, hermes, dev_on):
    provider = plugin.GrounderWebSearchProvider()
    community, search = provider.extract([PAGE, "https://www.reddit.com/search/?q=keychron&type=posts"])
    print("\nCOMMUNITY:", community["content"][:400].replace("\n", " / "))
    print("SEARCH:", search["content"][:400].replace("\n", " / "))
    assert community["url"] == PAGE + "#grounder-direct" and community["content"].startswith("[Grounder direct: read directly from www.reddit.com") and "MechanicalKeyboards" in community["content"]
    assert search["url"] == plugin.provider.GROUNDER_SITE and "did not read" in search["content"]
    assert not stub.requests




# Formats, /search-direct-format, and Grounder direct together with a search mode (owned by the /search session).

ARTICLE = ("Menu\nLog in\n"
           "Mechanical keyboards use an individual switch under every key, which gives each press a distinct feel and sound.\n"
           "The history of the typewriter begins in the nineteenth century with several competing designs across Europe.\n"
           "Hot-swappable mechanical keyboards let you change switches without soldering, which makes trying new switches cheap.\n")


@pytest.fixture
def article(plugin, monkeypatch):
    monkeypatch.setattr(plugin.direct_dev, "read", lambda url, signed_out=False: {"url": url, "status": 200, "title": "Keyboards", "text": ARTICLE, "chars": len(ARTICLE), "ms": 5})
    monkeypatch.setattr(plugin.direct_dev, "safari_version", lambda: "27.2")


def test_format_words_in_chat_pick_the_format(plugin):
    f = plugin.direct_formats.from_words
    assert f("give me the full page of it") == "full-page" and f("just the highlights") == "highlights" and f("summarize wikipedia on this") == "summary"
    assert f("tl;dr of that thread") == "summary" and f("what does reddit think") is None
    assert f("a summary, not the full page") == "summary"  # the earliest wins


def test_grounder_search_takes_a_valid_format_for_direct_reads(plugin, stub, hermes, dev_on, article):
    provider = plugin.GrounderWebSearchProvider()
    bad = json.loads(provider.search_tool({"query": "switches", "urls": [PAGE], "mode": "grounder-direct", "format": "poem"}))
    assert bad["success"] is False and "format must be one of" in bad["error"]
    for fmt in ("full-page", "highlights", "summary"):
        row = json.loads(provider.search_tool({"query": "hot-swappable switches", "urls": [PAGE], "mode": "grounder-direct", "format": fmt}))["data"]["web"][0]
        assert f"Format: {fmt}" in row["description"], fmt
    assert not stub.requests  # grounder-direct named in the call reads directly even with the mode off


def test_highlights_are_the_passages_that_match_the_question_in_page_order(plugin, stub, hermes, dev_on, article):
    text = json.loads(plugin.GrounderWebSearchProvider().search_tool(
        {"query": "hot-swappable switches", "urls": [PAGE], "mode": "grounder-direct", "format": "highlights"}))["data"]["web"][0]["description"]
    assert "Hot-swappable mechanical keyboards" in text and "Mechanical keyboards use an individual switch" in text
    assert "typewriter" not in text and "Log in" not in text
    assert text.index("individual switch") < text.index("Hot-swappable")


def test_full_page_is_the_whole_text(plugin, stub, hermes, dev_on, article):
    row = plugin.GrounderWebSearchProvider().search(f"read full page {PAGE}", limit=5)["data"]["web"][0]
    assert "Format: full-page" in row["description"] and "typewriter" in row["description"]
    assert "no search provider or Grounder credits were used" in row["description"]


def test_the_users_words_pick_the_format_of_a_web_extract(plugin, stub, hermes, dev_on, article, monkeypatch):
    monkeypatch.setattr(plugin.provider, "_session", lambda: "s")
    plugin.provider.note_message(session_id="s", user_message="summarize the mechanical keyboards article")
    (entry,) = plugin.GrounderWebSearchProvider().extract([PAGE])
    assert "Format: summary" in entry["content"]


def test_the_turn_note_asks_for_the_format_the_user_named(plugin, hermes, dev_on):
    note = plugin.direct_dev.turn_note(user_message="highlights from the wikipedia page on switches")["context"]
    assert 'format "highlights"' in note and 'mode "grounder-direct"' in note
    assert "format full-page" not in plugin.direct_dev.turn_note(user_message="what does wikipedia say")["context"]


def test_search_format_saves_the_default_format_and_chat_words_win_over_it(plugin, stub, hermes, dev_on, article, monkeypatch):
    reply = plugin.commands.search_format_command(Host(), "highlights")
    assert reply.startswith("Page format: Highlights") and plugin.direct_formats.saved() == "highlights"
    assert plugin.commands.search_format_command(Host(), "poem") == "Use /search-direct-format full-page, highlights or summary."
    assert plugin.commands.search_format_command(Host(["summary"]), "").startswith("Page format: Summary")
    row = plugin.GrounderWebSearchProvider().search(f"read full page {PAGE}", limit=5)["data"]["web"][0]
    assert "Format: full-page" in row["description"] and _dev(plugin_home(plugin)) is True  # saving the format kept the mode
    monkeypatch.setattr(plugin.provider, "_session", lambda: "s")
    plugin.provider.note_message(session_id="s", user_message=f"full page of {PAGE}")
    assert "Format: full-page" in plugin.GrounderWebSearchProvider().extract([PAGE])[0]["content"]


def plugin_home(plugin):
    from pathlib import Path
    return Path(os.environ["HERMES_HOME"])










def test_a_page_that_does_not_load_is_read_once_at_the_sites_other_host(plugin, monkeypatch):
    tried = []

    def at(url, signed_out=False):
        tried.append(url)
        if url.startswith("https://people.com"):
            return {"url": url, "status": 200, "problem": "load failed: bad URL"}
        return {"url": url, "status": 200, "title": "People", "text": "stories", "chars": 7, "ms": 9}

    monkeypatch.setattr(plugin.direct_dev, "_read_at", at)
    monkeypatch.setattr(plugin.direct_dev, "safari_version", lambda: "27.2")
    page = plugin.direct_dev.read("https://people.com/")
    assert tried == ["https://people.com/", "https://www.people.com/"] and not page.get("problem")
    assert "people.com did not load, so its other host was read" in plugin.direct_dev.header(page)
    blocked = []
    monkeypatch.setattr(plugin.direct_dev, "_read_at", lambda url, signed_out=False: blocked.append(url) or {"url": url, "status": 403, "problem": "the site answered HTTP 403"})
    assert plugin.direct_dev.read("https://www.example.com/").get("problem") and blocked == ["https://www.example.com/"]  # a site's own answer stands


def test_highlights_are_exact_quotes_listed_once_each(plugin, stub, hermes, dev_on, monkeypatch):
    page = ("Menu\nTrending today\nI never had a pet cat growing up, and now I have three.\nHotel staff on Reddit: what shocks guests?\nHotel staff on Reddit: what shocks guests?\n"
            "Advertisement: Amex founded Small Business Saturday to celebrate local shops.\nAmex founded Small Business Saturday to celebrate local shops.\n")
    monkeypatch.setattr(plugin.direct_dev, "read", lambda url, signed_out=False: {"url": url, "status": 200, "title": "r/popular", "text": page, "chars": len(page), "ms": 5})
    text = json.loads(plugin.GrounderWebSearchProvider().search_tool(
        {"query": "what is trending", "urls": [PAGE], "mode": "grounder-direct", "format": "highlights"}))["data"]["web"][0]["description"]
    assert "word for word" in text and "Add no words of your own" in text
    assert "Amex" not in text  # an ad and its unlabelled copy are left out
    assert text.count('"Hotel staff on Reddit: what shocks guests?"') == 1 and '"I never had a pet cat growing up, and now I have three."' in text


def test_summary_is_an_ai_summary_from_the_users_model(plugin, stub, hermes, dev_on, article):
    asked = []

    class Llm:
        def complete(self, messages, **kw):
            asked.append((messages, kw))
            return type("R", (), {"text": "Hot-swap boards let you change switches without soldering."})()

    plugin.direct_formats.use_host(type("Ctx", (), {"llm": Llm()})())
    text = json.loads(plugin.GrounderWebSearchProvider().search_tool(
        {"query": "hot-swappable switches", "urls": [PAGE], "mode": "grounder-direct", "format": "summary"}))["data"]["web"][0]["description"]
    assert "AI summary:\nHot-swap boards let you change switches without soldering." in text and 'headed "AI summary"' in text
    assert "typewriter" not in text  # the page's text went to the model, not to the answer
    assert "hot-swappable switches" in asked[0][0][0]["content"] and asked[0][1]["max_tokens"] == plugin.direct_formats.SUMMARY_TOKENS
    plugin.direct_formats.use_host(None)
    fallback = json.loads(plugin.GrounderWebSearchProvider().search_tool(
        {"query": "switches", "urls": [PAGE], "mode": "grounder-direct", "format": "summary"}))["data"]["web"][0]["description"]
    assert "could not be made here" in fallback and "typewriter" in fallback


def test_an_empty_script_only_page_is_reported_as_a_bot_check(plugin, monkeypatch):
    monkeypatch.setattr(plugin.direct_dev, "_read_at", plugin.direct_dev._read_at)
    page = {"url": "https://www.dailymail.co.uk/", "status": 200, "title": "", "text": "", "chars": 0, "links": [{"title": "x", "url": "https://x"}]}
    import subprocess
    monkeypatch.setattr(plugin.direct_dev, "_helper", lambda: (plugin.direct_dev.Path("/bin/echo"), ""))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(page) + "\n", ""))
    monkeypatch.setattr(plugin.direct_dev, "user_agent", lambda: "Safari")
    monkeypatch.setattr(plugin.direct_dev.sys, "platform", "darwin")
    problem = plugin.direct_dev._read_at("https://www.dailymail.co.uk/")["problem"]
    assert "bot check" in problem and "Nothing was done to get past it" in problem


def test_a_changed_format_or_mode_drops_hermess_cached_grounder_searches(plugin, hermes, monkeypatch, dev_on):
    import sys
    import threading
    import types
    memo = types.SimpleNamespace(_store={("grounder", "q", 10): (0, {}), ("exa", "q", 10): (0, {})}, _store_lock=threading.Lock())
    monkeypatch.setitem(sys.modules, "tools.web_result_cache", types.SimpleNamespace(search_memo=memo))
    plugin.direct_formats.save("summary")
    assert list(memo._store) == [("exa", "q", 10)]
    memo._store[("grounder", "q", 10)] = (0, {})
    plugin.direct_dev.set_state(plugin.direct_dev.OFF)
    assert list(memo._store) == [("exa", "q", 10)]




def test_a_read_through_grounders_resolver_says_so(plugin, monkeypatch):
    monkeypatch.setattr(plugin.direct_dev, "safari_version", lambda: "27.2")
    line = plugin.direct_dev.header({"url": PAGE, "status": 200, "ms": 5, "resolver": "grounder"})
    assert "its name looked up through Grounder's resolver" in line


def test_turning_grounder_direct_on_drops_pages_hermes_cached_from_grounders_reader(plugin, hermes, monkeypatch, tmp_path):
    import hashlib
    import sys
    import threading
    import types
    digest = lambda url, fmt, provider="": hashlib.sha256(f"{url}\n{fmt or 'markdown'}\n{provider or ''}".encode()).hexdigest()[:16]
    mine, other = tmp_path / "g.cache.md", tmp_path / "e.cache.md"
    mine.write_text("x"); other.write_text("y")
    index = {digest(PAGE, None, "grounder"): {"url": PAGE, "file": str(mine)}, digest(PAGE, None, "exa"): {"url": PAGE, "file": str(other)}}
    saved = {}
    cache = types.SimpleNamespace(_index_lock=threading.Lock(), _load_index=lambda: dict(index), _save_index=saved.update, _url_digest=digest)
    monkeypatch.setitem(sys.modules, "tools.web_result_cache", cache)
    plugin.direct_dev.set_state(plugin.direct_dev.ONLY)
    assert list(saved) == [digest(PAGE, None, "exa")] and not mine.exists() and other.exists()


def test_a_broken_sign_in_never_stops_a_read_it_is_read_logged_out(plugin, monkeypatch):
    calls = []

    def at(url, signed_out=False):
        calls.append(signed_out)
        if not signed_out:
            return {"url": url, "status": 0, "problem": "the keychain has a key for reddit.com but its sealed sign-in is missing; sign in again with /search-login reddit.com"}
        return {"url": url, "status": 200, "title": "r/x", "text": "posts", "chars": 5, "ms": 7}

    monkeypatch.setattr(plugin.direct_dev, "_read_at", at)
    monkeypatch.setattr(plugin.direct_dev, "safari_version", lambda: "27.2")
    page = plugin.direct_dev.read("https://www.reddit.com/r/x/")
    assert calls == [False, True] and not page.get("problem")
    assert "logged out because the saved sign-in for it could not be used" in plugin.direct_dev.header(page)


def test_the_user_can_ask_for_a_logged_out_read(plugin, stub, hermes, dev_on, monkeypatch):
    asked = []
    monkeypatch.setattr(plugin.direct_dev, "read", lambda url, signed_out=False: asked.append(signed_out) or {"url": url, "status": 200, "title": "t", "text": "x", "chars": 1, "ms": 1})
    monkeypatch.setattr(plugin.direct_dev, "safari_version", lambda: "27.2")
    monkeypatch.setattr(plugin.provider, "_session", lambda: "s")
    assert plugin.direct_formats.signed_out_words("read it not logged in") and plugin.direct_formats.signed_out_words("without my sign-in please")
    assert not plugin.direct_formats.signed_out_words("log in and read it")
    plugin.provider.note_message(session_id="s", user_message="read it not logged in")
    plugin.GrounderWebSearchProvider().extract([PAGE])
    json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "q", "urls": [PAGE], "mode": "grounder-direct", "signed_in": False}))
    plugin.provider.note_message(session_id="s", user_message="read it")
    plugin.GrounderWebSearchProvider().extract([PAGE])
    assert asked == [True, True, False]
    assert "The user asked to read logged out" in plugin.direct_dev.turn_note(user_message="read it not logged in")["context"]
    assert "Pass signed_in false" not in plugin.direct_dev.turn_note(user_message="read reddit")["context"]


def test_a_logged_out_read_tells_the_reader_to_use_no_sign_in(plugin, monkeypatch):
    import subprocess
    seen = {}
    monkeypatch.setattr(plugin.direct_dev, "_helper", lambda: (plugin.direct_dev.Path("/bin/echo"), ""))
    monkeypatch.setattr(plugin.direct_dev, "user_agent", lambda: "Safari")
    monkeypatch.setattr(plugin.direct_dev.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.update(k.get("env") or {}) or subprocess.CompletedProcess(a, 0, json.dumps({"url": PAGE, "status": 200, "title": "t", "text": "x"}) + "\n", ""))
    plugin.direct_dev._read_at(PAGE, True)
    assert seen.get("GROUNDER_DIRECT_SIGNED_OUT") == "1"
    seen.clear()
    plugin.direct_dev._read_at(PAGE)
    assert "GROUNDER_DIRECT_SIGNED_OUT" not in seen


# Saved sign-ins (0.8.0-dev.2): the reader holds the values in the login keychain; Hermes only names sites.



def test_a_site_is_a_bare_name_or_an_https_address(plugin):
    site_of = plugin.direct_dev.site_of
    assert site_of("reddit.com") == ("reddit.com", "https://reddit.com/")
    assert site_of("www.Reddit.com/") == ("reddit.com", "https://www.reddit.com/")
    assert site_of("https://www.reddit.com/login") == ("reddit.com", "https://www.reddit.com/login")
    assert site_of("http://reddit.com") == ("reddit.com", "https://reddit.com/")  # cleaned, and opened over https
    assert site_of("reddit")[0] == "" and site_of("a b.com")[0] == ""


def test_a_page_read_signed_in_says_so_and_names_no_value(plugin, monkeypatch):
    monkeypatch.setattr(plugin.direct_dev, "safari_version", lambda: "27.2")
    line = plugin.direct_dev.header({"url": PAGE, "status": 200, "ms": 9, "signed_in": "reddit.com", "cookies_sent": 7})
    assert "signed in to reddit.com with the sign-in the user saved (7 cookies sent)" in line and "logged-out" not in line
    assert "a fresh logged-out session" in plugin.direct_dev.header({"url": PAGE, "status": 200, "ms": 9})


@pytest.mark.skipif(os.environ.get("GROUNDER_DIRECT_LIVE") != "1", reason="writes a test item to the login keychain and reads httpbin.org; set GROUNDER_DIRECT_LIVE=1")
def test_live_a_saved_sign_in_goes_with_safaris_user_agent_to_its_site_only(plugin):
    import base64
    import stat
    import subprocess
    dev = plugin.direct_dev
    try:
        assert dev.paste("httpbin.org", "Cookie: grounder_test=fake-one; other=fake-two").startswith("Saved a sign-in for httpbin.org: 2 cookies")
        assert "httpbin.org" in [site for site, _ in dev.sites()]
        key = subprocess.run([dev.SECURITY, "find-generic-password", "-s", dev.SERVICE, "-a", "httpbin.org", "-w"], capture_output=True, text=True).stdout.strip()
        assert len(base64.b64decode(key)) == 32  # the keychain holds only a key
        sealed = dev.STORE / "httpbin.org.sealed"
        assert stat.S_IMODE(sealed.stat().st_mode) == 0o600 and b"fake-one" not in sealed.read_bytes()  # the cookies are sealed, not stored in the clear
        page = dev.read("https://httpbin.org/headers")
        sent = json.loads(page["text"])["headers"]
        assert sent["Cookie"] == "grounder_test=fake-one; other=fake-two" and sent["User-Agent"] == dev.user_agent() and page["signed_in"] == "httpbin.org"
        assert "fake-one" not in dev.header(page)
        refreshed = dev.read("https://httpbin.org/cookies/set?fresh=1")  # the site sets a cookie: saved back for the next read
        assert refreshed.get("sign_in_refreshed") is True
        assert json.loads(dev.read("https://httpbin.org/cookies")["text"])["cookies"] == {"grounder_test": "fake-one", "other": "fake-two", "fresh": "1"}
        assert json.loads(dev.read("https://postman-echo.com/cookies")["text"])["cookies"] == {}  # another site gets none
    finally:
        assert "Forgot the httpbin.org sign-in" in dev.sign_out("httpbin.org")
    assert dev.sign_out("httpbin.org") == "No sign-in was saved for httpbin.org." and not (dev.STORE / "httpbin.org.sealed").exists()


@pytest.mark.skipif(os.environ.get("GROUNDER_DIRECT_LIVE") != "1", reason="writes a test item to the login keychain and reads httpbin.org; set GROUNDER_DIRECT_LIVE=1")
def test_live_a_sign_in_saved_by_0_8_0_dev_2_still_works_and_moves_to_the_new_store(plugin):
    import base64
    import subprocess
    dev = plugin.direct_dev
    old = {"site": "httpbin.org", "cookies": [{"name": "legacy", "value": "fake-old", "domain": ".httpbin.org", "path": "/", "secure": True, "httpOnly": True}]}
    subprocess.run([dev.SECURITY, "add-generic-password", "-U", "-s", dev.LEGACY_SERVICE, "-a", "httpbin.org", "-w",
                    base64.b64encode(json.dumps(old).encode()).decode()], check=True, capture_output=True)  # the old format, fake value
    try:
        assert json.loads(dev.read("https://httpbin.org/cookies")["text"])["cookies"] == {"legacy": "fake-old"}
        dev.read("https://httpbin.org/cookies/set?fresh=1")  # a change is saved in the new store, and the old item goes
        gone = subprocess.run([dev.SECURITY, "find-generic-password", "-s", dev.LEGACY_SERVICE, "-a", "httpbin.org"], capture_output=True)
        assert gone.returncode == 44 and (dev.STORE / "httpbin.org.sealed").exists()
        assert json.loads(dev.read("https://httpbin.org/cookies")["text"])["cookies"] == {"legacy": "fake-old", "fresh": "1"}
    finally:
        dev.sign_out("httpbin.org")


# /search-login (0.8.0): a site typed any way is cleaned to the site itself, and one sign-in covers every subdomain and page of it.

@pytest.mark.parametrize("typed, site, start", [
    ("reddit.com", "reddit.com", "https://reddit.com/"),
    ("https://old.reddit.com/r/x?sort=new", "reddit.com", "https://old.reddit.com/r/x?sort=new"),
    ("www.Reddit.com", "reddit.com", "https://www.reddit.com/"),
    ("*.reddit.com", "reddit.com", "https://reddit.com/"),
    ("reddit.com/login", "reddit.com", "https://reddit.com/login"),
    ("http://news.bbc.co.uk/sport", "bbc.co.uk", "https://news.bbc.co.uk/sport"),
    ("someone.github.io/repo", "someone.github.io", "https://someone.github.io/repo"),
])
def test_a_site_typed_any_way_is_cleaned_to_the_site_itself(plugin, typed, site, start):
    assert plugin.direct_dev.site_of(typed) == (site, start)


@pytest.mark.parametrize("typed", ["", "reddit", "localhost", "192.168.1.1", "ftp://reddit.com", "a b.com"])
def test_what_is_not_a_site_is_refused(plugin, typed):
    assert plugin.direct_dev.site_of(typed)[0] == ""


def test_search_login_opens_the_window_for_the_cleaned_site(plugin, monkeypatch):
    opened = []
    monkeypatch.setattr(plugin.direct_dev, "sign_in", lambda raw, wait=False: opened.append(raw) or "opened")
    monkeypatch.setattr(plugin.direct_dev, "sites", lambda: [("reddit.com", "saved today")])
    host = Host()
    plugin.direct_dev.register_login(host)
    assert host.commands["search-login"].handler("https://old.reddit.com/r/x") == "opened" and opened == ["https://old.reddit.com/r/x"]
    usage = host.commands["search-login"].handler("")
    assert "Usage: `/search-login <site>`" in usage and "reddit.com: saved today" in usage


@pytest.mark.skipif(os.environ.get("GROUNDER_DIRECT_LIVE") != "1", reason="writes a test item to the login keychain and reads httpbin.org; set GROUNDER_DIRECT_LIVE=1")
def test_live_one_sign_in_covers_every_subdomain_and_page(plugin):
    dev = plugin.direct_dev
    helper, _ = dev._helper()
    # Cookies as a site might scope them: one host-only, one on a path, one __Host- (which must stay host-only).
    try:
        assert dev.paste("www.httpbin.org/anything", "a=fake-1; b=fake-2").startswith("Saved a sign-in for httpbin.org: 2 cookies")
        for url in ("https://httpbin.org/cookies", "https://www.httpbin.org/anything/deep/page?x=1"):
            page = dev.read(url)
            seen = json.loads(page["text"]).get("cookies") or dict(c.split("=", 1) for c in json.loads(page["text"])["headers"].get("Cookie", "").split("; ") if c)
            assert seen.get("a") == "fake-1" and seen.get("b") == "fake-2" and page["signed_in"] == "httpbin.org", url
    finally:
        dev.sign_out("httpbin.org")


# The resolver setting (0.8.0): the user's own by default, Grounder's when picked.

def test_reads_use_the_users_own_resolver_unless_grounders_is_picked(plugin, tmp_path):
    dev = plugin.direct_dev
    assert dev.resolver() == "system" and dev.reader_env() == {}
    dev.set_resolver("grounder")
    assert dev.resolver() == "grounder" and dev.reader_env() == {"GROUNDER_DIRECT_RESOLVER": "https://api.grounder.net/v1/dns-query"}
    dev.set_resolver("system")
    assert dev.reader_env() == {}
    with pytest.raises(ValueError):
        dev.set_resolver("8.8.8.8")


@pytest.mark.skipif(os.environ.get("GROUNDER_DIRECT_LIVE") != "1", reason="reads people.com and httpbin.org from this Mac; set GROUNDER_DIRECT_LIVE=1")
def test_live_a_read_through_another_resolver_reaches_the_site_and_refuses_private_answers(plugin, monkeypatch):
    dev = plugin.direct_dev
    dev.set_resolver("grounder")
    # Cloudflare's resolver at its address, standing in until Grounder's answers (a DNS filter that blocks cloudflare-dns.com does not block 1.1.1.1).
    monkeypatch.setattr(dev, "GROUNDER_RESOLVER", os.environ.get("GROUNDER_DIRECT_TEST_RESOLVER", "https://1.1.1.1/dns-query"))
    page = dev.read("https://httpbin.org/headers")
    assert page.get("resolver") == "grounder" and not page.get("problem") and json.loads(page["text"])["headers"]["Host"] == "httpbin.org"
    private = dev.read("https://localtest.me/")  # a public name that answers 127.0.0.1
    assert "only a private or local address" in private["problem"]


def test_search_logout_forgets_the_cleaned_site(plugin, monkeypatch):
    forgot = []
    monkeypatch.setattr(plugin.direct_dev, "sign_out", lambda raw: forgot.append(raw) or "forgot")
    monkeypatch.setattr(plugin.direct_dev, "sites", lambda: [])
    host = Host()
    plugin.direct_dev.register_login(host)
    assert set(host.commands) == {"search-login", "search-logout"}
    assert host.commands["search-logout"].handler("https://old.reddit.com/r/x") == "forgot" and forgot == ["https://old.reddit.com/r/x"]
    assert "Usage: `/search-logout <site>`" in host.commands["search-logout"].handler("")


def test_signing_out_cleans_the_site_the_same_way(plugin, monkeypatch, tmp_path):
    import subprocess
    monkeypatch.setattr(plugin.direct_dev, "STORE", tmp_path)  # never the user's real sign-ins
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: ran.append(args) or subprocess.CompletedProcess(args, 44, "", ""))
    assert plugin.direct_dev.sign_out("https://old.reddit.com/r/x") == "No sign-in was saved for reddit.com."
    assert all(args[-1] == "reddit.com" for args in ran) and len(ran) == 2  # both keychain services, the cleaned site


# 0.8.0: the installed grounder-direct is the reader when it runs; the swiftc build is the fallback, and with neither the problem names the install page.

def _fake_program(path, says):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{says}'\n")
    path.chmod(0o755)
    return path


def test_the_installed_program_is_the_reader_and_runs_as_its_reader_subcommand(plugin, monkeypatch, tmp_path):
    dd = plugin.direct_dev
    program = _fake_program(tmp_path / "bin" / "grounder-direct", "grounder-direct 0.8.0")
    monkeypatch.setattr(dd, "INSTALLED", program)
    monkeypatch.setattr(dd, "_installed_ok", {})
    monkeypatch.setattr(dd, "_build_helper", lambda: (_ for _ in ()).throw(AssertionError("no build when the program is installed")))
    helper, why = dd._helper()
    assert helper == program and why == ""
    assert dd._reader_command(helper) == [str(program), "reader"]
    assert dd._reader_command(tmp_path / "direct-reader-abc") == [str(tmp_path / "direct-reader-abc")]


def test_a_broken_installed_program_falls_back_to_the_build_and_with_neither_the_problem_names_the_install(plugin, monkeypatch, tmp_path):
    dd = plugin.direct_dev
    broken = _fake_program(tmp_path / "bin" / "grounder-direct", "not it")
    monkeypatch.setattr(dd, "INSTALLED", broken)
    monkeypatch.setattr(dd, "_installed_ok", {})
    built = tmp_path / "direct-reader-abc"
    monkeypatch.setattr(dd, "_build_helper", lambda: (built, ""))
    assert dd._helper() == (built, "")
    monkeypatch.setattr(dd, "_build_helper", lambda: (None, "this Mac has no Xcode command line tools, so the reader cannot be built here"))
    helper, why = dd._helper()
    assert helper is None and "https://grounder.net/docs/direct" in why


def test_a_stored_full_page_without_the_users_choice_reads_as_summary(plugin, hermes, monkeypatch):
    # GRO-988 (Harlan, 2026-11-24: "if grounder direct is used, it should by default be in summary mode"): an older default's full-page is not a choice, as on the account
    # (formatInForce), so a simple question is never answered with a whole page pasted back; the user's own /search-direct-format full-page still holds.
    monkeypatch.setattr(plugin.direct_dev, "account_direct_reader", lambda: None)
    plugin.direct_dev.save_setting(plugin.direct_formats.FORMAT_KEY, "full-page")
    assert plugin.direct_formats.saved() is None
    assert plugin.direct_formats.chosen(None, "who was the most popular person at the most recent oscars", plugin.direct_formats.saved()) == "summary"
    assert plugin.direct_formats.chosen(None, "give me the full page", plugin.direct_formats.saved()) == "full-page"
    plugin.direct_formats.save("full-page")
    assert plugin.direct_formats.saved() == "full-page"
    plugin.direct_dev.save_setting(plugin.direct_formats.FORMAT_KEY, "highlights")
    assert plugin.direct_formats.saved() == "highlights"
