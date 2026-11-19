# Copyright 2026 Grounder Labs, Inc
# SPDX-License-Identifier: Apache-2.0

"""Grounder direct's three formats for a page read straight from its site: ``full-page``, ``highlights`` and ``summary``.

A ``grounder_search`` call names one with ``format`` (only with ``mode: "grounder-direct"``, or while the mode is on). In chat, the user's own words pick one
("full page", "highlights", "summary"): the turn note asks the model to pass it and to answer in it, and a ``web_search`` or ``web_extract`` with no format of its
own takes it from the user's latest message. Left out, a read is ``full-page``.

The plugin shapes what the read returns and says how to answer: ``full-page`` is the page's whole text; ``highlights`` is the page's passages that match the
question, picked here by the question's words and kept in page order; ``summary`` is the page's text with the ask to answer in a few sentences (the model writes
the summary: the plugin calls no model).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

FULL, HIGHLIGHTS, SUMMARY = "full-page", "highlights", "summary"
FORMATS = (FULL, HIGHLIGHTS, SUMMARY)
DIRECT_MODE = "grounder-direct"
# The words that pick each in chat. The earliest in the message wins.
WORDS = {FULL: re.compile(r"\bfull[\s-]?page\b|\bwhole page\b|\bentire page\b|\bfull text\b", re.I),
         HIGHLIGHTS: re.compile(r"\bhighlights?\b|\bkey (?:points|passages|quotes)\b", re.I),
         SUMMARY: re.compile(r"\bsummar(?:y|ies|ize|ise|ized|ised)\b|\btl;?dr\b|\bsum (?:it |this |that )?up\b", re.I)}
ANSWER = {FULL: "Format: full-page. Give the user the page's text as read, in full and in its order, without summarizing; leave out only menus, "
                "buttons and other navigation lines. Then the address.",
          HIGHLIGHTS: "Format: highlights. Answer with these quotes only, word for word as they appear on the page, as a bulleted list, each in quotation "
                      "marks. Add no words of your own to them. Then the address.",
          SUMMARY: "Format: summary. Give the user the AI summary below as it is, headed \"AI summary\". Then the address."}
SUMMARY_FALLBACK = ("Format: summary. The AI summary could not be made here ({why}), so write it yourself: an AI-generated summary of the page in three to six "
                    "sentences, headed \"AI summary\", covering what bears on the question. Then the address.")
SUMMARY_PROMPT = ("Summarize this web page for a reader. Start with one sentence saying what the page is and what it is about right now. Then give its "
                  "main points as three to six short bullet points, most important first, each a complete fact from the page with names and numbers kept. "
                  "{focus}Use only what the page says. Leave out menus, ads, sign-up prompts and other page furniture. Write no preamble and nothing about the "
                  "request itself.\n\nPage ({url}):\n{text}")
FOCUS = "The reader asked: \"{question}\". Put what the page says about that first, and if it says nothing about it, add one last line saying so. "
SUMMARY_TOKENS = 500
# Words in a request that only pick the tool, the format or the site, not a topic: a request made of these alone asks for the page as a whole.
_NOT_TOPIC = re.compile(r"\b(?:grounder|direct|full[\s-]?page|whole page|highlights?|summar(?:y|ies|ize|ise|ized|ised)|tl;?dr|sum (?:it |this |that )?up|read|"
                        r"open|go to|fetch|get|show me|please|use|using|with|from|on|of|the|a|site|page|website|front page|home ?page|latest|news)\b", re.I)


def topic(question: str, url: str = "") -> str:
    """What the request asks about beyond reading the page: its words less those that only name the tool, the format or the site; "" when nothing is left."""
    host = (re.sub(r"^www\.", "", (re.match(r"https?://([^/]+)", url or "") or [None, ""])[1]) or "").split(".")[0]
    rest = _NOT_TOPIC.sub(" ", re.sub(r"https?://\S+", " ", question or ""))
    if host:
        rest = re.sub(rf"\b{re.escape(host)}(?:\.\w+)*\b", " ", rest, flags=re.I)
    rest = " ".join(w for w in rest.split() if len(w) > 1)
    return rest if len(rest) >= 3 else ""
HIGHLIGHT_COUNT = 10
HIGHLIGHT_CHARS = 400
SUMMARY_CHARS = 20000  # the text a summary is written from
MIN_PASSAGE = 25  # a shorter line is a menu, a button or a label, not a passage
MIN_WORDS = 4
AD = re.compile(r"^(?:advertisement|sponsored|promoted|ad)\b\s*[:·•|-]?", re.I)
_host: dict = {"ctx": None}


def use_host(ctx) -> None:
    """Keep Hermes's plugin context, for ``ctx.llm`` (the AI summary on the user's own active model)."""
    _host["ctx"] = ctx


def ai_summary(text: str, question: str, url: str) -> tuple[str, str]:
    """``(summary, "")`` written by the user's active model through Hermes's ``ctx.llm``, or ``("", why not)``."""
    ctx = _host["ctx"]
    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None or not callable(getattr(llm, "complete", None)):
        return "", "this Hermes offers plugins no model"
    try:
        about = topic(question, url)
        done = llm.complete([{"role": "user", "content": SUMMARY_PROMPT.format(focus=FOCUS.format(question=about) if about else "", url=url, text=text[:SUMMARY_CHARS])}],
                            max_tokens=SUMMARY_TOKENS, purpose="Grounder direct: summary format")
    except Exception as error:  # the summary is a format, not the read: the page still comes back
        return "", f"the model call failed: {str(error)[:120]}"
    summary = str(getattr(done, "text", "") or "").strip()
    return (summary, "") if summary else ("", "the model returned nothing")


def forget_cached_searches() -> None:
    """Drop Hermes's in-process cache of Grounder's web_search answers, so a changed mode or format takes effect at the next search instead of
    after the cache's twenty minutes. Only what Hermes already loaded is touched: nothing is imported."""
    import sys
    memo = getattr(sys.modules.get("tools.web_result_cache"), "search_memo", None)
    store, lock = getattr(memo, "_store", None), getattr(memo, "_store_lock", None)
    if not isinstance(store, dict) or lock is None:
        return
    with lock:
        for key in [k for k in store if isinstance(k, tuple) and k and k[0] == "grounder"]:
            del store[key]


