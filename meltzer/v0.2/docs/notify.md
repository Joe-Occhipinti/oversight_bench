# Notify

`notify` runs a program after each turn of the main session, with one JSON
argument:

```json
{"type": "agent-turn-complete", "thread-id": "...", "turn-id": "...", "cwd": "...",
 "client": "meltzer", "input-messages": ["..."], "last-assistant-message": "..."}
```

```toml
notify = ["python3", "/home/me/.meltzer/notify.py"]
```
