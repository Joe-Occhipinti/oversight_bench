# Telemetry and feedback

meltzer can send events about a session to an OpenTelemetry collector, and an
interactive session can send a feedback report. Both are off by default.

## Events

| Event | Attributes |
|---|---|
| `meltzer.conversation_starts` | `model`, `approval_policy`, `reviewer`, `sandbox_mode` |
| `meltzer.api_request` | `model`, `duration_ms`, `status` (`ok` or `error`) |
| `meltzer.tool_decision` | `tool_name`, `decision` (`approved`, `approved_for_session`, `denied`), `source` (`config`, `user`, `automated_reviewer`) |
| `meltzer.tool_result` | `tool_name`, `success`, `duration_ms` |
| `meltzer.sandbox_outcome` | `sandboxed`, `exit_code`, `duration_ms` |

Every event also has `session.id` and `event.name`. Events never contain
prompts, tool arguments, tool output or file contents. `tool_name` is the name
of the tool, up to 64 letters, digits, `_` and `-`; any other name is recorded
as `unknown`. A tool decision is recorded for each request that goes to review. Subagents record their events
with the id of the top-level session.

## Exporting events

Set both keys in your user config (`~/.meltzer/config.toml`):

```toml
[analytics]
enabled = true

[otel]
exporter = { otlp-http = { endpoint = "http://localhost:4318/v1/logs", headers = { "x-api-key" = "..." }, protocol = "json" } }
```

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `analytics.enabled` | `false` | collect events for the exporter |
| `otel.exporter` | `"none"` | `"none"`, or `{ otlp-http = { endpoint, headers, protocol } }` |
| `otel.exporter.otlp-http.endpoint` | (required) | full URL of the collector's OTLP/HTTP logs endpoint (`http` or `https`) |
| `otel.exporter.otlp-http.headers` | `{}` | extra HTTP headers, for example an API key |
| `otel.exporter.otlp-http.protocol` | `"json"` | only `"json"` is supported |

`[otel]` is read only from the user config and the command line; in a project
config it is ignored with a warning. An invalid exporter stops the session with
a config error. Events are sent as OTLP log records (`service.name =
"meltzer"`) by meltzer itself, in one request at the end of each turn and when
the session ends. If the collector cannot be reached or returns an error,
meltzer shows a warning and the session goes on without exporting; the events
of that request are not sent again.

## Feedback

```toml
[feedback]
enabled = true
```

With feedback enabled, an interactive session keeps its recent log lines (the
events above, up to 4 MiB) in memory. `/feedback [note]` writes the note and
those lines to a report file, `~/.meltzer/feedback/<session id>-<time>.txt`, and
asks before sending it to the address in `feedback.endpoint` (user config);
nothing is sent unless you answer yes. `/feedback` is not available in
`meltzer exec`.

`feedback.enabled` and `analytics.enabled` are read only from the user config
and the command line, like `[otel]`.