PAGE_FORMATS = (None, "markdown", "html", "text")  # the formats Hermes keys a cached page by


def forget_cached_pages() -> int:
    """Drop the pages Hermes cached from Grounder's own reader, so that once Grounder direct is on a page is read straight from its site and never served
    from an earlier read. Hermes keys a cached page by its address, format and backend ("grounder" in every mode). Only what Hermes already loaded is
    used; nothing is imported. Returns how many were dropped."""
    import sys
    cache = sys.modules.get("tools.web_result_cache")
    lock, load, save, digest = (getattr(cache, name, None) for name in ("_index_lock", "_load_index", "_save_index", "_url_digest"))
    if not all((lock, load, save, digest)):
        return 0
    dropped = 0
    with lock:
        index = load()
        for key, entry in list(index.items()):
            url = entry.get("url") if isinstance(entry, dict) else None
            if url and any(digest(url, fmt, "grounder") == key for fmt in PAGE_FORMATS):
                try:
                    Path(str(entry.get("file") or "")).unlink(missing_ok=True)
                except OSError:
                    pass
                del index[key]
                dropped += 1
        if dropped:
            save(index)
    return dropped
_STOP = set("the a an and or of to in on for with about what which who how why when where is are was were be been does did do this that these those "
            "it its from by at as into than then there their they them you your my me i we our us say says said tell give show page site please".split())


SETTINGS_KEY, FORMAT_KEY = "grounder", "direct_format"  # web.grounder.direct_format in config.yaml, set by /search-direct-format


def saved() -> Optional[str]:
    """The format ``/search-direct-format`` saved on this machine, or None."""
    try:
        from hermes_cli.config import load_config
        web = load_config().get("web")
    except Exception:
        return None
    section = web.get(SETTINGS_KEY) if isinstance(web, dict) else None
    return valid(section.get(FORMAT_KEY)) if isinstance(section, dict) else None


def save(fmt: str) -> None:
    from . import direct_dev
    direct_dev.save_setting(FORMAT_KEY, fmt)
    forget_cached_searches()


def chosen(asked: Optional[str] = None, words: object = None) -> str:
    """The format a read comes back in: the call's own, else the one the user's words name, else the one /search-direct-format saved, else full-page."""
    return valid(asked) or from_words(words) or saved() or FULL


