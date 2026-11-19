# Copyright 2026 Grounder Labs, Inc
# SPDX-License-Identifier: Apache-2.0

"""Grounder direct: go straight to the address the user gives, from this Mac, asking for it the way their Safari does.

Grounder direct is a mode of its own (``/search direct``: no search, only the addresses given) or goes with any other mode (``/search direct with-search``,
or Yes to the question /search asks after a mode). It is saved on this machine only (``web.grounder.direct`` in config.yaml), never on the Grounder account.

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
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

NAME = "Grounder direct"
SETTINGS_KEY, DIRECT_KEY, DEV_KEY = "grounder", "direct", "direct_dev"  # web.grounder.direct ("only", "with-search" or "off"); direct_dev is 0.8.0-dev's, still read
SAFARI_PLIST = Path("/Applications/Safari.app/Contents/Info.plist")
# Safari freezes the platform and WebKit parts of its user agent; only Version/ follows the installed Safari. Checked 2026-11-19 against the request Safari 27.2 sent.
SAFARI_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{version} Safari/605.1.15"
SOURCE = Path(__file__).with_name("direct_reader.swift")
# The prebuilt reader the one-line install puts on a Mac (grounder-direct, signed and notarized, which also carries the reader): preferred over building one.
INSTALLED = Path.home() / "Library" / "Application Support" / "Grounder Direct" / "bin" / "grounder-direct"
INSTALL_LINE = "curl -fsSL https://grounder.net/install | sh"
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


ONLY, ADDON, OFF = "only", "addon", "off"


def state() -> str:
    """How Grounder direct is set on this Mac: ``only`` (the mode by itself: no search, only the addresses given), ``addon`` (on top of the search mode: searches run
    as set, and every page and named site is read straight from its site) or ``off``. ``web.grounder.direct`` holds it (``only``, ``with-search`` or ``off``); without it,
    the dev builds' ``web.grounder.direct_dev`` (true is ``only``, ``"addon"`` the add-on)."""
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
        return ONLY if word == ONLY else ADDON if word in ("with-search", "add-on", "addon") else OFF
    value = section.get(DEV_KEY)
    if value is True or str(value).lower() == ONLY:
        return ONLY
    return ADDON if str(value).lower().replace("-", "") == ADDON else OFF


def is_on() -> bool:
    """Whether Grounder direct is the mode by itself (``only``)."""
    return state() == ONLY


def addon_on() -> bool:
    """Whether Grounder direct is on as an add-on to the search mode."""
    return state() == ADDON


def reads_pages() -> bool:
    """Whether pages are read straight from their sites: by itself or as the add-on."""
    return state() != OFF


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
    save_setting(DIRECT_KEY, ONLY if value == ONLY else "with-search" if value == ADDON else OFF)
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
    """Hermes's home: the launcher always sets ``HERMES_HOME`` (no published API names it)."""
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


_installed_ok: dict[str, bool] = {}


