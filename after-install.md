# Grounder is installed

The next time Hermes starts, Grounder becomes this profile's `web_search` and `web_extract` backend, replacing any backend
chosen for them before. Each query and page address then goes to `api.grounder.net`. To use another backend, pick it in
`hermes tools`; that choice stands.

Searches work now with no account, as Grounder's free searches. To connect an account for more, ask the user one question: sign in
in the browser, paste a search key from https://grounder.net/account/keys, or stay on the free searches? Then do the rest.

- Browser sign-in: run `hermes auth add grounder:grounder`. Nothing needs setting first; the plugin has already saved Grounder's
  public sign-in client. It opens the browser on the machine Hermes runs on, so over SSH or in a messaging chat offer a key or the
  free searches instead.
- Search key: the user runs `hermes config set GROUNDER_API_KEY <their-key>` themselves, so the key is not in the chat.

Once connected, `/search` chooses how Grounder searches, from three modes:

- Grounder optimized: Grounder picks the provider for your question. This is the default.
- Grounder preset: Fast, Cheap, Live, Deep, Code or Balanced. Grounder picks the best provider for the preset.
- Grounder direct: you pick the provider to search with, and only it (what it finds is what you get, even when that is nothing), with one of its modes if you like.

`/search` also chooses which live-data APIs Grounder may use. Every mode returns the provider's own results, each uncut and not reranked, less any a site's rules refuse it. `/search_presets` is the shortcut
to the presets, and `/search optimized` goes back to Grounder optimized. On Slack, `/search` is Slack's own command: type `/hermes search`.
