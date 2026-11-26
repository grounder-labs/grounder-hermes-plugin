# Copyright 2026 Grounder Labs, Inc
# SPDX-License-Identifier: Apache-2.0

"""Grounder direct: go straight to the address the user gives, from this Mac, asking for it the way their Safari does.

Grounder direct reads the source’s page for a search, or the page named by address. The saved account choice takes precedence over the setting on this Mac.

Each read starts a fresh, empty session. Where the user saved a sign-in for the page's site (``/search-login <site>``, which opens the site's own sign-in page
in a window on this Mac, or ``python3 direct_dev.py paste <site>`` in a terminal, for a Cookie header), that site's cookies are set in it first, so the site sees them
signed in, asked for with the same Safari user agent; cookies the site changes during a read are saved back. The reader seals each site's cookies in a file only
this user can read (``~/Library/Application Support/Grounder Direct/sign-ins``), under a key only the login keychain holds (service ``grounder-direct``); no cookie
value passes through Hermes, the model, this module, a log or a command line, and none is sent to any other site. A site's own checks run as they would in any browser; nothing here solves or skips one. Nothing a read returns is kept or sent
anywhere. One read at a time.

From a terminal: ``python3 ~/.hermes/plugins/grounder/direct_dev.py signin|paste|forget <site>`` or ``sites``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

NAME = "Grounder direct"
SETTINGS_KEY, DIRECT_KEY, DEV_KEY = "grounder", "direct", "direct_dev"  # web.grounder.direct ("only", "with-search" or "off"); direct_dev is 0.8.0-dev's, still read
SAFARI_PLIST = Path("/Applications/Safari.app/Contents/Info.plist")
# Safari freezes the platform and WebKit parts of its user agent; only Version/ follows the installed Safari. Checked 2026-11-19 against the request Safari 27.2 sent.
SAFARI_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{version} Safari/605.1.15"
SOURCE = Path(__file__).with_name("direct_reader.swift")
# The prebuilt reader Grounder's installer puts on a Mac (grounder-direct, signed and notarized, which also carries the reader): preferred over building one.
INSTALLED = Path.home() / "Library" / "Application Support" / "Grounder Direct" / "bin" / "grounder-direct"
# Named by its docs page, not as a curl-to-shell line, which Hermes's security scan flags in a plugin.
INSTALL_PAGE = "https://grounder.net/docs/direct"
MAX_CHARS = 60000  # most characters of a page's text a read returns; Hermes cuts a page to the model's char_limit itself
READ_TIMEOUT_S = 45  # the helper stops itself at 30 s
BUILD_TIMEOUT_S = 240
SAME_HOST_GAP_S = 2.0  # least time between two reads of one site
GATES = re.compile(r"blocked by network security|prove your humanity|please wait for verification|verify (?:that )?you are (?:a )?human|"
                   r"just a moment\.\.\.|attention required|unusual traffic|are you a robot|access denied|log in to use old reddit", re.I)

SERVICE = "grounder-direct"  # the login keychain service holding each site's key (direct_reader.swift)
LEGACY_SERVICE = "grounder-direct-dev"  # 0.8.0-dev.2's items, which held the sign-in itself; the reader still reads them
STORE = Path.home() / "Library" / "Application Support" / "Grounder Direct" / "sign-ins"  # each site's sealed cookies
SECURITY = "/usr/bin/security"
SITE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")

_lock = threading.Lock()
_last_read: dict[str, float] = {}


ONLY, OFF = "only", "off"
SEARCH_PRICE = "A Grounder direct search is one search, at the one price, even if its page could not be read."


# Set by provider.py: ``account_direct_reader()`` is the ``direct`` block of the account's saved setting (``use``, ``format``) or None with no credential or no block, and
# ``account_direct_writer(change)`` saves ``change`` (``{"use": ...}`` or ``{"format": ...}``) there, True when it was saved. The account is the source when there is one.
account_direct_reader: Callable[[], dict[str, Any] | None] = lambda: None
account_direct_writer: Callable[[dict[str, Any]], bool | None] = lambda change: None  # True saved, False Grounder did not take it, None no credential
account_save_error = ""  # why the last account save was not taken (set by provider.py)
_last_save: dict[str, Any] = {"on_account": None}  # the last save's outcome: True (on the account), False (the account refused it), None (no credential: this machine only)
USE_WORDS = {"off": "off", "only": "only"}


def account_use() -> str | None:
    """The effective account choice, or None when no account setting can be read."""
    use = (account_direct_reader() or {}).get("use")
    return None if use is None else ONLY if use == ONLY else OFF


def save_to_account(change: dict[str, Any]) -> None:
    """Save ``change`` on the account (``search.mode`` or page settings) and keep what came of it for the words under the reply (``saved_note``)."""
    _last_save["on_account"] = account_direct_writer(change)


def saved_note(key: str = DIRECT_KEY) -> str:
    """The line under a reply that says where the setting just saved went: the account when it took it, this machine alone with no credential, and when the account refused it,
    this machine alone and why, since the account's own value still applies."""
    saved = _last_save["on_account"]
    if saved is True:
        return "(saved on your Grounder account)"
    if saved is False:
        return (f"(saved on this machine only: Grounder did not take it{f' ({account_save_error})' if account_save_error else ''}, "
                "so the setting on your Grounder account still applies)")
    return f"(saved on this machine: web.grounder.{key} in config.yaml)"


