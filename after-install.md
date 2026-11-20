# Grounder is installed

Account: your searches are Grounder's free searches, with no account, unless this profile already has a search key or a sign-in, which an install or an update keeps. To search on your account instead, run `hermes auth add grounder:grounder` (it signs in through the browser), or `hermes config set GROUNDER_API_KEY <your-key>` with a key from https://grounder.net/account/keys. `/search` shows which you are on: with no account it answers with how to sign in, and with one it opens its menu.

A chat that is already open: it has this version's `/search` now. For this version's tools and command list, run `/reload-mcp`, then `/reload-skills`, in that chat, or start a new one with `/new`.

The next time Hermes starts, Grounder becomes this profile's `web_search` and `web_extract` backend, for each of the two that has no
backend chosen yet; a backend already chosen is left alone. Each query and page address Grounder serves then goes to `api.grounder.net`.
To use another backend, pick it in `hermes tools`; that choice stands.

Searches work now with no account, as Grounder's free searches. To connect an account for more, ask the user one question: sign in
in the browser, paste a search key from https://grounder.net/account/keys, or stay on the free searches? Then do the rest.

- Browser sign-in: run `hermes auth add grounder:grounder`. Nothing needs setting first; the plugin has already saved Grounder's
  public sign-in client. It opens the browser on the machine Hermes runs on, so over SSH or in a messaging chat offer a key or the
  free searches instead.
- Search key: the user runs `hermes config set GROUNDER_API_KEY <their-key>` themselves, so the key is not in the chat.

If the user asks about paying for each search from a crypto wallet instead (x402), point them to "Pay per search with a wallet" in the plugin README. They set `GROUNDER_X402_PRIVATE_KEY` themselves: never ask
for, repeat or handle a private key in the chat.

Once connected, `/search` chooses how Grounder searches, from four modes:

- Grounder optimized: Grounder picks the provider for your question. This is the default.
- Grounder preset: Fast, Cheap, Live, Deep, Code or Balanced. Grounder picks the best provider for the preset.
- Grounder pinned: you pick the search provider, the only search provider called (what it finds is what you get, even when that is nothing), with one of its modes if you like.
- Grounder direct: you name the page or the site, and Grounder reads it straight from that site, on the user's Mac, with their Safari's user agent: no search provider, no search, no fallback, and no search provider credits or Grounder credits used. As an add-on, a question that names no page is searched as the mode says and its top page (full page, summary) or top three sites (highlights) are read directly too. By itself (`/search direct`), or as an add-on to any of the three above (`/search direct with-search`; the menu asks "Also read sites you name directly, as you?" after a mode is picked); `/search direct off` turns it off.

Grounder direct needs macOS and Safari, and the reader program that `curl -fsSL https://grounder.net/install | sh` installs (no Xcode or command line tools needed; a Mac that has them can build the same reader itself). On a machine without them it reads nothing and does not fall back to Grounder's page reader: use one of the three search modes there. The user, not you, runs `/search-login <site>` (`/search_login` on Telegram) to save a sign-in for a site: it opens the site's own sign-in page in a window on their Mac, and the sealed sign-in goes only to that site (its cookies never leave the Mac; with a connected account, the site's name, the Mac and the save time are also listed on the user's Grounder account, where they can remove it and the Mac forgets it at its next sync). Never ask for a password or a cookie in the chat. `/search-logout <site>` (`/search_logout` on Telegram) forgets one. `/search-direct-format` sets how a page comes back: `full-page`, `highlights` (exact quotes) or `summary` (an AI summary by their own active model: one sentence on the page, then three to six bullets, with a fallback where you write it). The Settings section of `/search` (`/search settings`, or `/search resolver system|grounder`) picks the resolver direct reads look sites up through: the user's own, or Grounder's, for a site that blanks its page when their DNS filter blocks a script. See "Grounder direct" in the plugin README.

Your choices are saved on your Grounder account, the same setting as the Routing page on grounder.net, so a change in `/search` shows there and a change there shows in `/search`. Grounder direct and its format and resolver are the exception: they are saved on this Mac, in `config.yaml`, and never on your account. A search sends none of them: Grounder applies your saved setting, and a free search has no account to save one on. What the model passes to `grounder_search` applies to that search alone, but your choice wins: while you have chosen a preset or a provider, the model's own preset, provider, `exclusive` or `always` is not used, and a live-data API you set to Every call or Never keeps that setting. What only narrows a search (caps, a provider set to Off) is used.

A search sends no usage context (the model that asked for it, and a random token for its session and prompt) unless you turn it on with `hermes config set web.grounder.share_usage_context true`; see the README.

`/search` opens one menu: Let Grounder pick, A preset, A provider, Live-data APIs and Done, the current choice marked, and every screen has a Back row. Every list is one view and every row one line. A pick closes the menu and answers with what is set now: the mode, the provider and its mode, what it returns, and where it is saved. Live-data APIs sets each live-data API, in every mode, to Every call, Never or Grounder decides (the default: asked only when your question's features call for it). Every mode returns the provider's own results, each uncut and not reranked, less any a site's rules refuse it. Typed arguments skip the menu: `/search fast`, `/search exa:instant`, `/search optimized`, `/search apis`. On Slack, `/search` is Slack's own command: type `/hermes search`.

`/search`, `/search-direct-format`, `/search-login` and `/search-logout` are the plugin's commands, and they are registered only while Grounder is your web search backend. If you pick another backend in `hermes tools`, the commands go at the next start (search, the tools and the web provider are unchanged), and they come back when you pick Grounder again.
