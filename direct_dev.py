# Copyright 2026 Grounder Labs, Inc
# SPDX-License-Identifier: Apache-2.0

"""Grounder direct: go straight to the address the user gives, from this Mac, asking for it the way their Safari does.

Grounder direct reads the source’s page for a search, or the page named by address. The saved account choice takes precedence over the setting on this Mac.

Each read starts a fresh, empty session: no cookies and no saved sign-in, so every page is read logged out, asked for with the same Safari user agent. Grounder direct
no longer saves sign-ins (removed 2026-11-27); ``forget_saved_sign_ins`` deletes what an older version saved on this Mac. A site's own checks run as they would in any
browser; nothing here solves or skips one. Nothing a read returns is kept or sent anywhere. One read at a time.
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
import shutil
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
REPORT_TIMEOUT_S = 3  # the request that tells the account about a read
MAX_CHARS = 60000  # most characters of a page's text a read returns; Hermes cuts a page to the model's char_limit itself
READ_TIMEOUT_S = 45  # the helper stops itself at 30 s
BUILD_TIMEOUT_S = 240
SAME_HOST_GAP_S = 2.0  # least time between two reads of one site
GATES = re.compile(r"blocked by network security|prove your humanity|please wait for verification|verify (?:that )?you are (?:a )?human|"
                   r"just a moment\.\.\.|attention required|unusual traffic|are you a robot|access denied|log in to use old reddit", re.I)

SUPPORT = Path.home() / "Library" / "Application Support" / "Grounder Direct"
KEYCHAIN_SERVICES = ("grounder-direct", "grounder-direct-dev")  # the login keychain services older versions saved sign-in keys under
STORE = SUPPORT / "sign-ins"  # where older versions kept each site's sealed cookies
OLD_FILES = ("sync.json", "device.json")  # older versions' sign-in sync state and this Mac's id for the account's list
CLEANED = SUPPORT / ".sign-ins-removed"  # written once the cleanup has run
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
    the user's provider keys. The reader and the version check run code the pinned source's sha does not cover (the prebuilt grounder-direct)."""
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


def read(url: str) -> dict[str, Any]:
    """Read one page at the exact address a browser would end up at, logged out: no cookies and no saved sign-in. When the page does not load at all (not a site's answer
    such as a 403 or a check, which stands), the same address on the site's other host, ``www.`` or bare, is read once instead, and the page says so (``resolved_from``).
    Nothing else is tried."""
    page = _read_at(url)
    problem = str(page.get("problem") or "")
    if problem.startswith(("load failed", "no answer", "the reader returned nothing")) and (other := _other_host(url)):
        second = _read_at(other)
        if not second.get("problem"):
            second["resolved_from"] = url
            return second
    return page


def _read_at(url: str) -> dict[str, Any]:
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
                                  env={**minimal_env(), **reader_env(), "GROUNDER_DIRECT_SIGNED_OUT": "1"})  # the resolver chosen; the older installed reader still honours this word, so it reads logged out too
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
                           "Nothing was done to get past it."
                           if blank else f"the page showed no text when it was read (HTTP {status}); it may build its text with scripts that had not finished")
    logger.info("Grounder direct: %s read in %d ms, HTTP %s%s", host, took, status, ", not read" if page.get("problem") else "")
    return page


def header(page: dict[str, Any]) -> str:
    """The first line of a page's text, so the model can tell the user how it was read."""
    host = urlparse(str(page.get("url") or "")).hostname or "the site"
    cut = f", first {MAX_CHARS} of {page['chars']} characters" if int(page.get("chars") or 0) > MAX_CHARS else ""
    session = "a fresh logged-out session"
    moved = f" ({urlparse(str(page['resolved_from'])).hostname} did not load, so its other host was read)" if page.get("resolved_from") else ""
    moved += ", its name looked up through Grounder's resolver" if page.get("resolver") == "grounder" else ""
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
    return {"context": TURN_NOTE[:-1] + direct_formats.turn_words(direct_formats.from_words(user_message)) + "]"}