def state() -> str:
    """Read the account choice first, then the local choice."""
    from_account = account_use()
    if from_account is not None:
        return from_account
    try:
        from hermes_cli.config import load_config
        web = load_config().get("web")
    except Exception as error:
        logger.warning("Could not read the Grounder direct setting: %s", error)
        return OFF
    section = web.get(SETTINGS_KEY) if isinstance(web, dict) else None
    section = section if isinstance(section, dict) else {}
    if isinstance(section.get(DIRECT_KEY), str):
        word = section[DIRECT_KEY].strip().lower().replace("_", "-")
        return ONLY if word == ONLY else OFF
    value = section.get(DEV_KEY)
    if value is True or str(value).lower() == ONLY:
        return ONLY
    return OFF


def is_on() -> bool:
    """Whether Grounder direct is the mode by itself (``only``)."""
    return state() == ONLY


def reads_pages() -> bool:
    return state() == ONLY


def save_setting(key: str, value: Any) -> None:
    """Save one key of ``web.grounder``, keeping the section's others whether or not this Hermes's save merges nested sections."""
    from hermes_cli.config import load_config, save_config
    try:
        web = load_config().get("web")
    except Exception:
        web = None
    section = dict(web.get(SETTINGS_KEY) or {}) if isinstance(web, dict) and isinstance(web.get(SETTINGS_KEY), dict) else {}
    section[key] = value
    save_config({"web": {SETTINGS_KEY: section}}, merge_existing=True)


def set_state(value: str) -> None:
    """Save the setting on this machine and, with a credential, on the account (``search.mode``) too."""
    use = USE_WORDS[value]
    save_setting(DIRECT_KEY, use)
    save_to_account({"use": use})
    from . import direct_formats
    direct_formats.forget_cached_searches()
    if value != OFF:  # reading straight from sites from now on: no page Grounder's reader cached earlier is served in its place
        direct_formats.forget_cached_pages()


def set_on(on: bool) -> None:
    set_state(ONLY if on else OFF)


def safari_version() -> str:
    """The installed Safari's version (``27.2``), or "" without one."""
    try:
        with SAFARI_PLIST.open("rb") as handle:
            return str(plistlib.load(handle).get("CFBundleShortVersionString") or "")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ""


def user_agent() -> str:
    version = safari_version()
    return SAFARI_UA.format(version=version) if version else ""


def _home() -> Path:
    """The active profile's Hermes home (``hermes_constants.get_hermes_home``, which follows a profile switched after launch); the launch profile's ``HERMES_HOME``
    only where that module cannot be imported."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def minimal_env(*also: str) -> dict[str, str]:
    """The environment every program this plugin starts gets: HOME and PATH (and the named ``also`` variables, when set), never the Hermes process's own, which holds
    the user's provider keys. The reader, the sign-in window and the version check run code the pinned source's sha does not cover (the prebuilt grounder-direct)."""
    found = {"HOME": os.environ.get("HOME") or str(Path.home()), "PATH": os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"}
    found.update({name: os.environ[name] for name in also if os.environ.get(name)})
    return found


_installed_ok: dict[str, bool] = {}


def _installed() -> bool:
    """Whether the installed grounder-direct exists and runs (it answers ``version``). Asked once a process."""
    key = str(INSTALLED)
    if key not in _installed_ok:
        ok = False
        if INSTALLED.is_file() and os.access(INSTALLED, os.X_OK):
            try:
                done = subprocess.run([key, "version"], capture_output=True, text=True, timeout=10, env=minimal_env())
                ok = done.returncode == 0 and done.stdout.startswith("grounder-direct ")
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        _installed_ok[key] = ok
    return _installed_ok[key]


def _reader_command(helper: Path) -> list[str]:
    """The command that runs the reader: the installed grounder-direct's ``reader`` subcommand, or the compiled reader itself."""
    return [str(helper), "reader"] if helper.name == INSTALLED.name else [str(helper)]


def _helper() -> tuple[Optional[Path], str]:
    """The reader: the installed grounder-direct when there is one that runs, else one compiled from ``direct_reader.swift`` the first time and again whenever the
    source changes; or None and why not (which names the install page)."""
    if _installed():
        return INSTALLED, ""
    helper, why = _build_helper()
    return (helper, why) if helper is not None else (None, f"{why}; Grounder's installer adds a prebuilt reader: {INSTALL_PAGE}")


def _build_helper() -> tuple[Optional[Path], str]:
    try:
        source = SOURCE.read_bytes()
    except OSError as error:
        return None, f"the reader's source is missing ({error})"
    built = _home() / "cache" / "grounder-direct" / f"direct-reader-{hashlib.sha256(source).hexdigest()[:12]}"
    if built.exists():
        return built, ""
    try:  # a Mac without the command line tools has only a stub xcrun, which opens an install dialog: ask first
        tools = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, timeout=30, env=minimal_env("DEVELOPER_DIR"))
    except (OSError, subprocess.TimeoutExpired):
        tools = None
    if tools is not None and tools.returncode != 0:
        return None, "this Mac has no Xcode command line tools, so the reader cannot be built here"
    built.parent.mkdir(parents=True, exist_ok=True)
    partial = built.with_suffix(".partial")
    try:
        done = subprocess.run(["xcrun", "swiftc", "-O", "-o", str(partial), str(SOURCE)], capture_output=True, text=True, timeout=BUILD_TIMEOUT_S,
                              env=minimal_env("DEVELOPER_DIR", "TMPDIR", "SDKROOT"))
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"could not build the reader with Xcode's swiftc ({error})"
    if done.returncode != 0:
        return None, f"could not build the reader: {(done.stderr or done.stdout).strip()[:300]}"
    partial.replace(built)
    logger.info("Grounder direct: built the reader")
    return built, ""


