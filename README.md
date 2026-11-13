# Grounder for Hermes Agent

Licensed, live sources for Hermes Agent's `web_search` and `web_extract`. With Grounder as the web search backend, each search makes one
call to one search provider, such as Exa, Brave, Tavily or Perplexity, and asks the live-data APIs (scores, prices, odds, flights and more) beside it
as you set each one: every call, never, or when Grounder decides the question needs it. It returns each source's own results.

Every result is licensed: no site's robots.txt, license or AI opt-out refuses the provider that found it. A search can
return fewer results than asked for, and is never filled with unlicensed ones.

A search needs no account: with none, it is one of Grounder's free searches. An account gets free searches too, with no card
needed, and more past them ([pricing](https://grounder.net/pricing)). A `web_extract` call counts as one search, however many
pages it reads.

## Install

Needs Hermes Agent 0.22.3 or later.

```sh
hermes plugins install grounder-labs/grounder-hermes-plugin --enable
```

Once Grounder is listed in the Hermes plugin catalog, `hermes plugins install grounder --enable` installs the same plugin.

## Free searches

With no search key and no sign-in, a search is one of Grounder's free searches. It carries no credential and runs within the free
limits Grounder sets; this plugin repeats none of them, so a limit Grounder changes needs no change here. A free search has
no account to save a choice on (how Grounder searches, below), so Grounder runs it as Grounder optimized, and a `grounder_search` that asks for a profile, routing settings or a workspace
runs without them and says so. When the free searches are used up, the search answers with a message that says where to sign up and
where to add credit, with the links Grounder returned, as its result: Hermes would serve a failed search from its keyless free tier and
the message would never reach you. Sign up, then connect your account as below.

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
of what Grounder returns, in any of its three modes:

| Field | From Grounder |
|---|---|
| `title` | The result's title |
| `url` | The result's address |
| `description` | Its summary, or its text, as Grounder returned it, with its white space made one line: no word is cut |
| `position` | The number the answer cites it by. The results of one answer are numbered on from 1 across its searches and reads, and a page found twice keeps its first number |
| `date` | When it was published, when Grounder knows |

Where each result came from (whose it is, the feature and provider that found it, why it may be served) stays in Grounder's
records and in its API response; the model does not read it row by row, and the line under the answer names the providers.

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

A `web_extract` returns whole pages, so a long page is long: Hermes applies the `char_limit` the model asks for. A read that is the turn's first Grounder result
carries the citation note too.

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
`Searched with Grounder direct · via Exa · 5 sources · 1.1 s`. Searches of one answer that ran in different modes name each once, and where no mode is known
(a free search, whose response names none) the line says only `Searched with Grounder`. Both are added by the plugin. Hermes keeps them with the answer,
so they are also in the history later turns send. A host that does not say which answer a search serves gets no list: there
the model is asked to write its own, as in any client.

Two kinds of failure reach the model differently. One you must act on (the free searches or your account's limit reached,
a search key or sign-in Grounder no longer accepts) comes back as the search's result, with Grounder's message and what to do
about it, so Hermes does not replace it with another vendor's results and the message reaches you. A transient one (the network, a
timeout, a rate limit, a 5xx) comes back as `success: false` and one of these errors; Hermes then retries the search on its own
keyless free tier and, if that answers, returns those results with `rescued_from: grounder` and the error in `backend_error`. If it
does not, Hermes passes the error to the model. `web.keyless_fallback: false` in `config.yaml` turns off Hermes's keyless
tier, and a Hermes that honours a provider's `keyless_rescue` setting never rescues this plugin's failures.

| Error | What to do |
|---|---|
| Grounder rejected the search key in `GROUNDER_API_KEY` | Check the key on your Search keys page, or remove it to use the sign-in |
| You've used Grounder's free searches, or Grounder's free searches have reached their limit for now | Sign up at the link in the message and, to search past the free ones, add credit at the second link; then set a search key or run `hermes auth add grounder:grounder` |
| Grounder rejected the sign-in | Run `hermes auth add grounder:grounder` |
| Grounder refused a search with no account | Sign in with `hermes auth add grounder:grounder`, or set a search key |
| Grounder sign-in failed | Follow Hermes's reason in the message |
| Blocked: an address carries a key or token, or is not public | Leave that address out: nothing was sent to Grounder |
| Grounder rate limit reached | Wait; the message says which limit |
| Could not reach Grounder | Check the network, then search again |

## More control: `grounder_search`

Hermes's `web_search` takes a query and a limit, so the plugin adds two tools, on by default and listed under `grounder` in
`hermes tools`. They add to what each model request carries, so turn the `grounder` toolset off if you do not want them.

`grounder_search` takes every option of Grounder's MCP search: `top_k`, `days`, `domains`, `features`, `urls`,
`include_text`, `requested_provider`, `providers`, `routing_settings`, `workspace`, `profile`, `user_location` and
`user_time_zone`. It answers in `web_search`'s shape and counts as one search. `grounder_list_providers` lists the features a
search can use, the providers behind each and the presets `profile` takes, and is not a search.

Signing in grants searching only, so `user_info` and the workspace tools of Grounder's MCP server are not offered here. Manage
workspaces on your [account](https://grounder.net/account).

## Choose how Grounder searches: `/search`

Grounder has three modes. Every one returns the provider's own results, each uncut and not reranked, by Grounder or by this plugin. A result whose site's rules refuse that provider is left out, and nothing replaces it.

| Mode | What it is | How to choose it |
|---|---|---|
| **Grounder optimized** | Grounder picks the provider for your question. The default | `/search optimized` (or `/search auto`), or pick it in `/search` |
| **Grounder preset** | Fast, Cheap, Live, Deep, Code or Balanced: Grounder picks the best provider for that preset | `/search <preset>`, for example `/search fast`; or pick one in `/search` |
| **Grounder direct** | You pick the provider, and optionally one of its modes: the only search provider called, and what it finds is what you get, even when that is nothing | `/search <provider>` or `/search <provider>:<mode>`, for example `/search exa:instant`; or pick one in `/search` |

`/search` is the plugin's only command, and it chooses between them. Your choice is saved on your Grounder account, in the same setting as the Routing page on grounder.net: a change made in `/search` shows on that page, and a change made on that page shows in `/search` the next time it opens. It offers Grounder's own lists, read when you run it, so a preset, provider, mode or live-data
API Grounder adds is on offer at once. It is registered only while Grounder is your web search backend (`web.search_backend: grounder`, or the older `web.backend`): the plugin
makes Grounder the backend the first time it loads where you chose none, and a switch you make in `hermes tools` takes effect at the next start. With another backend chosen the plugin registers no command, and
its search tools and its web provider work as they do otherwise.

`/search` opens one menu, "How should Grounder search?", and every screen returns to it:

| Row | What it opens |
|---|---|
| **Let Grounder pick** | Grounder optimized: Grounder picks the provider for each question. Picking it sets it |
| **A preset** | The six presets, each with its purpose and how many providers it uses (for example "Fast: the quickest answer · 16 providers"), then **← Back**. Picking one sets it |
| **A provider** | Each provider with how many modes it has, a page at a time, then **← Back**. A provider with modes opens them: **Default** (its standard search), then each mode with the one line Grounder gives for what it is and how it differs from the provider's other modes, a page at a time, then **← Back** |
| **Live-data APIs** | **Set all to Grounder decides**, then each live-data API with its setting, then **← Back**. An API opens **Every call**, **Grounder decides** and **Never**, each with its note, then **← Back**. The row shows a count, for example "18 on Grounder decides, 1 on Every call, 1 on Never" |
| **Done** | Ends it. If anything changed, one line says what is set now, for example "Grounder: preset Fast · Live-data APIs: Kalshi every call" (a live-data API on Grounder decides is left out). If nothing changed, it says so |

The row for what is set now says "(current)". Each choice is saved to your account when you make it, so leaving the menu any other way keeps what you chose. A long list (the providers, a provider's modes, the live-data APIs) shows 8 items a page, then **More: 9–16 of 22**, then **← Back**; on a later page Back goes to the page before. A page is 8 items or fewer where a messaging app limits a card (Discord 24 choices; WhatsApp's list takes 10 rows and Hermes adds its own "Other" row, so 9 here). A name typed in Hermes's "Other (type your answer)" row picks an item on the page shown. Typed arguments skip the menu: `/search exa`, `/search exa:instant` (a provider's mode), `/search fast` (a preset), `/search optimized` (or `/search auto`), `/search apis`
(the live-data APIs), and `/search apis espn=every mlb=never polymarket=auto` to set some without the page (`every`, `never` or `auto`). `/search api:<id>` sets one live-data API to Every call.

### Live-data APIs: Every call, Never or Grounder decides

Each live-data API (scores, prices, odds, flights, weather and more: Grounder lists them) has its own setting, and it applies in every mode, Grounder optimized, a Grounder preset and Grounder direct,
in any combination. **Every call**: Grounder asks it on every search. **Never**: Grounder never asks it. **Grounder decides**, the default: Grounder asks it only when your question's features
call for it. The live-data APIs are asked beside the mode's one search-provider call, never in place of it, and their results come back beside the provider's, each labeled with its source.
Grounder direct changes only the search provider: it is the one you picked, and the live-data APIs follow their settings.

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

## What is sent

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

Searches are recorded under your Grounder account, and a free search with no account as Grounder's
[privacy policy](https://grounder.net/privacy) and [data use](https://grounder.net/docs/data-use) pages describe. The plugin logs how many results a search
returned or pages an extract read, never the query or an address. To add `cite_sources`, the Sources list and the line under an
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

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 Grounder Labs, Inc. The license grants
no rights to the Grounder name or marks.
