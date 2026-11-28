# Voice

`meltzer voice` holds a voice conversation with a realtime model (the OpenAI
Realtime API, over a WebSocket). The realtime model listens to your recorded
request and answers; when the request is about the code, files or commands, it
hands it to the agent, which works on it as a normal turn of the session. The
agent's final message goes back to the realtime model, which answers you.
Voice needs the `voice` extra: `pip install 'meltzer[voice]'`.

## Usage

```
meltzer voice --input request.wav [options]
```

`--input` is a WAV file with 24 kHz, 16-bit, mono PCM audio. The other options
are those of `meltzer exec` (`-C`, `-m`, `-s`, `-a`, `-c`). The session runs
without a terminal, as `meltzer exec` does, so approvals need
`approvals_reviewer = "monitor"` or `approval_policy = "never"`. Transcripts and
progress go to stderr; the realtime model's last answer goes to stdout.

## Config

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `realtime.base_url` | `wss://api.openai.com/v1/realtime` | WebSocket URL of the realtime service (`wss`, or `ws` for `localhost`); read only from the user config and the command line |
| `realtime.model` | `gpt-realtime` | realtime model, sent as the `model` query parameter |
| `realtime.voice` | `marin` | voice of the realtime model |

The API key is read from the same environment variable as the chat model's
(`api_key_env`) and sent as `Authorization: Bearer <key>`.

## How a conversation runs

1. meltzer connects and sends `session.update`: instructions, a short startup
   context (the workspace root and its files and directories to depth 2), and
   one function, `background_agent`.
2. The audio is sent with `input_audio_buffer.append` and
   `input_audio_buffer.commit`, then `response.create`.
3. When the realtime model calls `background_agent`, its `prompt` is submitted
   to the session as your prompt, on the same path as typed input: hooks,
   rules, the sandbox and approvals apply as usual. The history item records
   `source = "voice"`.
4. The agent's final message is sent back with `conversation.item.create`
   (`function_call_output`), followed by `response.create`.
5. The conversation ends when the realtime model answers without calling
   `background_agent`.

Each voice input makes one request. If the realtime model calls
`background_agent` again, the call is not run: meltzer shows a notice, answers
the call with a message saying so, and the conversation ends.

The agent's tools cannot start or continue a voice conversation.

## Errors

Exit code 1: the realtime service cannot be reached, sends no event within 120
seconds, sends an `error` event, or closes the connection early. Exit code 2,
before the conversation starts: an input file that is not 24 kHz 16-bit mono
PCM WAV, a `realtime.base_url` that is not a `wss` URL (or a `ws` URL for
`localhost`), or a missing `voice` extra.
