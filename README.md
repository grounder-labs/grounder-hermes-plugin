# Grounder for Hermes Agent

Better web routing for Hermes Agent's `web_search` and `web_extract`. A search goes to sources Grounder chose by hand for your question. Grounder provider returns those sources' results. Grounder direct finds the source's page and reads it on your Mac with Safari. Reading a page named by address is free.

A Grounder direct search is one search, at the one price, even if its page could not be read. [See pricing.](https://grounder.net/pricing)

## Install

Needs Hermes Agent 0.22.3 or later.

```sh
hermes plugins install grounder-labs/grounder-hermes-plugin --enable
```

Grounder is also in the Hermes plugin catalog: `hermes plugins install grounder --enable` installs the version the catalog pins, which can be older than this repository's.

The text Hermes shows after an install starts with your account: free searches with no account, unless this profile already has a search key or a sign-in, and the command that signs in. After an install or an update, a chat that is already open has the new `/search` at once; for the new tools and command list, run `/reload-mcp`, then `/reload-skills`, in that chat, or start a new one with `/new`.

## Free searches

With no search key and no sign-in, a search runs within Grounder's free limits. When those are used up, Grounder explains how to connect an account or pay from a wallet.

## Connect your account

Connect your account to search past the free limits and to choose how Grounder searches
([`/search`](#choose-how-grounder-searches-search)). Connect it with a search key or by
signing in; when both are set, the key is used.

The first search after install says once which way it ran: free with no account, on your account (a key or a sign-in), or paid by a wallet. Hermes's
config keeps `web.grounder.first_use_shown` so it is not said again.

### With a search key

Make a key on your account's [Search keys](https://grounder.net/account/keys) page (a search-only key is enough), then:

```sh
hermes config set GROUNDER_API_KEY <your-key>
```

Hermes keeps it in your profile's `.env`, and the plugin sends it with each search. Remove it to use the sign-in instead.

### By signing in

```sh
hermes auth add grounder:grounder
```

This opens Grounder's sign-in page. Sign in or create an account, then allow Hermes Agent to search for it. There is nothing to
set first: the plugin writes Grounder's public sign-in client (the same for everyone, not a secret) to your profile's `.env` itself
when it first loads. There is no key to copy. Hermes keeps the grant in your profile,
refreshes it and attaches it to each search; the plugin never sees a token. The grant can search and save the search choice. Account administration needs a separate sign-in.

| Command | What it does |
|---|---|
| `hermes auth add grounder:grounder` | Signs in, or signs in again after `logout` or an expired grant |
| `hermes auth status grounder:grounder` | Shows whether Hermes holds a usable grant |
| `hermes auth logout grounder:grounder` | Removes the grant |

On a server without a browser, `hermes auth add` prints the sign-in address, the local callback address it is waiting on and
a hint for an SSH tunnel. Open the sign-in address on the machine with the browser. Without a tunnel, sign-in ends on a page
that cannot connect to `127.0.0.1`: copy that page's full address from the browser and, on the host, run
`curl '<the address>'` within three minutes of starting `hermes auth add`.

### From version 0.1.0

Version 0.1.0 signed in with `hermes grounder login` and kept its own tokens in `$HERMES_HOME/grounder`. Hermes holds the
sign-in now, so that command is gone: connect again as above and delete the old folder.

## Pay per search with a wallet

When the free searches are used up, Grounder can take payment for each search instead of an account, with [x402](https://www.x402.org): a payment in USDC on Base, signed by a wallet whose key you hold.
It is off until you set a wallet, and then it pays only what you allow.

1. Make a wallet that holds only what you are willing to spend, with USDC on Base in it. It needs nothing else: the payment is settled by Grounder's facilitator, which pays the gas.
2. Install the `eth-account` package, 0.13 or later, in the Python that runs Hermes. The plugin does not install it for you, and imports it only when it signs a payment, so everyone else needs nothing more than before.
3. Set the key yourself, never in a chat:

   ```sh
   hermes config set GROUNDER_X402_PRIVATE_KEY <the-wallets-private-key>
   ```

   Hermes keeps it in your profile's `.env`, in plain text, as it keeps a search key: anyone who can read that file can spend the wallet, so keep the balance small.
4. Set the most one search may cost, in US dollars, if you want a limit other than the default of 0.05:

   ```sh
   hermes config set GROUNDER_X402_MAX_PER_SEARCH 0.02
   ```

A free search that Grounder answers with a request for payment (HTTP 402, with its offer in a `PAYMENT-REQUIRED` header) is then paid and sent again, once, with the signed payment in a `PAYMENT-SIGNATURE` header. Grounder runs the search, settles the
payment and answers with a `PAYMENT-RESPONSE` header, which holds the transaction hash. What the plugin pays, and nothing else:

- an `exact` offer, in USDC on Base (the network and the token's contract are fixed in the plugin, not read from the offer), moved as an EIP-3009 `transferWithAuthorization`, to the address the offer names;
- for no more than `GROUNDER_X402_MAX_PER_SEARCH`. A limit that is not an amount of dollars pays nothing, rather than becoming a bigger one.

Any other offer is not paid, and the search answers with a message that says why, and where to sign up instead. One search is paid once: a second 402 after the payment is reported, never paid, and a signed payment that got
no answer may have been taken, so the message says to check the wallet and the plugin pays nothing more for that search. The limit is for one search; there is no total, so the wallet's balance is the only other bound.
A search with a search key or a sign-in is never paid from the wallet: it uses your account's credit.

A settled search carries `grounder_payment` with the amount in USDC on Base and the transaction hash when supplied. Grounder releases the payment when no topic fits, and the plugin reports no payment. Pages named by address are read free.

## It selects itself, where you have chosen nothing

Hermes runs no plugin code while installing, so the first time this plugin loads in a profile, which is the next time
Hermes starts, it sets a backend to Grounder, whose id is `grounder`, in that profile's `config.yaml`, for each of the two that has no
backend yet:

```yaml
web:
  search_backend: grounder
  extract_backend: grounder
```

A backend you already chose for `web_search` or `web_extract` is left alone, each on its own: install this just for `grounder_search` and
your extract traffic stays where it was, and with only a search backend chosen, Grounder reads pages. An older shared `web.backend` counts as a choice for both.
An existing install does the same once, the first time it loads this version. It happens once per profile: to use Grounder where you chose another backend, pick it in `hermes tools` or run
`hermes config set web.search_backend grounder`, and that choice stands. Each setting names its own backend, so
`extract_backend` can stay on another backend while Grounder searches.

With no search key, the plugin also writes `GROUNDER_OAUTH_CLIENT_ID` to your profile's `.env`. It is Grounder's public
sign-in client, the same for everyone and not a secret, which Hermes needs before `hermes auth add` can sign in.
Until you connect your account, your searches are Grounder's free searches.

## What Hermes gets

- `web_search` searches with your saved choice. A page named by its address is read with Grounder direct.
- `web_extract` reads the addresses you give it. With Grounder direct selected, your Mac reads them with Safari. Otherwise the hosted reader needs your Safari user agent and reads logged out.
- `grounder_search` can specify a mode for one search, a date window, named pages or a site. To find a named page on a site, send the host in `site` and the question in `query`.

A question that fits no topic gives a plain message with no results and no charge. Search results identify their topic and source. Grounder adds each cited source's title and link and a line identifying the search mode and source under the answer.

## Choose how Grounder searches

`/search` opens the search choice, page format and resolver menu. `/search provider` selects Grounder provider. `/search direct` selects Grounder direct, and `/search direct off` switches back. The search choice and format are saved to the Grounder account when connected, otherwise on this machine. Use `/hermes search` on Slack.

### Grounder direct on a Mac

Install the reader on macOS 13 or later with Safari, using the command on the [Grounder direct docs page](https://grounder.net/docs/direct).

- `/search-direct-format` sets summary, full-page or highlights. Summary is the default. On the Mac, Hermes can ask your active model to write it from the page's first 20,000 characters.
- `/search-login <site>` opens the site to save a sign-in in your Mac's login keychain. A connected account lists the site, Mac and save time. `/search-logout <site>` forgets it. You can also ask to read a page logged out. The reader and the sign-in window run with only `HOME` and `PATH` (and the resolver you chose), never Hermes's environment or your provider keys; the saved sites are listed from Grounder's own sign-ins folder, not the keychain.
- `/search resolver system` uses the Mac's resolver. `/search resolver grounder` uses Grounder's resolver. The choice stays on the Mac.

A page that could not be read comes back with the reason. Grounder asks before another search. [Grounder direct documentation.](https://grounder.net/docs/direct)

## What is sent

A search sends the query, relevant search options and the search credential when connected to `api.grounder.net`. Hosted page reads also send the page address. The Mac sends a locally read page's address and any saved sign-in cookies directly to its site. Grounder receives the host name for a DNS lookup only when you choose Grounder's resolver. The summary format can send the opening of the page and your question to your active model in Hermes.

Usage context is off by default. For usage reporting, the optional `post_api_request` hook shares the model name and random session and prompt tokens. To enable it, set `hermes config set web.grounder.share_usage_context true` and restart Hermes. [Privacy policy.](https://grounder.net/privacy)

## Development

Link a checkout into your Hermes home to try a change:

```sh
ln -snf "$PWD" "${HERMES_HOME:-$HOME/.hermes}/plugins/grounder"
hermes plugins enable grounder
```

The tests run against a local stub of the Grounder API and never call the live one; a stand-in plays Hermes's part in
signed-in requests, attaching a token as Hermes does. They run in a Hermes Agent development environment, or in any Python
with pytest, PyYAML and httpx, where stand-ins for Hermes's interfaces take its place:

```sh
python -m pytest tests
```

The tests of the wallet's signature need `eth-account`, and skip without it; one more compares the signature with the one the official x402 Python SDK makes, and skips unless `x402[evm]` is installed too.
Every key in them is made by the test, and nothing is funded or spent.

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 Grounder Labs, Inc. The license grants
no rights to the Grounder name or marks.