def _other_host(url: str) -> str:
    """The same address on the site's other host: ``www.`` added to a bare domain, or taken off a ``www.`` one; "" for any other host."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if not host or parts.port or parts.username:
        return ""
    other = host[4:] if host.startswith("www.") else f"www.{host}" if host.count(".") == 1 else ""
    return parts._replace(netloc=other).geturl() if other else ""


def read(url: str, signed_out: bool = False) -> dict[str, Any]:
    """Read one page at the exact address a browser would end up at, signed in where the user saved a sign-in for its site, or logged out when ``signed_out``
    (the user asked for that). Signing in is optional, so a saved sign-in that cannot be used never stops a read: the page is read logged out instead and says
    why (``sign_in_skipped``). When the page does not load at all (not a site's answer such as a 403 or a check, which stands), the same address on the site's
    other host, ``www.`` or bare, is read once instead, and the page says so (``resolved_from``). Nothing else is tried."""
    sync_soon()
    page = _read_at(url, signed_out)
    problem = str(page.get("problem") or "")
    if not signed_out and "/search-login" in problem:  # the saved sign-in is broken or missing: read logged out
        page = _read_at(url, True)
        page["sign_in_skipped"] = problem
        problem = str(page.get("problem") or "")
    if problem.startswith(("load failed", "no answer", "the reader returned nothing")) and (other := _other_host(url)):
        second = _read_at(other, signed_out or bool(page.get("sign_in_skipped")))
        if not second.get("problem"):
            second["resolved_from"] = url
            for key in ("sign_in_skipped",):
                if page.get(key):
                    second[key] = page[key]
            return second
    if signed_out:
        page["signed_out_asked"] = True
    return page


def _read_at(url: str, signed_out: bool = False) -> dict[str, Any]:
    """Read one page. Always a dict: ``url``, ``status``, ``title``, ``text``, ``chars``, ``links``, ``ms``, ``user_agent`` and, when it was not read, ``problem``."""
    if sys.platform != "darwin":
        return {"url": url, "problem": "Grounder direct reads with Safari's engine on a Mac, and this machine is not a Mac"}
    agent = user_agent()
    if not agent:
        return {"url": url, "problem": "Safari is not installed here, so there is no Safari user agent to read with"}
    host = (urlparse(url).hostname or "").lower()
    with _lock:
        helper, why = _helper()
        if helper is None:
            return {"url": url, "problem": why, "user_agent": agent}
        wait = SAME_HOST_GAP_S - (time.monotonic() - _last_read.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        started = time.monotonic()
        try:
            done = subprocess.run([*_reader_command(helper), url, agent, str(MAX_CHARS)], capture_output=True, text=True, timeout=READ_TIMEOUT_S,
                                  env={**minimal_env(), **reader_env(), **({"GROUNDER_DIRECT_SIGNED_OUT": "1"} if signed_out else {})})  # the resolver chosen; no sign-in when asked
        except subprocess.TimeoutExpired:
            done = None
        _last_read[host] = time.monotonic()
    took = int((time.monotonic() - started) * 1000)
    if done is None:
        return {"url": url, "problem": f"no answer in {READ_TIMEOUT_S} s", "ms": took, "user_agent": agent}
    try:
        page = json.loads((done.stdout or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return {"url": url, "problem": f"the reader returned nothing ({(done.stderr or '').strip()[:200]})", "ms": took, "user_agent": agent}
    page = page if isinstance(page, dict) else {}
    page.update(ms=took, user_agent=agent)
    page.setdefault("url", url)
    status = int(page.get("status") or 0)
    gate = GATES.search(str(page.get("text") or "")[:3000])
    if page.get("error"):
        page["problem"] = str(page["error"])
    elif gate:
        page["problem"] = f"the site showed a check instead of the page (HTTP {status}): \"{gate.group(0)}\". Nothing was done to get past it"
    elif status >= 400:
        page["problem"] = f"the site answered HTTP {status}"
    elif not str(page.get("text") or "").strip():
        blank = not str(page.get("title") or "").strip() and len(page.get("links") or []) <= 1
        page["problem"] = (f"the site served an empty page that only runs a script (HTTP {status}), which is how a bot check such as Akamai's looks. "
                           "Nothing was done to get past it. A sign-in saved with /search-login, where the user passes the check themselves, may get through"
                           if blank else f"the page showed no text when it was read (HTTP {status}); it may build its text with scripts that had not finished")
    if page.get("sign_in_not_refreshed"):
        logger.warning("Grounder direct: the %s sign-in was used but not saved back: %s", page.get("signed_in"), page["sign_in_not_refreshed"])
    logger.info("Grounder direct: %s read in %d ms, HTTP %s%s%s", host, took, status, f", signed in to {page['signed_in']}" if page.get("signed_in") else "",
                ", not read" if page.get("problem") else "")
    return page


def header(page: dict[str, Any]) -> str:
    """The first line of a page's text, so the model can tell the user how it was read."""
    host = urlparse(str(page.get("url") or "")).hostname or "the site"
    cut = f", first {MAX_CHARS} of {page['chars']} characters" if int(page.get("chars") or 0) > MAX_CHARS else ""
    session = (f"signed in to {page['signed_in']} with the sign-in the user saved ({page.get('cookies_sent', 0)} cookies sent)" if page.get("signed_in")
               else "a fresh logged-out session")
    moved = f" ({urlparse(str(page['resolved_from'])).hostname} did not load, so its other host was read)" if page.get("resolved_from") else ""
    moved += ", its name looked up through Grounder's resolver" if page.get("resolver") == "grounder" else ""
    if page.get("sign_in_skipped"):
        moved += ", logged out because the saved sign-in for it could not be used (/search-login can save it again)"
    elif page.get("signed_out_asked"):
        moved += ", logged out as the user asked"
    charge = ("Found by a Grounder direct search. Reading the page on this Mac used no Grounder credits. " + SEARCH_PRICE
              if page.get("found_by_search") else "No search provider was called, and no search provider or Grounder credits were used.")
    return (f"[{NAME}: read directly from {host}{moved} on this Mac with Safari's user agent (Safari {safari_version()}), {session}, "
            f"HTTP {page.get('status')}, {page.get('ms')} ms{cut}. {charge}]")


TURN_NOTE = ("[Grounder direct is the user's search choice. For a question, use web_search to find and read the source's page. "
             "Pass URLs the user provides, or use grounder_search with site and query when the user writes a site's host without a page's address. "
             "Reading a page named by address is free. " + SEARCH_PRICE + "]")
NO_ADDRESS = "No page address was provided. Give a specific address to read, or search for the page using the question."

def turn_note(user_message: Any = None, **_: Any) -> Optional[dict[str, str]]:
    if not reads_pages():
        return None
    from . import direct_formats
    signed_out = " The user asked to read logged out. Pass signed_in false to grounder_search." if direct_formats.signed_out_words(user_message) else ""
    return {"context": TURN_NOTE[:-1] + direct_formats.turn_words(direct_formats.from_words(user_message)) + signed_out + "]"}


def not_read(url: str, problem: str) -> str:
    return f"{NAME} did not read {url}: {problem}. Tell the user what happened and ask before making another search."


# Saved sign-ins. The reader holds the values; this side only names sites, opens the sign-in window and lists or forgets what the keychain holds.

# Endings under which a site is three labels, not two (``bbc.co.uk``, not ``co.uk``). Not the whole public suffix list: the common country second levels.
TWO_LABEL_ENDINGS = frozenset("""
    co.uk org.uk ac.uk gov.uk me.uk ltd.uk plc.uk net.uk sch.uk nhs.uk
    com.au net.au org.au edu.au gov.au asn.au id.au co.nz net.nz org.nz govt.nz ac.nz
    co.jp ne.jp or.jp ac.jp go.jp co.kr or.kr ne.kr ac.kr go.kr
    com.cn net.cn org.cn gov.cn edu.cn com.hk org.hk net.hk com.tw org.tw net.tw idv.tw com.sg org.sg net.sg edu.sg gov.sg
    co.in net.in org.in firm.in gen.in ind.in ac.in gov.in com.my net.my org.my com.ph com.vn com.pk co.id or.id ac.id co.th in.th ac.th
    com.br net.br org.br gov.br com.mx org.mx gob.mx com.ar org.ar gob.ar com.co net.co org.co com.pe com.ve com.uy com.ec com.bo com.py
    co.za org.za gov.za ac.za com.ng com.eg co.ke or.ke co.il org.il ac.il gov.il com.tr org.tr net.tr gov.tr com.sa com.qa com.kw ae.org
    co.at or.at ac.at com.pl net.pl org.pl com.ua org.ua com.ru org.ru net.ru com.es org.es com.pt com.gr com.cy co.hu
    github.io gitlab.io herokuapp.com vercel.app netlify.app pages.dev workers.dev blogspot.com wordpress.com substack.com tumblr.com
""".split())


def site_of(raw: str) -> tuple[str, str]:
    """The site a sign-in is for, and the address its sign-in window opens at; or "" and why not. Whatever the user types is cleaned to the site itself, so one
    sign-in covers every subdomain and every page of it: ``https://old.reddit.com/r/x?sort=new``, ``www.Reddit.com``, ``*.reddit.com`` and ``reddit.com/login``
    are all ``reddit.com``; ``news.bbc.co.uk`` is ``bbc.co.uk``. The window opens at the page typed, over https."""
    typed = (raw or "").strip().strip("<>\"'`").strip()
    if not typed:
        return "", "name a site, like reddit.com"
    parsed = urlparse(typed if "://" in typed else f"https://{typed.lstrip('*.')}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or not SITE.match(host) or not re.search(r"[a-z]", host.rsplit(".", 1)[-1]):
        return "", f"\"{raw}\" is not a site; give one like reddit.com, or the address of one of its pages"
    labels = host.split(".")
    site = ".".join(labels[-3:]) if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LABEL_ENDINGS else ".".join(labels[-2:])
    start = f"https://{host}{parsed.path or '/'}" + (f"?{parsed.query}" if parsed.query else "")
    return site, start


def sign_in(raw: str, wait: bool = False) -> str:
    """Open a window on this Mac at the site's own pages, signed in as the keychain last saw it, for the user to sign in and click Save sign-in. From Hermes it opens
    and this returns at once; from a terminal (``wait``) it returns once the window closes, saying what was saved."""
    site, start = site_of(raw)
    if not site:
        return start
    agent = user_agent()
    if not agent:
        return "Safari is not installed here, so there is no Safari user agent to sign in with"
    helper, why = _helper()
    if helper is None:
        return why
    command = [*_reader_command(helper), "--signin", site, agent, start]
    env = {**minimal_env(), **reader_env()}  # the window looks the site up through the same resolver reads use
    if not wait:
        window = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        threading.Thread(target=lambda: (window.wait(), sync_soon(True)), daemon=True).start()  # once the window closes, the account hears of what was saved
        return (f"A window opened on this Mac at {start}. Sign in to {site} there, then click Save sign-in (Command-S). From then on {NAME} reads {site}, "
                f"every subdomain of it and every page, signed in, with your Safari's user agent; its cookies stay sealed on this Mac and go only to {site}. "
                f"`/search-login` lists sign-ins, `/search-logout {site}` forgets this one.")
    done = subprocess.run(command, capture_output=True, text=True, env=env)
    return _saved(done, site)


def paste(raw: str, header: Optional[str] = None) -> str:
    """Save a Cookie header for a site that will not sign in inside the window (one that refuses an embedded browser). The reader asks for it at a hidden prompt on
    this terminal, so it never passes through Python. ``header`` is for tests only: it goes to the reader's standard input."""
    site, why = site_of(raw)
    if not site:
        return why
    helper, why = _helper()
    if helper is None:
        return why
    if header is None:
        return _saved(subprocess.run([*_reader_command(helper), "--paste", site], stdout=subprocess.PIPE, text=True, env=minimal_env()), site)
    return _saved(subprocess.run([*_reader_command(helper), "--paste", site], input=header, capture_output=True, text=True, env=minimal_env()), site)


def _saved(done: subprocess.CompletedProcess, site: str) -> str:
    try:
        result = json.loads((done.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return f"The reader returned nothing for {site} (exit {done.returncode})"
    if result.get("error"):
        return f"Not saved: {result['error']}"
    if result.get("canceled"):
        return f"Nothing saved for {site}: the window was closed without Save sign-in."
    return f"Saved a sign-in for {site}: {result.get('saved')} cookies ({', '.join(result.get('names') or [])}), sealed under a key in your login keychain."


def sign_out(raw: str) -> str:
    site, why = site_of(raw)
    if not site:
        return why
    gone, failed = False, []
    for service in (SERVICE, LEGACY_SERVICE):
        done = subprocess.run([SECURITY, "delete-generic-password", "-s", service, "-a", site], capture_output=True, text=True, env=minimal_env())
        if done.returncode == 0:
            gone = True
        elif done.returncode != 44:
            failed.append(f"security exited {done.returncode} ({done.stderr.strip()[:200]})")
    sealed = STORE / f"{site}.sealed"
    if sealed.exists():
        sealed.unlink()
        gone = True
    if failed:
        return f"Could not forget the {site} sign-in: {'; '.join(failed)}"
    return f"Forgot the {site} sign-in: {NAME} reads {site} logged out again." if gone else f"No sign-in was saved for {site}."


def listed(timeout: float = 30) -> Optional[list[tuple[str, str]]]:
    """Each site with a saved sign-in, and when it was saved, read from Grounder's own ``sign-ins`` store (one ``<site>.sealed`` file each), never from the keychain:
    the plugin does not list other apps' keychain items, not even their names. Nothing is read from a file. ``None`` when the directory could not be read: that is
    not an empty list (a missing directory is: nothing was ever saved), and nothing may treat it as one. ``timeout`` is kept for callers; a directory listing needs none."""
    try:
        names = sorted(STORE.iterdir()) if STORE.exists() else []
    except OSError:
        return None
    found = []
    for entry in names:
        site = entry.name[: -len(".sealed")] if entry.name.endswith(".sealed") else ""
        if not site or not entry.is_file():
            continue
        try:
            when = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            continue
        found.append((site, f"saved {when} by this Mac"))
    return found


def sites() -> list[tuple[str, str]]:
    """``listed``, with a keychain that could not be listed shown as none."""
    return listed() or []


# The account's copy of this list (GRO-933): the site names, which Mac and when each was saved, listed on grounder.net so a sign-in can be removed from there;
# the cookies never leave this Mac. ``sync`` reports the list and applies the removals the account asks for. It is best effort and never fails what called it.
ACCOUNT_URL = "https://grounder.net/account/routing#route-signins"
SYNC_EVERY_S = 600  # a read syncs first when the last sync was longer ago than this
SYNC_TIMEOUT_S = 3  # one request, and the keychain listing a sync of a read waits for
MAX_NAME = 60
UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SAVED_NOTE = re.compile(r"^saved ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z) by ")
BACKGROUND_LISTING_S = 30  # a sync on a thread of its own can wait for a slow keychain
# Set by provider.py: ``account_sender(body, timeout)`` is the answer of ``PUT /v1/direct/sign-ins`` (a dict) or None when it failed, and ``credential_probe()`` is
# whether the plugin has anything to send it with (a search key, or a Hermes sign-in): asked before the keychain is listed, so a plugin with neither lists nothing.
account_sender: Optional[Callable[[dict[str, Any], float], Optional[dict[str, Any]]]] = None
credential_probe: Callable[[], bool] = lambda: True


def support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Grounder Direct"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.replace(partial, path)


def device_id() -> str:
    """This Mac's id for the account: a random lowercase UUID made on first need and kept in ``device.json``, shared with grounder-direct."""
    path = support_dir() / "device.json"
    try:
        known = json.loads(path.read_text()).get("id")
        if isinstance(known, str) and UUID_V4.match(known):
            return known
    except (OSError, ValueError, AttributeError):
        pass
    return claim_device_id(str(uuid.uuid4()))


def claim_device_id(made: str) -> str:
    """Make ``made`` this Mac's id unless one is already kept, and return the one that is. The file is written whole and linked into place, which fails when the file
    exists, so the plugin and grounder-direct starting together never keep two ids. A file that exists but holds no id is replaced."""
    path = support_dir() / "device.json"
    try:
        known = json.loads(path.read_text()).get("id")
        if isinstance(known, str) and UUID_V4.match(known):
            return known
    except (OSError, ValueError, AttributeError):
        pass
    if path.exists():
        _write_private(path, json.dumps({"id": made}))
        return made
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps({"id": made}))
    try:
        os.link(partial, path)
        return made
    except FileExistsError:
        try:
            known = json.loads(path.read_text()).get("id")
            return known if isinstance(known, str) and UUID_V4.match(known) else made
        except (OSError, ValueError, AttributeError):
            return made
    finally:
        partial.unlink(missing_ok=True)


def device_name() -> str:
    """The name the account shows for this Mac: its computer name, else its host name, trimmed and cut to 60 characters."""
    for command in (["/usr/sbin/scutil", "--get", "ComputerName"], ["/bin/hostname", "-s"]):
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=5, env=minimal_env())
        except (OSError, subprocess.TimeoutExpired):
            continue
        name = done.stdout.strip()
        if done.returncode == 0 and name:
            return name[:MAX_NAME].strip()
    return "Mac"


def saved_at(note: str) -> Optional[str]:
    """When a sign-in was saved, from its note (``saved <time> by <how>``), or None."""
    found = SAVED_NOTE.match(note or "")
    if not found:
        return None
    text = found.group(1)
    try:  # a time that is no real instant (2026-02-30) would be refused by the account, and with it the whole report
        datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]), int(text[11:13]), int(text[14:16]), int(text[17:19]))
    except ValueError:
        return None
    return text