def from_words(text: object) -> Optional[str]:
    """The format the user's words ask for, or None."""
    if not isinstance(text, str):
        return None
    found = [(match.start(), name) for name, pattern in WORDS.items() if (match := pattern.search(text))]
    return min(found)[1] if found else None


SIGNED_OUT = re.compile(r"\b(?:not|without)\s+(?:being\s+)?(?:logged|signed)\s+in\b|\blogged[\s-]?out\b|\bsigned[\s-]?out\b|"
                        r"\bwithout\s+(?:my\s+|the\s+|a\s+)?(?:sign[\s-]?in|login|log[\s-]?in|account|cookies)\b|\bincognito\b|\bprivate(?:ly| window)\b", re.I)


def signed_out_words(text: object) -> bool:
    """Whether the user's words ask for a page read logged out ("not logged in", "logged out", "without my sign-in")."""
    return isinstance(text, str) and bool(SIGNED_OUT.search(text))


def valid(value: object) -> Optional[str]:
    """``value`` as one of the three formats (any case, a space for the hyphen), or None."""
    word = str(value or "").strip().lower().replace(" ", "-").replace("_", "-")
    word = {"full": FULL, "fullpage": FULL, "page": FULL, "highlight": HIGHLIGHTS, "summarize": SUMMARY}.get(word.replace("-", ""), word)
    return word if word in FORMATS else None


def _stem(word: str) -> str:
    """A plural's singular, near enough to match "switches" with "switch" and "keyboards" with "keyboard"."""
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _STOP}


def _terms(question: str) -> set[str]:
    return _words(question)


def highlights(text: str, question: str) -> list[str]:
    """The page's passages that share the most words with the question, in page order; with no shared word, its first passages."""
    passages = list(dict.fromkeys(" ".join(p.split()) for p in re.split(r"\n+", text) if len(p.strip()) >= MIN_PASSAGE and len(p.split()) >= MIN_WORDS))
    ads = [p for p in passages if AD.match(p)]  # a page's ads, and the copies of their text it shows again without the label, are not the page's words
    passages = [p for p in passages if not AD.match(p) and not any(p in ad for ad in ads)]
    terms = _terms(question)
    scored = [(len(_words(p) & terms), i) for i, p in enumerate(passages)] if terms else []
    picked = sorted(i for score, i in sorted(scored, key=lambda s: (-s[0], s[1]))[:HIGHLIGHT_COUNT] if score) or list(range(min(HIGHLIGHT_COUNT, len(passages))))
    return [passages[i][:HIGHLIGHT_CHARS] + ("…" if len(passages[i]) > HIGHLIGHT_CHARS else "") for i in picked]


def shape(header: str, text: str, fmt: str, question: str, url: str = "") -> str:
    """A read's content in ``fmt``: the header line, how to answer, then the page's text (full-page), its quotes (highlights) or an AI summary of it
    (summary, written by the user's own active model; when that cannot be made, the page's text with the ask to write one)."""
    text = text.strip()
    if fmt == HIGHLIGHTS:
        found = highlights(text, question)
        body = "\n".join(f"- \"{p}\"" for p in found) if found else "(The page has no passage long enough to quote.)"
        return f"{header}\n{ANSWER[HIGHLIGHTS]}\n\nQuotes from the page, in page order:\n{body}"
    if fmt == SUMMARY:
        summary, why = ai_summary(text, question, url)
        if summary:
            return f"{header}\n{ANSWER[SUMMARY]}\n\nAI summary:\n{summary}"
        cut = f"\n\n(The first {SUMMARY_CHARS} characters of the page.)" if len(text) > SUMMARY_CHARS else ""
        return f"{header}\n{SUMMARY_FALLBACK.format(why=why)}\n\n{text[:SUMMARY_CHARS]}{cut}"
    return f"{header}\n{ANSWER[FULL]}\n\n{text}"


def turn_words(fmt: Optional[str]) -> str:
    """What the turn note adds when the user's words picked a format, or /search-direct-format saved one."""
    if not fmt:
        kept = saved()
        return f" The user's /search-direct-format is {kept}: pages come back in it, and answer in it. {ANSWER[kept]}" if kept else ""
    return (f" The user asked for the {fmt} format: call grounder_search with mode \"{DIRECT_MODE}\", format \"{fmt}\" and the address in urls, "
            f"and answer in that format. {ANSWER[fmt]}")
