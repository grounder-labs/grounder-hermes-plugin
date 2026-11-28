# Grounder is installed

Start Hermes Agent or run `/reload-mcp` followed by `/reload-skills` in an open chat. Grounder selects itself for web search or extract where you have no backend selected.

You can search free without an account. To connect an account, run `hermes auth add grounder:grounder` or set a key with `hermes config set GROUNDER_API_KEY <your-key>`. Make a key on the account page. [Grounder search keys.](https://grounder.net/account/keys) The first search after install says once which of these it ran on: free with no account, your account, or a wallet.

Use `/search provider` to search through sources Grounder chose for the question or `/search direct` to find and read the source's page on a Mac. The reader for macOS with Safari installs as https://grounder.net/docs/direct shows. A page named by address is read free. A Grounder direct search is one search, at the one price, even if its page could not be read.

`/search` (`/hermes search` on Slack) also sets the page format and resolver. Grounder direct reads every page logged out: it keeps no sign-ins or cookies. [Grounder direct documentation.](https://grounder.net/docs/direct)

Usage context is off by default. To share the model name and pseudonymous session and prompt identifiers for usage reporting, run `hermes config set web.grounder.share_usage_context true` and restart Hermes. [Privacy policy.](https://grounder.net/privacy)
