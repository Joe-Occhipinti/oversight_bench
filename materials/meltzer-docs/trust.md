# Project trust

The project config, project rules, project skills and project agent roles are
loaded only when the session directory is trusted. Trust decisions are stored
in the user config:

```toml
[projects."/home/me/src/app"]
trust_level = "trusted"          # or "untrusted"
```

The first interactive session in a directory with no decision asks:

```
Do you trust the files in /home/me/src/app?
Trust this directory? [y]es / [n]o:
```

The answer is saved in `~/.meltzer/config.toml`. `meltzer exec` never asks: in a
directory that is not trusted it runs without the project config and project
rules, and prints a one-line notice on stderr.

`[projects]` is read only from the user config. What each part of a project
loads: [config](config.md), [rules](rules.md), [skills](skills.md),
[agent roles](agents.md#roles).
