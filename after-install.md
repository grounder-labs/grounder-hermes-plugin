# Grounder is installed

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

Once connected, `/search` chooses how Grounder searches, from three modes:

- Grounder optimized: Grounder picks the provider for your question. This is the default.
- Grounder preset: Fast, Cheap, Live, Deep, Code or Balanced. Grounder picks the best provider for the preset.
- Grounder direct: you pick the search provider, the only search provider called (what it finds is what you get, even when that is nothing), with one of its modes if you like.

Your choices are saved on your Grounder account, the same setting as the Routing page on grounder.net, so a change in `/search` shows there and a change there shows in `/search`. A search sends none of them: Grounder applies your saved setting, and a free search has no account to save one on. What the model passes to `grounder_search` applies to that search alone, but your choice wins: while you have chosen a preset or a provider, the model's own preset, provider, `exclusive` or `always` is not used, and a live-data API you set to Every call or Never keeps that setting. What only narrows a search (caps, a provider set to Off) is used.

A search sends no usage context (the model that asked for it, and a random token for its session and prompt) unless you turn it on with `hermes config set web.grounder.share_usage_context true`; see the README.

`/search` opens one menu: Let Grounder pick, A preset, A provider, Live-data APIs and Done, the current choice marked, and every screen has a Back row. Live-data APIs sets each live-data API, in every mode, to Every call, Never or Grounder decides (the default: asked only when your question's features call for it). Every mode returns the provider's own results, each uncut and not reranked, less any a site's rules refuse it. Typed arguments skip the menu: `/search fast`, `/search exa:instant`, `/search optimized`, `/search apis`. On Slack, `/search` is Slack's own command: type `/hermes search`.

`/search` is the plugin's only command, and it is registered only while Grounder is your web search backend. If you pick another backend in `hermes tools`, `/search` goes at the next start (search, the tools and the web provider are unchanged), and it comes back when you pick Grounder again.
