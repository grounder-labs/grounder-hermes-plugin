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

Once connected, `/search` chooses how Grounder searches: let Grounder pick for you based on your prompt, a preset (Fast, Cheap, Live, Deep, Code or Balanced), or a
provider to search with and only it (if it finds nothing, Grounder picks for you), with one of its modes if you like, and which live-data APIs it may use. `/search_presets` is the shortcut to the presets, and `/search auto`
lets Grounder pick again. On Slack, `/search` is Slack's own command: type `/hermes search`.