def sync_body(identity: str, name: str, found: list[tuple[str, str]]) -> dict[str, Any]:
    """The body of ``PUT /v1/direct/sign-ins`` for a listing: this Mac and each site once, in order, with when it was saved. A name that is no site is left out."""
    rows, seen = [], set()
    for site, note in found:
        if site in seen or len(site) > 253 or not SITE.match(site) or site != site.lower():
            continue
        seen.add(site)
        rows.append({"site": site, "saved_at": saved_at(note)})
    return {"device": {"id": identity, "name": (name or "Mac").strip()[:MAX_NAME]}, "sites": sorted(rows, key=lambda row: row["site"])}


# A read done on this Mac, told to the account (GRO-989): the read itself reaches no server, so the search records and PostHog never saw it. The plugin posts the prompt and each
# page's address (host and path only) with its own key, and only while the account's saved setting says ``mac_reads.record`` is true. The reader binary never gets a key.
READ_ADDRESS = re.compile(r"https?://[^\s<>\"'`]+", re.I)
MAX_READ_PAGES = 10
MAX_READ_PROMPT = 2000


def host_path(address: str) -> Optional[str]:
    """An address as Grounder keeps it: scheme, host and path, with no port, user name, query or fragment; None when it is no http(s) address with a host."""
    try:
        parts = urlparse(address)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return f"{parts.scheme.lower()}://{host}{parts.path}"


