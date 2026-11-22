# Network

By default, sandboxed commands have no network access. With network access
on, they reach the network only through meltzer's network proxy, which allows
the hosts you list and asks the reviewer about other hosts.

## Turning it on

```toml
sandbox_mode = "workspace-write"

[network]
enabled = true
allowed_domains = ["pypi.org", "files.pythonhosted.org", "*.github.com"]
denied_domains = ["gist.github.com"]
```

`network.enabled = true` gives commands in the `workspace-write` sandbox
network access through the proxy. A [permission profile](permissions.md) turns
it on with `[permissions.<name>.network] enabled = true`. Commands that run
without a sandbox (`full-access`, or an approved escalation) use the host
network directly and do not go through the proxy.

## Keys

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `network.enabled` | `false` | network access for sandboxed commands, through the proxy |
| `network.allowed_domains` | `[]` | hosts that commands may reach without review |
| `network.denied_domains` | `[]` | hosts that commands may never reach |
| `network.allow_local_binding` | `false` | `true`: commands can reach services on the host's loopback addresses (`127.0.0.1`, `::1`) through the proxy; like other hosts, they are reached when listed or approved. User config and command line only |

Entries of both lists are:

- an exact host name: `pypi.org` matches `pypi.org` only;
- `*.domain`: `*.github.com` matches `api.github.com` and `a.b.github.com`, not
  `github.com` (list both to allow both);
- an IP address: `10.0.0.5`, `2001:db8::1`.

Other forms (`*`, `**.domain`, address ranges, ports, URLs) are a
configuration error. Host names are compared without case and without a
trailing dot. Lists from the user config, a trusted project config and the
command line are merged.

## How commands use the proxy

Each sandboxed command gets a proxy of its own, which runs while the command
runs. The command still has its own network namespace: it sees only a
loopback interface, where a small bridge forwards connections to the proxy.
meltzer sets these environment variables for the command:

```
HTTP_PROXY  HTTPS_PROXY  ALL_PROXY   http://127.0.0.1:<port>
http_proxy  https_proxy  all_proxy   http://127.0.0.1:<port>
NO_PROXY    no_proxy                 localhost,127.0.0.1,::1
```

Programs that read these variables (curl, pip, git over HTTPS, npm, Python
`urllib` and `requests`, and most package managers) work unchanged. A program
that connects directly, without the proxy, gets no connection. The proxy
handles `CONNECT host:port` (HTTPS and other TCP protocols tunnelled through
it) and plain HTTP requests in absolute form (`GET http://host/path`). Host
names are resolved by the proxy; there is no DNS in the sandbox.

## Decisions

Every request is decided on its own, in this order:

1. A host on `denied_domains` is refused.
2. An IP address that is loopback, private, link-local or multicast is refused
   unless it is listed in `allowed_domains` (with `allow_local_binding`,
   loopback addresses are allowed).
3. A host on `allowed_domains` is allowed.
4. Any other host goes to the reviewer.

An IPv6 address is compared without its scope id (`%eth0`), and an IPv4-mapped
address (`::ffff:10.0.0.1`) as the IPv4 address it holds. Addresses in
`64:ff9b::/96` and `::/96` count as local addresses.

When a host name is allowed, the proxy resolves it and connects to the
addresses it resolved. If one of them is loopback, private, link-local or
multicast and not listed, or is an IP address on `denied_domains`, the
request is refused. A plain HTTP request goes to the host in its URL, and the
`Host` header is set to that host.

A refused request gets an HTTP `403` response with the reason in the body,
for example:

```
HTTP/1.1 403 Forbidden

meltzer network proxy: gist.github.com is on the denied list (network.denied_domains)
```

A malformed request gets `400`, and a host that cannot be resolved or reached
gets `502`.

## Review

A host that is not on either list goes through `approval_policy` (see
[approvals](approvals.md)):

- `on-request`: the reviewer sees the host, port and protocol (`https` for
  `CONNECT`, `http` for plain HTTP requests) and the command that made the
  request. The command waits for the answer. The user reviewer is asked on
  the terminal; the monitor reviews it like any other request.
  `PermissionRequest` [hooks](hooks.md) run first and can deny it.
- `never`: the request is refused.

The user is asked to approve once, approve for the session, or deny. An
approval for the session holds for the rest of the session, for the same host,
port and protocol: later requests to it, from any command of the session, are
allowed without asking again. Every other approval (the monitor's, or the
user's "once") covers that request only. After that approval, or a denial, the
next request to the same host goes to the reviewer again. Approvals are not
saved: a new session starts with the lists of the configuration only.

## Example

With the configuration above and `approval_policy = "on-request"`:

- `pip install requests` downloads from `pypi.org` and
  `files.pythonhosted.org` without review;
- `git clone https://github.com/user/repo` goes to the reviewer
  (`github.com` is not matched by `*.github.com`);
- `curl https://gist.github.com/...` is refused;
- `curl --noproxy '*' https://pypi.org` fails: the command has no direct
  network access.
