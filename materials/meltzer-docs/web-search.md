# Web search

The `web_search` tool lets the model search the web. meltzer sends the query to
a search service you set up and returns the results to the model as text. The
request is made by meltzer itself, not by a command in the sandbox.

## Turning it on

Web search is off by default. Set both keys in your user config
(`~/.meltzer/config.toml`):

```toml
[web_search]
enabled = true
endpoint = "https://search.example.com/v1/search"
api_key_env = "SEARCH_API_KEY"
```

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `web_search.enabled` | `false` | register the `web_search` tool |
| `web_search.endpoint` | unset | URL of the search service (`http` or `https`) |
| `web_search.api_key_env` | unset | name of the environment variable that holds the API key; when set, the key is sent as `Authorization: Bearer <key>` |

The three keys are read only from the user config and the command line.

The tool is registered only when `enabled` is true and `endpoint` is set. An
`endpoint` that is not an `http` or `https` URL stops the session with an error.
Subagents get the tool too.

## The tool

```json
{"query": "python 3.12 release notes", "max_results": 5}
```

| Argument | Meaning |
|---|---|
| `query` | the search query (required) |
| `max_results` | number of results, 1 to 10; default 5 |

The result lists each hit with its title, URL and snippet:

```
Results for "python 3.12 release notes":

1. What's New In Python 3.12
   https://docs.python.org/3/whatsnew/3.12.html
   This article explains the new features in Python 3.12.
```

Long results are cut to the same length as `shell` output.

## Approval

Every search goes to the reviewer (see [approvals](approvals.md)) before the
query is sent. The reviewer sees the query; there is no "always allow" and no
approval for the session. With `approval_policy = "never"` every search is
rejected.

If the host of `endpoint` is listed in `network.denied_domains` (an exact host
or IP address, or `*.domain` for its subdomains), searches are refused without
review.

## Search service API

meltzer sends one request per search:

```
POST <endpoint>
Content-Type: application/json

{"query": "python 3.12 release notes", "max_results": 5}
```

and expects a JSON response:

```json
{
  "results": [
    {"title": "What's New In Python 3.12", "url": "https://docs.python.org/3/whatsnew/3.12.html", "snippet": "..."}
  ]
}
```

Missing `title`, `url` or `snippet` fields are left empty. Results beyond
`max_results` are dropped.

## Errors

These are returned to the model as tool errors; the session goes on:

- the API key variable named by `api_key_env` is not set;
- the reviewer denies the search;
- the service does not answer within 30 seconds, or cannot be reached;
- the service answers with a status other than 2xx (redirects are not followed);
- the response is not JSON or has no `results` list.