def read_prompt(query: str) -> str:
    """The words with every address in them cut to host and path, at most 2000 characters."""
    text = query
    for address in sorted(set(READ_ADDRESS.findall(query)), key=len, reverse=True):
        text = text.replace(address, host_path(address) or "")
    return text.strip()[:MAX_READ_PROMPT]


def read_body(query: str, addresses: list[str]) -> Optional[dict[str, Any]]:
    """The body of ``POST /v1/direct/reads``, or None when there is nothing to send: no words, or no address left after the cut. At most 10 addresses, each once."""
    pages: list[str] = []
    for address in addresses:
        cut = host_path(address)
        if cut and len(cut) <= 2000 and cut not in pages:
            pages.append(cut)
    prompt = read_prompt(query)
    return {"prompt": prompt, "pages": pages[:MAX_READ_PAGES]} if prompt and pages else None


# Set by the plugin (provider.py): whether the account asks this Mac to record its reads, and the call that tells it. Neither is set when this module runs on its own.
reads_recording: Callable[[], bool] = lambda: False
read_sender: Optional[Callable[[dict[str, Any], float], bool]] = None
spawn: Callable[[Callable[[], None]], None] = lambda work: threading.Thread(target=work, daemon=True).start()  # tests run the work at once


def report_read(query: str, addresses: list[str]) -> None:
    """Tell the account about a read done here, in the background, when it asks for them. Never raises and never delays the read. ``addresses`` are the ones tried (not one refused)."""
    try:
        body = read_body(query, addresses)
        if body is None or read_sender is None:
            return

        def work() -> None:
            try:
                if reads_recording() and read_sender is not None:
                    read_sender(body, SYNC_TIMEOUT_S)
            except Exception as error:
                logger.info("A read was not told to the account: %s", error)

        spawn(work)
    except Exception as error:
        logger.info("A read was not told to the account: %s", error)


