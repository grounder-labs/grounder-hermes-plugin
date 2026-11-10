# Grounder is installed

The next time Hermes starts, Grounder becomes this profile's `web_search` and `web_extract` backend, replacing any backend
chosen for them before. Each query and page address then goes to api.grounder.net. To use another backend, pick it in
`hermes tools`; that choice stands.

Connect your account, free for 50 searches a day:

```sh
hermes config set GROUNDER_OAUTH_CLIENT_ID https://api.grounder.net/oauth/clients/hermes-agent
hermes auth add grounder:grounder
```

Or use a search key from https://grounder.net/account/keys:

```sh
hermes config set GROUNDER_API_KEY <your-key>
```