def _installed() -> bool:
    """Whether the installed grounder-direct exists and runs (it answers ``version``). Asked once a process."""
    key = str(INSTALLED)
    if key not in _installed_ok:
        ok = False
        if INSTALLED.is_file() and os.access(INSTALLED, os.X_OK):
            try:
                done = subprocess.run([key, "version"], capture_output=True, text=True, timeout=10)
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
    source changes; or None and why not (which names the one-line install)."""
    if _installed():
        return INSTALLED, ""
    helper, why = _build_helper()
    return (helper, why) if helper is not None else (None, f"{why}; the one-line install adds a prebuilt reader: {INSTALL_LINE}")


def _build_helper() -> tuple[Optional[Path], str]:
    try:
        source = SOURCE.read_bytes()
    except OSError as error:
        return None, f"the reader's source is missing ({error})"
    built = _home() / "cache" / "grounder-direct" / f"direct-reader-{hashlib.sha256(source).hexdigest()[:12]}"
    if built.exists():
        return built, ""
    try:  # a Mac without the command line tools has only a stub xcrun, which opens an install dialog: ask first
        tools = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        tools = None
    if tools is not None and tools.returncode != 0:
        return None, "this Mac has no Xcode command line tools, so the reader cannot be built here"
    built.parent.mkdir(parents=True, exist_ok=True)
    partial = built.with_suffix(".partial")
    try:
        done = subprocess.run(["xcrun", "swiftc", "-O", "-o", str(partial), str(SOURCE)], capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
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
                                  env={**os.environ, **reader_env(), **({"GROUNDER_DIRECT_SIGNED_OUT": "1"} if signed_out else {})})  # the resolver chosen; no sign-in when asked
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
    return (f"[{NAME}: read directly from {host}{moved} on this Mac with Safari's user agent (Safari {safari_version()}), {session}, "
            f"HTTP {page.get('status')}, {page.get('ms')} ms{cut}. No search provider was called, and no search provider or Grounder credits were used.]")


# Added to each user message while the mode is on (``turn_context``): the model turns a site the user names into that site's exact address and sends it through
# Grounder. No site is listed here; the address is the model's to work out from the message.
TURN_NOTE = (f"[{NAME} is the user's search mode: Grounder goes only to an exact web address and reads that page straight from its site, with no search. "
             "When the user names a site by its name rather than its address (any site, publication, forum, wiki or service), work out that site's exact address "
             "from their message yourself: the most specific page on that site that answers it, such as the site's own page for the topic, section, article or "
             "profile, or the site's own search page with their words in it, else its home page. Write it the way the site serves it in a browser: the "
             "site's real domain, https, and www. when the site uses it. Then call web_search with that full https address in the query, or web_extract with it. "
             "Use Grounder's web_search and web_extract for this, not a browser, a terminal or any other tool. If the user names no site and gives no address, "
             "ask which site to go to.]")

NO_ADDRESS = (f"{NAME} searches nothing: it goes only to an exact address, and this call had none, so nothing was read. If the user named a site, work out "
              "that site's exact address from their message (the most specific page on it that answers them, such as its page for the topic or its own search "
              "page with their words) and call web_search again with that full https address in the query. If they named no site, ask them which site to go to.")


ADDON_NOTE = ("[Grounder direct is on together with the user's Grounder search mode (their /search choice). Decide for this question which to use. "
              "If the user names a site by its name (any site, publication, forum, wiki or service) or gives an address, use Grounder direct: work out that "
              "site's exact address from their message yourself (the most specific page on it that answers them, such as its page for the topic, section, article or profile, or its own "
              "search page with their words, else its home page; written the way the site serves it in a browser: its real domain, https, and www. when the "
              "site uses it) and read it with web_search (the full https address in the query) or web_extract; Grounder "
              "reads it straight from the site. If they name no site, search with web_search as usual: Grounder searches in their mode. If the question is "
              "better answered by both, such as what a named site says plus wider context, or a claim on that site checked against other sources, do both and "
              "combine them in one answer, saying which parts came from the site itself and which from the search. Use Grounder's tools for all of it, not a "
              "browser, a terminal or any other tool.]")


def turn_note(user_message: Any = None, **_: Any) -> Optional[dict[str, str]]:
    """``pre_llm_call`` hook: while Grounder direct is on, by itself (``TURN_NOTE``) or as the add-on (``ADDON_NOTE``), add its note to the user's message, and,
    when their words pick a format (``direct_formats``), the ask to pass it and answer in it. Off, it adds nothing."""
    now = state()
    if now == OFF:
        return None
    from . import direct_formats
    note = TURN_NOTE if now == ONLY else ADDON_NOTE
    out = (" The user asked to read logged out: pages are read with no saved sign-in (grounder_search takes signed_in false for this; web_search and "
           "web_extract do it on their own for this message)." if direct_formats.signed_out_words(user_message) else
           " Signing in is optional: a page is read logged out unless the user saved a sign-in for its site, and logged out anyway if that sign-in cannot be used.")
    return {"context": note[:-1] + direct_formats.turn_words(direct_formats.from_words(user_message)) + out + "]"}


def not_read(url: str, problem: str) -> str:
    return (f"{NAME} did not read {url}: {problem}. It goes only to the address given and tries nothing else, so tell the user that, "
            "and that they can give another address or pick another mode with /search.")


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
    env = {**os.environ, **reader_env()}  # the window looks the site up through the same resolver reads use
    if not wait:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
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
        return _saved(subprocess.run([*_reader_command(helper), "--paste", site], stdout=subprocess.PIPE, text=True), site)
    return _saved(subprocess.run([*_reader_command(helper), "--paste", site], input=header, capture_output=True, text=True), site)


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
        done = subprocess.run([SECURITY, "delete-generic-password", "-s", service, "-a", site], capture_output=True, text=True)
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


def sites() -> list[tuple[str, str]]:
    """Each site with a saved sign-in, and when and how it was saved (the keychain item's comment). Names only: no value is read."""
    done = subprocess.run([SECURITY, "dump-keychain"], capture_output=True, text=True, timeout=30)
    found = []
    for item in done.stdout.split("keychain: ")[1:]:
        if f'"svce"<blob>="{SERVICE}"' not in item and f'"svce"<blob>="{LEGACY_SERVICE}"' not in item:
            continue
        account = re.search(r'"acct"<blob>="([^"]*)"', item)
        note = re.search(r'"icmt"<blob>="([^"]*)"', item)
        if account:
            found.append((account.group(1), note.group(1) if note else ""))
    return sorted(dict(found).items())


def sites_text() -> str:
    found = sites()
    if not found:
        return f"No saved sign-ins: {NAME} reads every site logged out. `/search-login <site>` saves one."
    return "\n".join([f"{NAME} reads these sites signed in:", *(f"  {site}: {note}" for site, note in found)])


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
    return sign_out(typed.split()[0])


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
