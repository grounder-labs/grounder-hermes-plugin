# Grounder for Hermes Agent

Licensed, live sources for Hermes Agent's `web_search`. With Grounder as the web search backend, one search reaches the
providers the query needs, such as Exa, Brave, Tavily and Perplexity, and live data for scores, prices, odds and flights,
and comes back as one deduplicated list.

Every result is licensed: no site's robots.txt, license or AI opt-out refuses the provider that found it. A search can
return fewer results than asked for, and is never filled with unlicensed ones.

An account gets 50 free searches a day, with no card needed ([pricing](https://grounder.net/pricing)). Grounder is search
only: pair it with any extract backend for `web_extract`.

## Install

Needs Hermes Agent 0.18.1 or later.

```sh
hermes plugins install grounder-labs/grounder-hermes-plugin --enable
```

Once Grounder is listed in the Hermes plugin catalog, `hermes plugins install grounder --enable` installs the same plugin.

## Sign in

```sh
hermes grounder login
```

The browser opens Grounder's sign-in page. Sign in or create an account, then allow Hermes Agent to search for it. There
is no key to copy: the plugin saves a search-only grant with mode 0600 at `$HERMES_HOME/grounder/oauth.json` and never
prints it. The grant searches and nothing else; it cannot reach your account's settings, keys or billing. The access token
lasts an hour and renews itself on the next search.

| Command | What it does |
|---|---|
| `hermes grounder login` | Signs in, or signs in again after `logout` or an expired grant |
| `hermes grounder status` | Shows which credential searches use and how long the access token has left |
| `hermes grounder logout` | Deletes the saved grant |

### On a server without a browser

Pin the callback port and forward it from the machine whose browser you sign in with. On that machine:

```sh
ssh -L 8765:127.0.0.1:8765 <host>
```

Then, in that SSH session:

```sh
hermes grounder login --no-browser --port 8765
```

Open the address it prints in your browser. Any free port works; pick another if 8765 is taken on either machine.

Without a tunnel, sign-in ends on a page that cannot connect to `127.0.0.1`. Copy that page's full address from the
browser and, on the host within three minutes of starting `login`, run `curl '<the address>'` to finish.

### A Grounder search key

A search key from your account's [Search keys](https://grounder.net/account/keys) page works too (a search-only key is
enough), and is used ahead of any sign-in:

```sh
hermes config set GROUNDER_API_KEY <your-key>
```


## Select it

```sh
hermes config set web.search_backend grounder
```

Or choose Grounder under web search in `hermes tools`, or set it in `config.yaml` beside an extract backend:

```yaml
web:
  search_backend: grounder
  extract_backend: firecrawl
```

## What Hermes gets

Each search returns up to 20 rows (Hermes asks for 5 unless told otherwise), in Grounder's order:

| Field | From Grounder |
|---|---|
| `title` | The result's title |
| `url` | The result's address |
| `description` | Its summary, or its text on one line: the passages that best match your search, chosen by Grounder to fit 800 characters |
| `position` | Its rank, from 1 |

A failed search returns `success: false` and one of these errors, which Hermes passes to the model:

| Error | What to do |
|---|---|
| Not signed in to Grounder | Run `hermes grounder login` |
| Grounder sign-in has expired | Run `hermes grounder login` |
| Grounder rejected the credentials | Run `hermes grounder login`, or check `GROUNDER_API_KEY` |
| Grounder rate limit reached | Wait; the message says which limit |
| Could not reach Grounder | Check the network, then search again |

## What is sent

Each `web_search` query, up to its first 2,000 characters, goes to `https://api.grounder.net/v1/search` with the number
of results wanted, the 800 characters of text it wants for each result, your access token and a user agent naming this
plugin and its version. Nothing else from Hermes is
sent: not the conversation, the model's other output or what other tools returned.

Searches are recorded under your Grounder account, as Grounder's [privacy policy](https://grounder.net/privacy) and
[data use](https://grounder.net/docs/data-use) pages describe. The plugin logs how many results a search
returned, never the query.

## Development

Link a checkout into your Hermes home to try a change:

```sh
ln -snf "$PWD" "${HERMES_HOME:-$HOME/.hermes}/plugins/grounder"
hermes plugins enable grounder
```

The tests run against a local stub of the Grounder API and never call the live one. Run them in the Python environment
Hermes Agent is installed in, since the plugin imports its web search interface:

```sh
python -m pytest tests
```

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 Grounder Labs, Inc. The license grants
no rights to the Grounder name or marks.