def removal_site(raw: Any) -> str:
    """The site a removal the account sent names, or "" when it is not one this Mac could have saved: only a name ``site_of`` would give back is used, so a name with
    a slash, a dot-dot, a capital or anything else never reaches a path."""
    if not isinstance(raw, str) or len(raw) > 253 or raw != raw.strip() or raw != raw.lower() or not SITE.match(raw):
        return ""
    return raw if site_of(raw)[0] == raw else ""


def account_line(connected: bool) -> str:
    """The line under the list of sign-ins: where else the list is."""
    if connected:
        return f"This list is also on your Grounder account, under Saved sign-ins on the Routing page ({ACCOUNT_URL}); the cookies stay on this Mac."
    return "This list is on this Mac only."


def last_sync() -> Optional[datetime]:
    try:
        return datetime.strptime(json.loads((support_dir() / "sync.json").read_text()).get("last_sync_at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def sync_due() -> bool:
    last = last_sync()
    return last is None or (datetime.now(timezone.utc) - last).total_seconds() > SYNC_EVERY_S


_sync_lock = threading.Lock()
_last_attempt = -1.0  # when this process last started a background sync (monotonic), so a read that finds no credential does not list the keychain again and again
RETRY_S = 60


def sync(listing_timeout: float = 30) -> str:
    """Report this Mac's sign-ins to the account and forget the ones the account asks to remove, then report again so the site sees them gone. Says what happened.
    With no credential nothing is sent; with a keychain that cannot be listed nothing is sent either (an empty report would delete every row of this Mac)."""
    if account_sender is None or not _has_credential():
        return "No Grounder account is connected here, so the sign-in list stays on this Mac only."
    with _sync_lock:
        removed: list[str] = []
        for _ in range(3):  # a report, and at most two more after removals
            found = listed(listing_timeout)
            if found is None:
                return "Skipped: the login keychain could not be listed, and an empty report would delete this Mac's rows on your account."
            try:
                answer = account_sender(sync_body(device_id(), device_name(), found), SYNC_TIMEOUT_S)
            except Exception as error:  # a sync never fails what called it
                logger.warning("Could not sync the saved sign-ins: %s", error)
                answer = None
            if answer is None:
                return "Nothing was sent: no Grounder account is connected here, or it could not be reached. The sign-ins stay as they are."
            _write_private(support_dir() / "sync.json", json.dumps({"last_sync_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
            held = {site for site, _ in found}
            asked = answer.get("remove") if isinstance(answer.get("remove"), list) else []
            todo = []
            for raw in asked:
                site = removal_site(raw)
                if site and site in held:
                    todo.append(site)
                else:
                    logger.warning("Ignored a sign-in removal the account sent for %r", raw if isinstance(raw, str) else type(raw).__name__)
            todo = [site for site in dict.fromkeys(todo) if not sign_out(site).startswith("Could not")]
            if not todo:
                break
            removed += todo
        return (f"Synced {len(found)} saved sign-in{'s' if len(found) != 1 else ''} with your Grounder account"
                + (f"; forgot {', '.join(removed)} as asked on grounder.net." if removed else "."))


def sync_soon(force: bool = False) -> None:
    """Sync in the background (the credential probe runs there too, as it can be a network call), for the first use in this process, after a sign-in or sign-out (``force``) and before a read when the last sync is old. Never raises."""
    global _last_attempt
    try:
        now = time.monotonic()
        first = _last_attempt < 0
        if account_sender is None or not (force or first or (now - _last_attempt > RETRY_S and sync_due())):
            return
        _last_attempt = now
        threading.Thread(target=_quiet_sync, daemon=True).start()
    except Exception as error:
        logger.warning("Could not start the sign-in sync: %s", error)


def _has_credential() -> bool:
    try:
        return bool(credential_probe())
    except Exception:
        return False


def _quiet_sync() -> None:
    try:
        said = sync(BACKGROUND_LISTING_S)
        if said.startswith("Skipped"):
            logger.warning("%s", said)
    except Exception as error:
        logger.warning("Could not sync the saved sign-ins: %s", error)


def sites_text() -> str:
    found = sites()
    if not found:
        return f"No saved sign-ins: {NAME} reads every site logged out. `/search-login <site>` saves one."
    return "\n".join([f"{NAME} reads these sites signed in:", *(f"  {site}: {note}" for site, note in found), account_line(_connected())])


def _connected() -> bool:
    """Whether a sync has worked, which is what says the list is on the account (a key that was revoked, or never sent, is not)."""
    return last_sync() is not None


DEV_COMMANDS = ("signin", "signout", "sites", "paste")


def command(words: list[str]) -> str:
    """The older ``/search direct signin|signout|sites`` forms, kept working as aliases of ``/search-login`` and ``/search-logout``, which are the ones documented.
    A Cookie header is pasted only in a terminal, so it never enters a chat."""
    verb, rest = (words[0].lower(), words[1:]) if words else ("", [])
    if verb == "sites":
        return sites_text()
    if verb == "paste":
        return (f"Paste a Cookie header in a terminal, not here, so it stays out of the chat: `python3 {Path(__file__).resolve()} paste "
                f"{rest[0] if rest else '<site>'}`")
    if not rest:
        command = LOGOUT_COMMAND if verb == "signout" else LOGIN_COMMAND
        return f"Usage: `/{command} <site>`, for example `/{command} reddit.com`."
    return sign_in(rest[0]) if verb == "signin" else sign_out(rest[0])


# Which resolver reads look sites up through: the user's own (this Mac's, with any filter on it or its network), or Grounder's. Pages always go straight from
# this Mac to each site, encrypted end to end; Grounder's resolver learns only which hosts are read.
RESOLVER_KEY = "direct_resolver"  # web.grounder.direct_resolver in config.yaml
RESOLVERS = ("system", "grounder")
GROUNDER_RESOLVER = "https://api.grounder.net/v1/dns-query"  # DNS over HTTPS, JSON answers (application/dns-json)


def resolver() -> str:
    """The resolver reads use here: "grounder" when the user picked Grounder's, else "system" (their own)."""
    try:
        from hermes_cli.config import load_config
        web = load_config().get("web")
    except Exception as error:
        logger.warning("Could not read the Grounder direct resolver setting: %s", error)
        return "system"
    section = web.get(SETTINGS_KEY) if isinstance(web, dict) else None
    return "grounder" if isinstance(section, dict) and section.get(RESOLVER_KEY) == "grounder" else "system"


def set_resolver(value: str) -> None:
    if value not in RESOLVERS:
        raise ValueError(f"resolver must be one of {', '.join(RESOLVERS)}")
    save_setting(RESOLVER_KEY, value)


def reader_env() -> dict[str, str]:
    """What the reader's environment needs for the resolver picked: Grounder's resolver's address, or nothing for the user's own."""
    return {"GROUNDER_DIRECT_RESOLVER": GROUNDER_RESOLVER} if resolver() == "grounder" else {}


LOGIN_COMMAND = "search-login"  # /search-login; Hermes names it search_login on Telegram, which allows no hyphen
LOGOUT_COMMAND = "search-logout"  # /search-logout; search_logout on Telegram


def login_command(raw_args: str) -> str:
    """``/search-login <site>``: save a sign-in for a site. Whatever is typed (an address, a subdomain, a page) is cleaned to the site itself (``site_of``), and the
    sign-in then covers every subdomain and page of it. With nothing typed, it says how and lists the sign-ins saved."""
    typed = (raw_args or "").strip()
    if not typed:
        return (f"Usage: `/{LOGIN_COMMAND} <site>`, for example `/{LOGIN_COMMAND} reddit.com` or `/{LOGIN_COMMAND} https://old.reddit.com/login`. "
                f"It opens the site's sign-in page in a window on this Mac.\n{sites_text()}")
    return sign_in(typed.split()[0])


def logout_command(raw_args: str) -> str:
    """``/search-logout <site>``: forget the sign-in saved for a site, cleaned the same way (``/search-logout old.reddit.com`` forgets reddit.com's). With nothing
    typed, it says how and lists the sign-ins saved."""
    typed = (raw_args or "").strip()
    if not typed:
        return f"Usage: `/{LOGOUT_COMMAND} <site>`, for example `/{LOGOUT_COMMAND} reddit.com`.\n{sites_text()}"
    said = sign_out(typed.split()[0])
    sync_soon(True)
    return said


def register_login(ctx) -> None:
    """Register ``/search-login`` (``login_command``) and ``/search-logout`` (``logout_command``) beside ``/search``."""
    ctx.register_command(LOGIN_COMMAND, lambda raw_args: login_command(raw_args), args_hint="<site>",
                         description=f"Save your sign-in for a site, so {NAME} reads all of it, every subdomain and page, signed in as you")
    ctx.register_command(LOGOUT_COMMAND, lambda raw_args: logout_command(raw_args), args_hint="<site>",
                         description=f"Forget your sign-in for a site, so {NAME} reads it logged out again")


def main(argv: list[str]) -> int:
    usage = "usage: direct_dev.py signin <site> | paste <site> | forget <site> | sites"
    if not argv or argv[0] not in ("signin", "paste", "forget", "sites") or (argv[0] != "sites" and len(argv) < 2):
        print(usage)
        return 64
    verb = argv[0]
    if verb == "sites":
        print(sites_text())
    elif verb == "forget":
        print(sign_out(argv[1]))
    elif verb == "signin":
        print("A window is open on this Mac; sign in there, then click Save sign-in.")
        print(sign_in(argv[1], wait=True))
    else:
        print(paste(argv[1]))  # the reader asks for the header itself, at a hidden prompt
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main(sys.argv[1:]))