def not_read(url: str, problem: str) -> str:
    return f"{NAME} did not read {url}: {problem}. Tell the user what happened and ask before making another search."


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
    """The site an address belongs to, and the address cleaned to https; or "" and why not. Whatever is given is cleaned to the site itself:
    ``https://old.reddit.com/r/x?sort=new``, ``www.Reddit.com``, ``*.reddit.com`` and ``reddit.com/login`` are all ``reddit.com``; ``news.bbc.co.uk`` is ``bbc.co.uk``."""
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


def read_body(query: str, addresses: list[str], user_prompt: str = "") -> Optional[dict[str, Any]]:
    """The body of ``POST /v1/direct/reads``, or None when there is nothing to send: no words, or no address left after the cut. At most 10 addresses, each once.
    ``user_prompt`` is the user's own latest message (GRO-1170), cut the same way as the words; left out when Hermes gave none, so the read is recorded as having no user prompt."""
    pages: list[str] = []
    for address in addresses:
        cut = host_path(address)
        if cut and len(cut) <= 2000 and cut not in pages:
            pages.append(cut)
    prompt = read_prompt(query)
    if not (prompt and pages):
        return None
    said = read_prompt(user_prompt)
    return {"prompt": prompt, **({"user_prompt": said} if said else {}), "pages": pages[:MAX_READ_PAGES]}


# Set by the plugin (provider.py): whether the account asks this Mac to record its reads, and the call that tells it. Neither is set when this module runs on its own.
reads_recording: Callable[[], bool] = lambda: False
read_sender: Optional[Callable[[dict[str, Any], float], bool]] = None
spawn: Callable[[Callable[[], None]], None] = lambda work: threading.Thread(target=work, daemon=True).start()  # tests run the work at once


def report_read(query: str, addresses: list[str], user_prompt: str = "") -> None:
    """Tell the account about a read done here, in the background, when it asks for them. Never raises and never delays the read. ``addresses`` are the ones tried (not one refused)."""
    try:
        body = read_body(query, addresses, user_prompt)
        if body is None or read_sender is None:
            return

        def work() -> None:
            try:
                if reads_recording() and read_sender is not None:
                    read_sender(body, REPORT_TIMEOUT_S)
            except Exception as error:
                logger.info("A read was not told to the account: %s", error)

        spawn(work)
    except Exception as error:
        logger.info("A read was not told to the account: %s", error)


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


def _security(*args: str) -> int:
    return subprocess.run([SECURITY, *args], capture_output=True, text=True, timeout=30, env=minimal_env()).returncode


def forget_saved_sign_ins_soon() -> None:
    """Run ``forget_saved_sign_ins`` on a thread of its own, so a slow keychain never holds up the plugin's load."""
    threading.Thread(target=forget_saved_sign_ins, daemon=True).start()


def forget_saved_sign_ins() -> None:
    """Once, on a Mac: delete what an older version saved here, now that Grounder direct keeps no sign-ins: its sealed cookie directory, its sync files, and the
    login keychain items under its own services (``security delete-generic-password -s <service>``, repeated until the keychain reports none; no other app's items are
    listed or touched). Best effort, logged, never raises. A marker file says it has run."""
    if sys.platform != "darwin":
        return
    try:
        if CLEANED.exists():
            return
        if STORE.is_dir():
            shutil.rmtree(STORE)
        for name in OLD_FILES:
            (SUPPORT / name).unlink(missing_ok=True)
        deleted = 0
        for service in KEYCHAIN_SERVICES:
            for _ in range(500):  # one item a call; 44 is "no such item", the end
                code = _security("delete-generic-password", "-s", service)
                if code != 0:
                    break
                deleted += 1
        if STORE.exists():
            raise OSError("the sign-in directory is still there")
        SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
        CLEANED.write_text("")
        logger.info("Grounder direct: deleted the saved sign-ins an older version kept on this Mac (%d keychain items)", deleted)
    except Exception as error:
        logger.warning("Grounder direct: could not delete the saved sign-ins an older version kept on this Mac: %s", error)
