# Grounder for Hermes Agent

More of the internet for your agent: licensed, live sources for Hermes Agent's `web_search` and `web_extract`. With Grounder as the web search backend, each search makes one
call to one search provider, such as Exa, Brave, Tavily or Perplexity, and asks the live-data APIs (scores, prices, odds, flights and more) beside it
as you set each one: every call, never, or when Grounder decides the question needs it. It returns each source's own results.

Grounder direct is a fourth mode, on a Mac: by itself it searches nothing and reads only the pages you name, and as an add-on it also reads the page that best answers a search that names none, straight from their own sites, the way your Safari asks for them, and it uses no search provider credits and no Grounder credits. It stands alone or goes together with the other three. See [Grounder direct](#grounder-direct-read-the-page-from-its-own-site-as-you).

Every result is licensed: no site's robots.txt, license or AI opt-out refuses the provider that found it. A search can
return fewer results than asked for, and is never filled with unlicensed ones.

A search needs no account: with none, it is one of Grounder's free searches. An account gets free searches too, with no card
needed, and more past them ([pricing](https://grounder.net/pricing)); or, with no account, a wallet can pay for each search past the free ones
([Pay per search with a wallet](#pay-per-search-with-a-wallet)). A `web_extract` call counts as one search, however many
pages it reads.

## Install

Needs Hermes Agent 0.22.3 or later.

```sh
hermes plugins install grounder-labs/grounder-hermes-plugin --enable
```

Grounder is also in the Hermes plugin catalog: `hermes plugins install grounder --enable` installs the version the catalog pins, which can be older than this repository's.

The text Hermes shows after an install starts with your account: free searches with no account, unless this profile already has a search key or a sign-in, and the command that signs in. After an install or an update, a chat that is already open has the new `/search` at once; for the new tools and command list, run `/reload-mcp`, then `/reload-skills`, in that chat, or start a new one with `/new`.

## Free searches

With no search key and no sign-in, a search is one of Grounder's free searches. It carries no credential and runs within the free
limits Grounder sets; this plugin repeats none of them, so a limit Grounder changes needs no change here. A free search has
no account to save a choice on (how Grounder searches, below), so Grounder runs it as Grounder optimized, and a `grounder_search` that asks for a profile or routing settings
runs without them and says so. When the free searches are used up, Grounder asks for payment (HTTP 402) and the search answers with a message that says where to sign up and
where to add credit, with the links Grounder returned, and that a wallet can pay for each search instead, as its result: Hermes would serve a failed search from its keyless free tier and
the message would never reach you. Sign up, then connect your account as below, or [set a wallet](#pay-per-search-with-a-wallet).

## Connect your account

Connect your account to search past the free limits and to choose how Grounder searches
([`/search`](#choose-how-grounder-searches-search)). Connect it with a search key or by
signing in; when both are set, the key is used.

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
refreshes it and attaches it to each search; the plugin never sees a token. The grant searches and nothing else: it cannot
reach your account's settings, keys or billing.

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

A paid search keeps what a free one leaves out: a `profile` or `routing_settings` that `grounder_search` was given go with it. Its result carries `grounder_payment` in its data: what was paid, in USDC on Base, and the transaction hash
from `PAYMENT-RESPONSE`. A `web_extract` has no place to show it, so its payment is only in Hermes's log, which holds the amount and the hash and never the key. The plugin refuses an address, in a query or in `urls`, that carries the wallet's key.

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

Each search returns the rows Hermes asked for, up to 100 (Hermes asks for 5 unless told otherwise), in Grounder's order. This plugin cuts, reranks and reorders nothing
of what Grounder returns, in any of its three search modes (a Grounder direct read is not one of them: [Grounder direct](#grounder-direct-read-the-page-from-its-own-site-as-you) says what it returns):

| Field | From Grounder |
|---|---|
| `title` | The result's title |
| `url` | The result's address |
| `description` | Its summary, or its text, as Grounder returned it, with its white space made one line: no word is cut |
| `position` | The number the answer cites it by. The results of one answer are numbered on from 1 across its searches and reads, and a page found twice keeps its first number |
| `date` | When it was published, when Grounder knows |
| `fetched` | When its text was fetched, apart from when it was published: `during this search`, the day the provider's stored copy was fetched, or `not given` when the provider does not say, so a stored copy is never read as current |

Where each result came from (whose it is, the feature and provider that found it, why it may be served) stays in Grounder's
records and in its API response; the model does not read it row by row, and the line under the answer names the providers.

Every search and read Grounder served says so once, in `served_by`: the mode it ran in, the provider and any live-data API that
answered, and how many of Grounder's results the model is shown when Hermes cut them to the model's `limit`, such as
`Grounder preset (Live) via You.com, ESPN; 5 of its 10 results shown`. Hermes writes the same field on a search another backend
answered after Grounder failed, so a model asked whether Grounder was used can tell from the result itself.

The first result Grounder serves in a turn, a search or a read, ends with a note the model then has for its answer, `cite_sources`, which
asks the model to cite each result it uses by its number in square brackets, like `[3]`, and to write no Sources list, since
the plugin adds one. A result's number belongs to the answer that searched: the numbers run on across that turn's searches and
page reads, a page keeps its number within the turn, and the next answer that searches starts again at `[1]`. An answer cites only
its own turn's results; one that made no search cites nothing by number, and the model is told to search again when it needs a
source. The turn's later results carry no note, which saves about 680 characters on each.

`web_extract` reads up to 5 pages in a call and returns one entry for each, in the order asked:

| Field | From Grounder |
|---|---|
| `url` | The address you asked for |
| `title` | The page's title, or its address when it has none |
| `content` | The page's text as Grounder returned it, whole: the plugin sends no `text_max_chars` and cuts nothing. Hermes itself applies a model's `char_limit` to the whole page |
| `error` | Only on a page that could not be read and that no search returned. It says why when Grounder knows: the address does not exist, the site refused the read, or the read ran out of time. The other pages still return. A page a search returned that cannot be read comes back without an error, as the text that search showed of it, which says so |

A `web_extract` returns whole pages, so a long page is long: Hermes applies the `char_limit` the model asks for. A read Grounder served carries `served_by`
beside its pages, and one that is the turn's first Grounder result carries the citation note too.

A `web_search` whose query holds a page's address reads that page for the query's other words, for example
`https://en.wikipedia.org/wiki/Jamie_Oliver books`. It returns the page whole, as Grounder returned it, as the result's `description`, and counts as one search.
Hermes's `web_extract` takes only addresses and answers a repeated read from its own cache, so this reads a page again for a new question. The plugin does not
tell the model about it.
The pages you ask for always lead what Grounder returns, ahead of anything its search found beside them.

An answer that cites a Grounder result ends with a Sources list the plugin writes: one plain bullet for each result the answer cites
by number, with its title and link, from that answer's own searches and page reads only. A follow-up that searched nothing has no list.
The model writes only the numbers, which spares it the
slowest tokens it makes; an answer that wrote its own list, under a Sources or References heading or label, gets none added, and
neither does a bracketed number inside code, like `items[2]`, make one. Only `http` and `https` addresses are listed, and a title
is shown as plain words: a page's own title cannot add a link or a heading to your answer. Then an answer that searched ends with
one line saying which Grounder mode it searched in, who answered, how many sources the model was shown and how long you waited, for example
`Searched with Grounder optimized · via Exa, Brave · 8 sources · 1.4 s`, `Searched with Grounder preset (Fast) · via Exa · 3 sources · 600 ms` or
`Searched with Grounder pinned · via Exa · 5 sources · 1.1 s`. A live-data API that answered beside the provider is named with it, as in
`via You.com, ESPN`. Searches of one answer that ran in different modes name each once, and where no mode is known
(a free search, whose response names none) the line says only `Searched with Grounder`. Both are added by the plugin, and the citation note tells
the model the line is Grounder's record of the answer's searches. Hermes keeps them with the answer,
so they are also in the history later turns send. A host that does not say which answer a search serves gets no list: there
the model is asked to write its own, as in any client.

Two kinds of failure reach the model differently. One you must act on (the free searches or your account's limit reached, a request for payment,
a search key or sign-in Grounder no longer accepts) comes back as the search's result, with Grounder's message and what to do
about it, so Hermes does not replace it with another vendor's results and the message reaches you. A transient one (the network, a
timeout, a rate limit, a 5xx) comes back as `success: false` and one of these errors; Hermes then retries the search on its own
keyless free tier and, if that answers, returns those results with `rescued_from: grounder` and the error in `backend_error`. If it
does not, Hermes passes the error to the model. `web.keyless_fallback: false` in `config.yaml` turns off Hermes's keyless
tier, and a Hermes that honours a provider's `keyless_rescue` setting never rescues this plugin's failures.

| Error | What to do |
|---|---|
| Grounder rejected the search key in `GROUNDER_API_KEY` | Check the key on your Search keys page, or remove it to use the sign-in |
| `GROUNDER_API_KEY` holds a character no search key has: nothing was sent | The key was pasted with a space, a line break or a curly quote in it. Set it again with `hermes config set GROUNDER_API_KEY <key>`, or remove it to use the sign-in |
| You've used Grounder's free searches, or Grounder's free searches have reached their limit for now | Sign up at the link in the message and, to search past the free ones, add credit at the second link; then set a search key or run `hermes auth add grounder:grounder` |
| Grounder needs payment for this search | The free searches are used up. Sign up at the link in the message, or [set a wallet](#pay-per-search-with-a-wallet) to pay for each search |
| This search costs more than the limit, none of Grounder's payment options is USDC on Base, or the offer could not be read: nothing was paid | The wallet is set and this offer is not one it pays; raise `GROUNDER_X402_MAX_PER_SEARCH`, or sign up as the message says |
| `GROUNDER_X402_PRIVATE_KEY` is not a private key, or `eth-account` is not installed: nothing was paid | Set the 64-hexadecimal-character key, or install `eth-account` in the Python that runs Hermes |
| Grounder did not accept the wallet's payment | Check that the wallet holds enough USDC on Base; the payment was not sent again |
| A payment was signed and sent, and Grounder did not answer | It may have been taken: check the wallet before searching again |
| Grounder rejected the sign-in | Run `hermes auth add grounder:grounder` |
| Grounder refused a search with no account | Sign in with `hermes auth add grounder:grounder`, or set a search key |
| Grounder sign-in failed | Follow Hermes's reason in the message |
| Blocked: an address carries a key or token, or is not public | Leave that address out: nothing was sent to Grounder |
| Grounder rate limit reached | Wait; the message says which limit |
| Grounder's search failed: a provider and why, and no other provider was tried | `/search` is set to one provider (Grounder pinned) and that provider is failing. Pick another with `/search`, or run `/search optimized` so Grounder picks one that is up |
| Grounder's search failed: several providers and why | Every provider Grounder called failed. Search again in a moment |
| Could not reach Grounder | Check the network, then search again |

## More control: `grounder_search`

Hermes's `web_search` takes a query and a limit, so the plugin adds two tools, on by default and listed under `grounder` in
`hermes tools`. They add to what each model request carries, so turn the `grounder` toolset off if you do not want them.

`grounder_search` takes every option of Grounder's MCP search: `top_k`, `days`, `domains`, `features`, `urls`,
`include_text`, `requested_provider`, `providers`, `routing_settings`, `profile`, `user_location` and
`user_time_zone`. It answers in `web_search`'s shape and counts as one search. `grounder_list_providers` lists the features a
search can use, the providers behind each and the presets `profile` takes, and is not a search.

Signing in grants searching only, so `user_info` of Grounder's MCP server is not offered here. Manage your account at
[grounder.net/account](https://grounder.net/account).

## Choose how Grounder searches: `/search`

Grounder has four modes. The first three search, and each makes one search-provider call; they return the provider's own results, each uncut and not reranked, by Grounder or by this plugin. A result whose site's rules refuse that provider is left out, and nothing replaces it. The fourth, Grounder direct, makes no search-provider call at all and has [its own section](#grounder-direct-read-the-page-from-its-own-site-as-you).

| Mode | What it is | How to choose it |
|---|---|---|
| **Grounder optimized** | Grounder picks the provider for your question. The default | `/search optimized` (or `/search auto`), or pick it in `/search` |
| **Grounder preset** | Fast, Cheap, Live, Deep, Code or Balanced: Grounder picks the best provider for that preset | `/search <preset>`, for example `/search fast`; or pick one in `/search` |
| **Grounder pinned** | You pick the provider, and optionally one of its modes: the only search provider called, and what it finds is what you get, even when that is nothing | `/search <provider>` or `/search <provider>:<mode>`, for example `/search exa:instant`; or pick one in `/search` |
| **Grounder direct** | You name the page; Grounder reads it from its own site, on your Mac, with your Safari's user agent: no search provider, no fallback, no credits. By itself, or as an add-on to any of the three above | `/search direct` (by itself), `/search direct with-search` (add-on), `/search direct off`; or pick it in `/search` |

`/search` chooses between them. The plugin has three more commands, `/search-direct-format`, `/search-login` and `/search-logout` (`/search_login` and `/search_logout` on Telegram), which belong to Grounder direct and are described below. Your choice is saved on your Grounder account, in the same setting as the Routing page on grounder.net: a change made in `/search` shows on that page, and a change made on that page shows in `/search` the next time it opens. It offers Grounder's own lists, read when you run it, so a preset, provider, mode or live-data
API Grounder adds is on offer at once. The commands are registered only while Grounder is your web search backend (`web.search_backend: grounder`, or the older `web.backend`): the plugin
makes Grounder the backend the first time it loads where you chose none, and a switch you make in `hermes tools` takes effect at the next start. With another backend chosen the plugin registers no command, and
its search tools and its web provider work as they do otherwise.

`/search` opens one menu, "How should Grounder search?". Every list is one view and every row is one line:

| Row | What it opens |
|---|---|
| **Let Grounder pick** | Grounder optimized: Grounder picks a provider for each question. Picking it sets it |
| **A preset** | The six presets, each with its purpose and, where the line has room, how many providers it uses (for example "Fast: the quickest answer · 16 providers"), then **← Back**. Picking one sets it |
| **A provider** | Every provider in one list, each with what its standard search returns and how many modes it has (for example "Exa: web search, page text · 16 modes"), then **← Back**. A provider with modes opens them: **Default** (its standard search), then each mode with the line Grounder gives for what it is, cut to fit the row, then **← Back**. Picking a mode, or a provider that has none, sets it |
| **Grounder direct** | Grounder direct by itself: only the pages you name are read, from their own sites, on this Mac. Picking it sets it, and answers with what it does. It is saved on this Mac, not on your account |
| **Live-data APIs** | **Set all to Grounder decides**, then every live-data API with its setting, then **← Back**. An API opens **Every call**, **Grounder decides** and **Never**, then **← Back**, and the list comes back so you can set another. Once a setting has changed, the list's last row is **Done**. The menu's row shows a count, for example "18 on Grounder decides, 1 on Every call, 1 on Never" |
| **Settings** | Grounder direct's own settings, saved on this Mac. **Resolver** opens **Your own resolver** (the default) and **Grounder's resolver**, then **← Back**. The row shows the one set now |
| **Done** | Ends it, and says nothing changed |

After a pick of Grounder optimized, a preset or a provider, the menu asks one more question, "Also read sites you name directly, as you?", with **Yes** and **No** (the one now set marked "(current)"). Yes sets Grounder direct as an add-on to that mode and the reply's first line reads "Grounder direct + Grounder pinned" (or the mode you picked); No sets the mode alone.

A pick closes the menu and answers with what is set now, in the shape of Hermes's own reply to a model switch:

```text
✓ Search switched: Grounder pinned
  Provider: Wikipedia, the only search provider called
  Capabilities: encyclopedia, page text
  Filters: language
  Results: only what it finds, even when that is nothing
  Live-data APIs: 20 on Grounder decides (`/search apis`)
  (saved to your Grounder account; applies wherever it searches)
```

A provider with modes adds a **Mode** line: the mode, or Default, and the line Grounder gives it. A preset's reply says what the preset is for and how many providers it draws from. Grounder optimized's says Grounder picks the provider. Leaving the live-data APIs by **Done** lists the ones set to Every call or Never. A typed argument is answered the same way, and when the pick is what your account already had, the first line says "Search unchanged". No line of a reply is longer than 64 characters: a longer one is wrapped at a word, so a terminal does not break it mid-word.

The row for what is set now says "(current)". Each choice is saved to your account when you make it. A row takes at most 56 characters with its number, so it stays one line in Hermes's panel in a terminal 66 columns wide or more: a note that does not fit gives way to a shorter one, or is cut at a word with "…". A list is one card, except where a messaging app limits a card and the list does not fit it (Discord 24 choices; WhatsApp's list takes 10 rows and Hermes adds its own "Other" row, so 9 here). There it is a page at a time, with **More: 8–14 of 22** and, on a later page, a Back row that goes to the page before. Hermes numbers the first ten rows of a card (1 to 9, then 0); the rows after them have no number, and the arrow keys reach them. In a terminal with fewer rows than a list, Hermes's panel shows the part of the list around the cursor and its question line scrolls out of view; the arrow keys still reach every row. A name typed in Hermes's "Other (type your answer)" row picks an item on the card shown. Typed arguments skip the menu: `/search exa`, `/search exa:instant` (a provider's mode), `/search fast` (a preset), `/search optimized` (or `/search auto`), `/search apis`
(the live-data APIs), and `/search apis espn=every mlb=never polymarket=auto` to set some without the page (`every`, `never` or `auto`). `/search api:<id>` sets one live-data API to Every call. For Grounder direct: `/search direct` (by itself), `/search direct with-search` (as an add-on to the mode you have), `/search direct off`, and the shorter `/search addon on|off`. `/search settings` opens Settings, and `/search resolver system|grounder` sets the resolver without the menu.

### Live-data APIs: Every call, Never or Grounder decides

Each live-data API (scores, prices, odds, flights, weather and more: Grounder lists them) has its own setting, and it applies in every mode, Grounder optimized, a Grounder preset and Grounder pinned,
in any combination. **Every call**: Grounder asks it on every search. **Never**: Grounder never asks it. **Grounder decides**, the default: Grounder asks it only when your question's features
call for it. The live-data APIs are asked beside the mode's one search-provider call, never in place of it, and their results come back beside the provider's, each labeled with its source.
Grounder pinned changes only the search provider: it is the one you picked, and the live-data APIs follow their settings.

A preset and a provider you pick clear each other. A provider you pick is the only search provider a search calls, saved with its fallback off: if it
finds nothing, the search returns nothing and Grounder picks no other provider. (A provider already saved with its fallback on, on the Routing page, keeps it when you pick it again here, and Grounder
then picks one other when it finds nothing; the menu says so.) Every Hermes released today asks the menu with `ctx.ask_choice`. **On Slack, `/search` is Slack's own search**, so type `/hermes search`, which opens the same menu. (A Hermes that offers `ctx.ask_form` would show the same choices as one form with the presets side by side and
a drop-down of each provider's modes, and each live-data API as a header over its three settings side by side; no released Hermes offers it yet.)

**Where your choices live.** On your Grounder account, never in Hermes's `config.yaml`: the mode, the preset or provider and mode you picked, and each live-data API's setting (only those you set to Every call or
Never differ from the default, so an API Grounder adds later is Grounder decides). `/search` reads the setting when it opens, writes each choice as you make it and shows what Grounder answered, with your search key or
your sign-in. If Grounder cannot be reached, or refuses a choice (a provider that is off on the Routing page, for example), `/search` says why and changes nothing. A search made with your search key or sign-in sends none
of these choices: Grounder applies your saved setting to a search that picks no mode, preset or provider of its own, and bills it as that mode. `grounder_search` can pass its own `profile`, `requested_provider` or
`routing_settings`: they apply to that search alone and your saved setting is not changed, but your choice wins. While you have chosen a preset or a provider, the model's own `profile`, `requested_provider`,
`providers`, `exclusive` and `always` are not sent (the plugin reads your setting first), so the search runs as your choice and is billed as it, and a live-data API you set to Every call or Never keeps that
setting; what only narrows a search (caps, a provider or mode set to Off, `fallback`) is sent. If you chose Let Grounder pick, the model's own choice runs that one search. A preset or provider Grounder no longer lists reads as Let Grounder pick. A
page read (`web_extract`, or a `web_search` of an address) sends none of this either.

With no search key and no sign-in, a command answers with how to sign in instead of a list, because a free
search has no account to save a choice on. A Hermes that bundles Grounder's provider has the command already, and this plugin adds none there. A Hermes that
cannot register a command or ask a choice gets none, and search works the same.

## Grounder direct: read the page from its own site, as you

Grounder direct is the fourth mode. You name the page, or the site, and Grounder reads it straight from that site with your own user agent: no search provider is asked, no search runs, and nothing else is tried if the page cannot be read. It is a plugin feature on your Mac, and it works through Safari's engine.

What a direct read uses and sends:

- No search provider credits and no Grounder credits. A direct read is not a search, so it is not counted against the free searches and is not charged to your account or your wallet.
- Nothing a read returns goes to Grounder with it. The page comes from the site to your Mac, and what it returns is not kept or sent anywhere. The exceptions are a lookup and the sign-in list (see "Sign-ins", below), neither of which carries a page or a cookie. The lookup: when you choose Grounder's resolver (below), the host names the reader looks up go to Grounder as DNS queries, and nothing else on a read does.

### What a Mac needs

- macOS, because the reader is a small Swift program that opens each page in a hidden WebKit view, the engine Safari is built on.
- Safari, installed at `/Applications/Safari.app`. The reader asks for the page with the user agent your installed Safari's version makes (Safari freezes the platform and WebKit parts of it, so only the `Version/` part follows your Safari), and a fresh, empty session.
- The reader, which is a signed, notarized program, `grounder-direct`, installed by `curl -fsSL https://grounder.net/install | sh` into `~/Library/Application Support/Grounder Direct/bin`. The plugin runs it for every read (as `grounder-direct reader`), so no Xcode and no command line tools are needed. Without it, a Mac that has Xcode's command line tools still works: the plugin compiles the same reader with `xcrun swiftc` the first time it reads a page, and again when the reader's source changes, into `$HERMES_HOME/cache/grounder-direct`.

Elsewhere, Grounder direct does not read: on a machine that is not a Mac, or a Mac without Safari, the plugin does not fall back to Grounder's page reader or to any other host. A read returns a result that says so ("this machine is not a Mac", or "Safari is not installed here"), and the model tells you. Use one of the three search modes there.

### Choosing it

Grounder direct is chosen by itself, or as an add-on to any of the other three modes. With a connected account it follows the Grounder direct setting saved on your account (its use and format); without one it is the setting on this Mac, `web.grounder.direct` in your Hermes `config.yaml`.

- **By itself.** Only the pages you name are read, and no search runs. `web_search`, `web_extract` and `grounder_search` all read only the addresses they are given (or the addresses in your latest message). A request that names no address and no site reads nothing: the model asks which site to go to. Choose it with `/search direct`, or with the **Grounder direct** row of the `/search` menu.
- **As an add-on.** Searches run as the mode you chose says (Grounder optimized, a Grounder preset or Grounder pinned), and every page you name, or a search asks to read, is read with Grounder direct instead of Grounder's page reader. A question that names a page is read from that page; a page or article named without its link is found with a search on its site, then read; one that names none is searched, and the result page that best answers it (the three best sites' pages in the highlights format) is read directly in the format you saved, ahead of the search's own results. The search is sent and billed as its mode says; the reads add nothing. After you pick Grounder optimized, a preset or a provider in the `/search` menu, it asks "Also read sites you name directly, as you?": Yes turns the add-on on, No turns it off. The reply then says "Grounder direct + Grounder optimized" (or the mode you picked). Typed: `/search direct with-search` turns the add-on on for the mode you have, and `/search direct off` turns Grounder direct off, so a page is read by Grounder's reader again. Picking a search mode, in the menu or typed (`/search fast`), ends Grounder direct by itself and searches again as that mode says; an add-on that was on stays as it was.

`/search` needs a connected account, like the rest of its menu, and so do `/search settings` and `/search resolver`. `/search-login`, `/search-logout` and `/search-direct-format` do not, though without one the list of saved sign-ins stays on this Mac only (see "Sign-ins", below). `hermes config set web.grounder.direct only` (by itself), `with-search` (the add-on) or `off` sets the same thing without one.

You never need to give a site's exact address. Name the site (a forum, a wiki, a publication) and the model works out the most specific page that answers you, written the way the site serves it, then reads it. The plugin's instructions to the model say the same of every web lookup: use Grounder's tools for them, including reading a page you link or a site you name, and not a browser or a terminal command such as `curl`.

### Formats: how a page comes back

A page comes back in one of three formats. Name one in your message ("give me the highlights", "summarize it", "the full page"), or set a default with `/search-direct-format`. A format you name in chat, or that a call sets, wins for that turn, and the saved default is `full-page` until you set one.

| Format | What you get |
|---|---|
| `full-page` | The page's text as it was read, in full and in its order, without summarizing. Only menus, buttons and other navigation lines are left out. At most 60,000 characters of a page's text are returned |
| `highlights` | Exact quotes from the page, word for word, as a bulleted list in page order. The plugin picks the passages that share the most words with your question (at most 10, each cut at 400 characters), lists each once in page order, and drops advertisements and the unlabelled copies of their text, so the model adds no words of its own to them |
| `summary` | An AI summary written by your own active model through Hermes (`ctx.llm`): one sentence on what the page is, then three to six bullet points, most important first, with names and numbers kept. When your request names a topic beyond reading the page, the summary puts what the page says about it first, and ends with a line saying so if the page says nothing about it. The page's first 20,000 characters go to that model (500 tokens at most for the answer), under the heading "AI summary". If this Hermes gives plugins no model, or the model call fails or returns nothing, the page's text comes back with the ask to the answering model to write the summary itself, and says why. The plugin calls no model of its own |

`/search-direct-format` opens a choice of the three. `/search-direct-format highlights` sets one at once, and the saved default is `web.grounder.direct_format`, on this Mac only. A messaging chat that cannot show a choice answers with the current format and how to set one.

### Sign-ins: read a site signed in as you

A direct read starts with a fresh, logged-out session. For a site that needs you signed in, save a sign-in once:

`/search-login reddit.com` (`/search_login` on Telegram, which allows no hyphen in a command name). Give a site, such as `reddit.com`, or any address on it: `https://old.reddit.com/login` is cleaned to the site, `reddit.com`, and the sign-in then covers every subdomain and page of it. A window opens on your Mac at the site's own sign-in page. Sign in there, passing the site's own checks yourself, then click Save sign-in (Command-S). From then on Grounder direct reads that site signed in as you, with the same Safari user agent. `/search-login` with no site lists the sign-ins you have saved.

Signing in is optional. A saved sign-in that cannot be used (it expired, or its keychain key is gone) never stops a read: the page is read logged out instead, and the page's first line says so. To read a site logged out on purpose while a sign-in is saved for it, say so in your message ("not logged in", "without my sign-in"); the model then passes `signed_in: false` to `grounder_search`.

How a sign-in is kept: the site's cookies are sealed in a file only your user can read (`~/Library/Application Support/Grounder Direct/sign-ins`), under a key that only your login keychain holds (service `grounder-direct`). No cookie passes through Hermes, the model, a log or a command line, and none is sent to any site but the one it was saved for. Cookies the site refreshes during a read are saved back. The cookies are never sent to Grounder. When this profile has a connected account (a search key or a Hermes sign-in), the plugin also tells Grounder the name of each site you saved a sign-in for, your Mac's name and a random id for it, and when each was saved, and the Saved sign-ins block of your Routing page on grounder.net (`/account/routing#route-signins`) lists them. Remove one there and the plugin forgets it the next time it syncs: on first use after Hermes starts, after `/search-login` and `/search-logout`, and before a direct read when the last sync is over ten minutes old. With no connected account nothing is sent and the list is on this Mac only.

To forget one: `/search-logout reddit.com` (`/search_logout` on Telegram) removes its keychain key and its sealed file, and the site is read logged out again; like `/search-login`, it cleans what you type to the site. `/search-logout` with no site, like `/search-login` with none, lists the saved sign-ins, by site and when each was saved, never their values. Neither command needs a connected account; with one, the list is also on your account at grounder.net, and `/search-logout` drops the site from it at the next sync. The older `/search direct signin`, `signout` and `sites` still work as aliases. From a terminal, `python3 direct_dev.py forget <site>` and `python3 direct_dev.py sites` in the plugin's folder do the same, and `python3 direct_dev.py paste <site>` saves a Cookie header at a hidden prompt, for a site that refuses to sign in inside the window.

### Which resolver it looks sites up with

Some sites blank their own page when your DNS filter blocks one of their scripts (a site whose ad-block check cannot load its helper script, for example). The Settings section of `/search` (`/search settings`, then Resolver) chooses the resolver a direct read looks sites up through, saved on this Mac as `web.grounder.direct_resolver`. `/search resolver system` and `/search resolver grounder` set it without the menu. When it is Grounder's, a page's header says its name was looked up through Grounder's resolver.

| Resolver | What it means |
|---|---|
| `system` (the default; "Your own resolver") | Your own: this Mac's, with any filter on it or on your network |
| `grounder` ("Grounder's resolver") | Grounder's, at `https://api.grounder.net/v1/dns-query` (DNS over HTTPS, no account needed). Grounder's resolver sees the host names the reader looks up, not the pages, which go from your Mac to the site, encrypted end to end. Grounder keeps no log of the names |

`hermes config set web.grounder.direct_resolver grounder` sets it without the menu.

### The line under the answer

An answer that read pages with `web_extract` or a direct read ends with one more line, beside the "Searched with ..." line. For pages Grounder direct read on your Mac: `Grounder fetched www.cnn.com (summary) · 9.0 s`: the host, the format, and how long the reads took (with the number of pages when there are several; at most three hosts are named, then `+n`). For pages Grounder's own reader extracted: `Grounder extracted example.com`.

### Limits

- Public `http` and `https` addresses only: an address that carries a key or token, or is not public, is refused, as everywhere in the plugin. Up to 10 pages in a call, though Hermes's `web_extract` asks for at most 5.
- One page at a time, with at least 2 seconds between two reads of one site. A read stops itself after 30 seconds, and the plugin gives up on it at 45.
- A site's own check, a block page or a "verify you are human" page, is reported with what it said, and never solved or skipped. The page is not retried through another route.
- A page that builds its text with scripts that have not finished when it is read can come back with no text, and says so. A page the site answers with HTTP 400 or above comes back as not read, with the status.
- No fallback: a page that cannot be read says why, and nothing else is tried, with one exception. When a page does not load at all (not when the site answers with a status, a check or a block), the plugin reads the same address once more on the site's other host name: `www.` added to a bare domain, or taken off a `www.` one. The page then says its other host was read. Nothing else is tried.

### When a read does not work

| What you see | What it means and what to do |
|---|---|
| The site showed a check instead of the page ("verify you are human", "just a moment", "access denied", "unusual traffic") | The site's own bot check answered. Nothing was done to get past it. Save a sign-in with `/search-login <site>`, where you pass the check yourself, or give another address |
| The site served an empty page that only runs a script | This is how a bot check such as Akamai's looks. A sign-in saved with `/search-login <site>`, where you pass the check yourself, may get through |
| The page asks you to log in, or comes back as a log-in page | Save a sign-in with `/search-login <site>`; the site is then read signed in as you |
| The site answered HTTP 403 or another status above 399 | The site refused the read. It is reported as it was, and not retried through another route |
| The page showed no text when it was read | It builds its text with scripts that had not finished. Try again, or read another page of the site |
| Safari is not installed here | Grounder direct needs Safari's version for the user agent it reads with. Install or restore Safari, or use a search mode |
| Could not build the reader | The program is not installed and this Mac has no Xcode command line tools to build one: run `curl -fsSL https://grounder.net/install | sh`, then read again |
| This machine is not a Mac | Grounder direct reads with Safari's engine on a Mac only. Use a search mode here |
| The page did not load, and its other host name was read | The page at the address you gave did not load at all, so the plugin read its `www.` or bare twin once. The header of the page says so |
| "Grounder direct searches nothing: it goes only to an exact address, and this call had none" | Name a site or give an address. In Grounder direct by itself, nothing is searched |
| A site blanks its page and nothing changes | Your DNS filter may be blocking one of its scripts. Choose Grounder's resolver in Settings of `/search` |

## What is sent

**A Grounder direct read sends nothing to Grounder.** (Saving a sign-in is not a read: with a connected account it lists the site's name on your account, as "Sign-ins" says, and never a cookie.) The page address goes from your Mac to the page's own site, with your Safari's user agent and, for a site you saved a sign-in for, that site's own cookies. It uses no search provider credits and no Grounder credits, and nothing it returns is kept or sent anywhere. The three things that do leave your Mac, each only to the one they are for: the site you read gets the address you asked for; with Grounder's resolver chosen, Grounder gets the host names the reader looks up, as DNS queries to `https://api.grounder.net/v1/dns-query`, and not the pages; and with the `summary` format, the page's first 20,000 characters and your question go to your own active model through Hermes, the model you already use. In the add-on, the search itself is sent and billed as its mode says, and only the page reads are Grounder direct's.

Each `web_search` query, up to its first 2,000 characters, goes to `https://api.grounder.net/v1/search` with the number
of results wanted, your search key or, when you signed in, the
access token Hermes attaches (a free search sends neither), and a user agent naming this plugin and its version. It sends none of the choices you made with `/search`: they are saved on your Grounder account, and Grounder applies them. Nothing else from Hermes goes with a search, unless you turn on usage context (below): not the conversation, the
model's other output or what other tools returned. An address in a query or in `urls` that carries what looks like a key or token
(Hermes's own check for `web_extract`, on the address and its decoded form, and your own search key), or that is not a public web
address, is never sent: the call is refused and says why. The plugin follows no redirect from Grounder, so a search key goes to
Grounder and nowhere else. When a search fails for a transient reason, Hermes may send the same query to its keyless
free tier, as "What Hermes gets" says; that is Hermes's own behaviour, not Grounder's.

**Usage context, off by default.** A search can also carry two random tokens, one for the session and one for the prompt, so one prompt's searches can be told from the next, and
the name of the model whose answer asked for it, as Hermes reports it (`context.session_ref`, `context.prompt_ref` and `context.agent.model`). They are for Grounder's usage reporting:
which models run searches, and how many searches a prompt takes. A search does not need any of it, so none is sent and the hook that learns the model (`post_api_request`) is not
registered unless you turn it on: `hermes config set web.grounder.share_usage_context true`, then restart Hermes (the hook is registered when the plugin loads). Set it back to `false`
and all three fields stop at the next search. The tokens carry no Hermes id or message, are random to everyone and last only as long as the Hermes process.

Each `web_extract` sends the addresses of its pages, the same credential and
user agent, and a question to read them for: the query of the `web_search` that found them, when one did, and otherwise your
latest message in the session, up to its first 2,000 characters. No `text_max_chars` or other option that cuts a page goes with it. The plugin keeps both in memory only, for the last 500
pages found and the last 64 sessions. With neither, the addresses are sent as the query. The question is recorded as a
search's query is. Your other messages, and the rest of the conversation, are never sent.

A `grounder_search` sends what a `web_search` sends plus the options the model chose, including a location or time zone only
when the user stated one. `grounder_list_providers` reads Grounder's public feature list and list of presets and sends no
credential. `/search` reads Grounder's public lists of providers, presets and live-data APIs, and reads and writes your saved search setting (`GET` and `PUT https://api.grounder.net/v1/search/settings`); none of it is a search. A key goes with
the setting's requests as with a search; with a sign-in, Hermes attaches its token to the lists' read, which is how a command knows you are signed in.

**With a wallet set**, a free search that Grounder answers with a request for payment is sent a second time, once, with the same words and a signed payment authorization: your wallet's address, the amount and the address it
pays, the validity window and the signature. Grounder, and the facilitator that settles the payment, see that address and the transaction, which is public on Base. Your private key is never sent.

Searches are recorded under your Grounder account, and a free search with no account as Grounder's
[privacy policy](https://grounder.net/privacy) and [data use](https://grounder.net/docs/data-use) pages describe. The plugin logs how many results a search
returned or pages an extract read, never the query or an address. To add `served_by`, `cite_sources`, the Sources list and the line under an
answer it reads its own results and the finished answer on your machine, and sends neither.

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
