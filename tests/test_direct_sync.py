# Copyright 2026 Grounder Labs, Inc
# SPDX-License-Identifier: Apache-2.0

"""The account's copy of Grounder direct's saved sign-ins (GRO-933): the site names, which Mac and when each was saved, listed on grounder.net so one can be removed
there; the cookies never leave this Mac. HOME is a scratch folder and the keychain, the forgetting and the account's answers are stand-ins: no test lists the
user's keychain or calls Grounder.
"""

import io
import json
import re
import urllib.error

import pytest

NOTE = "saved 2026-11-19T12:00:00Z by sign-in window, 7 cookies"
UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture
def mac(plugin, tmp_path, monkeypatch):
    """A Mac with a scratch HOME, a stand-in keychain and sign-out, and an account that answers from a script."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    dd = plugin.direct_dev
    state = {"held": [("reddit.com", NOTE), ("bbc.co.uk", "odd")], "answers": [], "sent": [], "forgot": [], "fails": False}
    monkeypatch.setattr(dd, "listed", lambda timeout=30: None if state["fails"] else list(state["held"]))
    monkeypatch.setattr(dd, "device_name", lambda: "Harlan's Mac")

    def sign_out(site):
        state["forgot"].append(site)
        state["held"] = [row for row in state["held"] if row[0] != site]
        return f"Forgot the {site} sign-in"

    def sender(body, timeout):
        state["sent"].append(body)
        return state["answers"].pop(0) if state["answers"] else None

    monkeypatch.setattr(dd, "sign_out", sign_out)
    monkeypatch.setattr(dd, "account_sender", sender)
    monkeypatch.setattr(dd, "credential_probe", lambda: True)
    state["home"] = home
    return state


def test_a_report_names_this_mac_each_site_once_and_keeps_the_time(plugin, mac):
    dd = plugin.direct_dev
    mac["answers"] = [{"remove": []}]
    assert dd.sync().startswith("Synced 2 saved sign-ins")
    body = mac["sent"][0]
    assert UUID_V4.match(body["device"]["id"]) and body["device"]["name"] == "Harlan's Mac"
    assert body["sites"] == [{"site": "bbc.co.uk", "saved_at": None}, {"site": "reddit.com", "saved_at": "2026-11-19T12:00:00Z"}]
    folder = mac["home"] / "Library" / "Application Support" / "Grounder Direct"
    assert json.loads((folder / "device.json").read_text()) == {"id": body["device"]["id"]}
    assert (folder / "device.json").stat().st_mode & 0o777 == 0o600
    assert dd.last_sync() is not None and not dd.sync_due()
    assert dd.device_id() == body["device"]["id"]  # kept from one run to the next


def test_a_removal_is_checked_then_forgotten_and_the_list_reported_again(plugin, mac):
    mac["answers"] = [{"remove": ["reddit.com", "../x", "old.reddit.com", "nosuch.com", "reddit.com", 5, "a/b.com"]}, {"remove": []}]
    said = plugin.direct_dev.sync()
    assert mac["forgot"] == ["reddit.com"]
    assert len(mac["sent"]) == 2 and [row["site"] for row in mac["sent"][1]["sites"]] == ["bbc.co.uk"]
    assert "forgot reddit.com" in said


def test_nothing_is_sent_empty_when_the_keychain_cannot_be_listed(plugin, mac):
    mac["fails"] = True
    mac["answers"] = [{"remove": ["reddit.com"]}]
    assert plugin.direct_dev.sync().startswith("Skipped") and mac["sent"] == [] and mac["forgot"] == []


def test_an_account_that_does_not_answer_changes_nothing(plugin, mac):
    assert "Nothing was sent" in plugin.direct_dev.sync()
    assert mac["forgot"] == [] and plugin.direct_dev.last_sync() is None


def test_listed_is_none_when_security_fails(plugin, monkeypatch):
    import subprocess
    dd = plugin.direct_dev
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 36, "", "locked"))
    assert dd.listed() is None and dd.sites() == []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("security", 3)))
    assert dd.listed(3) is None


@pytest.mark.parametrize("raw,ok", [("reddit.com", True), ("bbc.co.uk", True), ("old.reddit.com", False), ("Reddit.com", False), ("reddit.com/", False), ("../etc.passwd", False),
                                    ("a..b.com", False), ("reddit.com\n", False), ("", False), (None, False), (["reddit.com"], False), ("a/../b.com", False)])
def test_a_removal_names_only_a_site_this_mac_could_have_saved(plugin, raw, ok):
    assert plugin.direct_dev.removal_site(raw) == (raw if ok else "")


def test_the_words_say_where_else_the_list_is(plugin, monkeypatch):
    dd = plugin.direct_dev
    monkeypatch.setattr(dd, "sites", lambda: [("reddit.com", NOTE)])
    monkeypatch.setattr(dd, "last_sync", lambda: object())  # a sync has worked
    assert "also on your Grounder account" in dd.sites_text() and "#route-signins" in dd.sites_text() and "cookies stay on this Mac" in dd.sites_text()
    monkeypatch.setattr(dd, "last_sync", lambda: None)  # none has, whatever key is held
    assert dd.sites_text().endswith("This list is on this Mac only.")


def test_a_read_syncs_in_the_background_the_first_time_and_then_when_the_last_sync_is_old(plugin, mac, monkeypatch):
    import threading
    dd = plugin.direct_dev
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))
    monkeypatch.setattr(dd, "_last_attempt", -1.0)
    dd.sync_soon()
    assert len(started) == 1  # the first use in this process
    dd.sync_soon()
    assert len(started) == 1  # not again at once
    dd.sync_soon(force=True)
    assert len(started) == 2  # after a sign-in or sign-out
    monkeypatch.setattr(dd, "account_sender", None)
    dd.sync_soon(force=True)
    assert len(started) == 2  # no account, no sync


def test_a_sign_out_from_the_command_syncs_at_once(plugin, mac, monkeypatch):
    dd = plugin.direct_dev
    calls = []
    monkeypatch.setattr(dd, "sync_soon", lambda force=False: calls.append(force))
    assert "reddit.com" in dd.logout_command("reddit.com") and calls == [True]


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_the_request_is_a_put_with_the_key_and_follows_no_redirect(plugin, monkeypatch):
    pr = plugin.provider
    seen = []
    monkeypatch.setenv("GROUNDER_API_KEY", "gr-test-key")
    monkeypatch.setattr(pr, "_open", lambda request, timeout: seen.append((request, timeout)) or _Response(b'{"remove":["reddit.com"]}'))
    answer = pr._sign_ins_call({"device": {"id": "x", "name": "M"}, "sites": []}, 3)
    request, timeout = seen[0]
    assert answer == {"remove": ["reddit.com"]} and timeout == 3
    assert request.get_method() == "PUT" and request.full_url == "https://api.grounder.net/v1/direct/sign-ins"
    assert request.get_header("Authorization") == "Bearer gr-test-key" and json.loads(request.data)["device"]["name"] == "M"
    assert request.get_header("User-agent") == "grounder-hermes/0.8.3"


def test_a_refusal_or_a_missing_credential_is_no_answer_and_raises_nothing(plugin, monkeypatch):
    pr = plugin.provider
    monkeypatch.setenv("GROUNDER_API_KEY", "gr-test-key")

    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b'{"error":{"code":"unauthorized"}}'))

    monkeypatch.setattr(pr, "_open", refused)
    assert pr._sign_ins_call({}, 3) is None
    monkeypatch.setenv("GROUNDER_API_KEY", "bad key")
    assert pr._sign_ins_call({}, 3) is None  # a key that cannot go in a header is never sent
    monkeypatch.delenv("GROUNDER_API_KEY")
    assert pr._sign_ins_call({}, 3) is None  # no key and no Hermes sign-in


@pytest.mark.parametrize("text,ok", [("2026-02-30T10:00:00Z", False), ("2026-13-01T10:00:00Z", False), ("2026-11-19T25:00:00Z", False), ("2026-11-19T12:00:60Z", False),
                                     ("2025-02-29T12:00:00Z", False), ("0000-01-01T00:00:00Z", False), ("2024-02-29T12:00:00Z", True), ("2026-11-19T12:00:00.5Z", True)])
def test_a_save_time_the_account_would_refuse_is_sent_as_none(plugin, text, ok):
    assert plugin.direct_dev.saved_at(f"saved {text} by paste") == (text if ok else None)


def test_the_name_is_cut_by_code_points_as_the_account_cuts_it(plugin):
    family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"  # one character to the eye, five code points
    cut = plugin.direct_dev.sync_body("x", "m" * 58 + family, [])["device"]["name"]
    assert len(cut) == 60 and cut == ("m" * 58 + family)[:60]


def test_a_plugin_with_nothing_to_send_with_lists_no_keychain(plugin, mac, monkeypatch):
    import threading
    dd = plugin.direct_dev
    listed = []
    monkeypatch.setattr(dd, "listed", lambda timeout=30: listed.append(timeout) or [])
    probed = []
    monkeypatch.setattr(dd, "credential_probe", lambda: probed.append(threading.current_thread()) or False)
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))
    monkeypatch.setattr(dd, "_last_attempt", -1.0)
    dd.sync_soon(force=True)
    assert probed == []  # the probe can be a network call, so a read never waits on it: it runs in the background thread
    assert len(started) == 1
    assert "stays on this Mac only" in dd.sync() and listed == []


def test_the_background_sync_waits_for_a_slow_keychain_longer_than_a_read_does(plugin, mac, monkeypatch):
    dd = plugin.direct_dev
    waits = []
    monkeypatch.setattr(dd, "listed", lambda timeout=30: waits.append(timeout) or [])
    mac["answers"] = [{"remove": []}]
    dd._quiet_sync()
    assert waits == [dd.BACKGROUND_LISTING_S] and dd.BACKGROUND_LISTING_S > dd.SYNC_TIMEOUT_S


def test_two_programs_starting_together_keep_one_id(plugin, mac):
    dd = plugin.direct_dev
    first = "0b5d3497-7477-4121-8d1c-9a3f5e6b7c80"
    second = "1c6e4508-8588-4232-9e2d-0b4a6f7c8d91"
    assert dd.claim_device_id(first) == first
    assert dd.claim_device_id(second) == first and dd.device_id() == first
    (mac["home"] / "Library" / "Application Support" / "Grounder Direct" / "device.json").write_text("{not json")
    assert dd.claim_device_id(second) == second  # a damaged file is replaced
