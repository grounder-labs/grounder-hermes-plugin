# Grounder is installed

The next time Hermes starts, Grounder becomes this profile's `web_search` and `web_extract` backend, replacing any backend
chosen for them before. Each query and page address then goes to `api.grounder.net`. To use another backend, pick it in
`hermes tools`; that choice stands.

Searches work now with no account, as Grounder's free searches. Connect your account for more:

```sh
hermes config set GROUNDER_OAUTH_CLIENT_ID https://api.grounder.net/oauth/clients/hermes-agent
hermes auth add grounder:grounder
```

Or use a search key from https://grounder.net/account/keys:

```sh
hermes config set GROUNDER_API_KEY <your-key>
```

Once connected, `/search` chooses the server Grounder always searches with and `/search_presets` the mode it searches in.
`/search auto` and `/search_presets auto` let Grounder pick again.
