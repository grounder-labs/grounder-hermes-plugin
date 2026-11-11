# Grounder for Hermes Agent

Licensed, live sources for Hermes Agent's `web_search` and `web_extract`. With Grounder as the web search backend, one search reaches the
providers the query needs, such as Exa, Brave, Tavily and Perplexity, and live data for scores, prices, odds and flights,
and comes back as one deduplicated list.

Every result is licensed: no site's robots.txt, license or AI opt-out refuses the provider that found it. A search can
return fewer results than asked for, and is never filled with unlicensed ones.

An account gets 50 free searches a day, with no card needed ([pricing](https://grounder.net/pricing)). A `web_extract` call
counts as one search, however many pages it reads.

## Install

Needs Hermes Agent 0.22.3 or later.

```sh
hermes plugins install grounder-labs/grounder-hermes-plugin --enable
```

Once Grounder is listed in the Hermes plugin catalog, `hermes plugins install grounder --enable` installs the same plugin.

## Connect your account

Searches run on your Grounder account. Connect it with a search key or by signing in; when both are set, the key is used.

### With a search key

Make a key on your account's [Search keys](https://grounder.net/account/keys) page (a search-only key is enough), then:

```sh
hermes config set GROUNDER_API_KEY <your-key>
```

Hermes keeps it in your profile's `.env`, and the plugin sends it with each search. Remove it to use the sign-in instead.

### By signing in

```sh
hermes config set GROUNDER_OAUTH_CLIENT_ID https://api.grounder.net/oauth/clients/hermes-agent
hermes auth add grounder:grounder
```

The first line names Grounder's public sign-in client for Hermes: the same for everyone and not a secret, set once because
Hermes reads a plugin's client from your profile's `.env`. The plugin writes the same line itself when it first loads, so
it is only needed if you sign in before starting Hermes. The second opens Grounder's sign-in page. Sign in or create an
account, then allow Hermes Agent to search for it. There is no key to copy. Hermes keeps the grant in your profile,
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

## It selects itself

Hermes runs no plugin code while installing, so the first time this plugin loads in a profile, which is the next time
Hermes starts, it sets both backends in that profile's `config.yaml`:

```yaml
web:
  search_backend: grounder
  extract_backend: grounder
```

This replaces any backend chosen for `web_search` or `web_extract` before. An existing install does the same once, the
first time it loads this version. It happens once per profile: to use another backend, pick it in `hermes tools` or run
`hermes config set web.search_backend <backend>`, and that choice stands. Each setting names its own backend, so
`extract_backend` can stay on another backend while Grounder searches.

With no search key, the plugin also writes `GROUNDER_OAUTH_CLIENT_ID` to your profile's `.env`. It is Grounder's public
sign-in client, the same for everyone and not a secret. Hermes turns `web_search` and `web_extract` off when the selected
backend is not ready, and this keeps them on. Until you connect your account, a search says how to sign in, and Hermes
may answer it from its keyless free tier as "What Hermes gets" describes.

## What Hermes gets

Each search returns up to 20 rows (Hermes asks for 5 unless told otherwise), in Grounder's order:

| Field | From Grounder |
|---|---|
| `title` | The result's title |
| `url` | The result's address |
| `description` | Its summary, or its text on one line: the passages that best match your search, chosen by Grounder to fit 800 characters |
| `position` | The number the answer cites it by. The results of one answer are numbered on from 1 across its searches and reads, and a page found twice keeps its first number |
| `date` | When it was published, when Grounder knows |

Where each result came from (whose it is, the feature and provider that found it, why it may be served) stays in Grounder's
records and in its API response; the model does not read it row by row, and the line under the answer names the providers.

The first result Grounder serves in a turn, a search or a read, ends with two notes the model then has for its answer: `pages`,
which says each text is part of its page, how to ask for another part and that Grounder saves no page to a file (Hermes's own
`web_extract` saves a long page's text for `read_file`, and a model expecting that guessed file names), then `cite_sources`, which
asks the model to cite each result it uses by its number in square brackets, like `[3]`, and to write no Sources list, since
the plugin adds one. The turn's later results carry neither, which saves about 420 characters on each.

`web_extract` reads up to 5 pages in a call and returns one entry for each, in the order asked:

| Field | From Grounder |
|---|---|
| `url` | The address you asked for |
| `title` | The page's title, or its address when it has none |
| `content` | What in the page best answers the `web_search` that found it, or your latest message when no search found it, in the page's own order, at most 6,000 characters, or what the model's `char_limit` asks for up to 30,000. A page that fits comes back whole, and with no search and no message a page comes back from its start, or from the section its address names (`#Books`) |
| `error` | Only on a page that could not be read and that no search returned. It says why when Grounder knows: the address does not exist, the site refused the read, or the read ran out of time. The other pages still return. A page a search returned that cannot be read comes back without an error, as the text that search showed of it, which says so |

A `web_extract` keeps less than a whole page on purpose, to spare your model the tokens. Run a `web_search` first to get the
parts that matter. A read that is the turn's first Grounder result carries the two notes too.

For another part of a page, a `web_search` whose query holds the page's address reads that page for the query's other words,
for example `https://en.wikipedia.org/wiki/Jamie_Oliver books`. It returns the part that answers, at most 6,000 characters,
as the result's `description`, and counts as one search. Hermes's `web_extract` takes only addresses and answers a repeated read
from its own cache, so without this a model that needed more of a page fetched it with its own tools.
The pages you ask for always lead what Grounder returns, ahead of anything its search found beside them.

An answer that cites a Grounder result ends with a Sources list the plugin writes: one line for each result the answer cites
by number, with its title and link. The model writes only the numbers, which spares it the slowest tokens it makes; an
answer that wrote its own list, under a Sources or References heading, gets none added. Then an answer that searched ends with
one line saying who answered, how many sources came back and how long it took, for example
`Searched with Grounder · via Exa, Brave · 8 sources · 1.4 s`. Both are added by the plugin. Hermes keeps them with the answer,
so they are also in the history later turns send. A host that does not say which answer a search serves gets no list: there
the model is asked to write its own, as in any client.

A failed search returns `success: false` and one of these errors. Hermes then retries the search on its own keyless free
tier and, if that answers, returns those results with `rescued_from: grounder` and the error in `backend_error`. If it
does not, Hermes passes the error to the model. `web.keyless_fallback: false` in `config.yaml` turns off Hermes's keyless
tier.

| Error | What to do |
|---|---|
| Grounder rejected the search key in GROUNDER_API_KEY | Check the key on your Search keys page, or remove it to use the sign-in |
| Not signed in to Grounder | Set a search key, or run `hermes auth add grounder:grounder` |
| Grounder rejected the sign-in | Run `hermes auth add grounder:grounder` |
| Grounder sign-in failed | Follow Hermes's reason in the message, such as setting `GROUNDER_OAUTH_CLIENT_ID` |
| Grounder rate limit reached | Wait; the message says which limit |
| Could not reach Grounder | Check the network, then search again |

## More control: `grounder_search`

Hermes's `web_search` takes a query and a limit, so the plugin adds two tools, on by default and listed under `grounder` in
`hermes tools`. They add to what each model request carries, so turn the `grounder` toolset off if you do not want them.

`grounder_search` takes every option of Grounder's MCP search: `top_k`, `days`, `domains`, `features`, `urls`,
`include_text`, `requested_provider`, `providers`, `routing_settings`, `workspace`, `user_location` and `user_time_zone`.
It answers in `web_search`'s shape and counts as one search. `grounder_list_providers` lists the features a search can use
and the providers behind each, and is not a search.

Signing in grants searching only, so `user_info` and the workspace tools of Grounder's MCP server are not offered here. Manage
workspaces on your [account](https://grounder.net/account).

## What is sent

Each `web_search` query, up to its first 2,000 characters, goes to `https://api.grounder.net/v1/search` with the number
of results wanted, the 800 characters of text it wants for each result, your search key or, when you signed in, the
access token Hermes attaches, a user agent naming this plugin and its version, two random tokens, one for the session and one for the prompt, so one prompt's searches can be told from the next, and the name of the model whose answer asked for the search, as Hermes reports it. The tokens carry no Hermes id or message. Nothing else from Hermes goes with a search: not the conversation, the
model's other output or what other tools returned. When a search fails, Hermes may send the same query to its keyless
free tier, as "What Hermes gets" says; that is Hermes's own behaviour, not Grounder's.

Each `web_extract` sends the addresses of its pages, the characters of text it wants for each (6,000, or the model's `char_limit` up to 30,000), the same credential and
user agent, and a question to read them for: the query of the `web_search` that found them, when one did, and otherwise your
latest message in the session, up to its first 2,000 characters, so a page you paste or the model reads by address comes back
as the part that answers you rather than its first 6,000 characters. The plugin keeps both in memory only, for the last 500
pages found and the last 64 sessions. With neither, the addresses are sent as the query. The question is recorded as a
search's query is. Your other messages, and the rest of the conversation, are never sent.

A `grounder_search` sends what a `web_search` sends plus the options the model chose, including a location or time zone only
when the user stated one. `grounder_list_providers` reads Grounder's public feature list and sends no credential.

Searches are recorded under your Grounder account, as Grounder's [privacy policy](https://grounder.net/privacy) and
[data use](https://grounder.net/docs/data-use) pages describe. The plugin logs how many results a search
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
