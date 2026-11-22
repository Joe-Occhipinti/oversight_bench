# Skills

A skill is a set of instructions for one kind of task, stored in a `SKILL.md`
file, with any scripts, references or templates the instructions use. The
model sees a list of the available skills and uses a skill when the user names
it or when the task matches its description.

## Where skills are read

- `~/.meltzer/skills/`
- in a [trusted project](trust.md), `<session root>/.meltzer/skills/`

Each skill is a directory that contains a `SKILL.md` file. Skills can be
grouped in subdirectories, down to 6 levels below a skills directory; at most
2,000 directories are scanned per skills directory. Hidden directories (names
starting with `.`) and symbolic links are skipped.

The skills directories are scanned once, when the session starts; subagents
use the skills of the session that started them. A `SKILL.md` that cannot be read or is invalid is
skipped with a warning.

```
~/.meltzer/skills/
  deploy/
    SKILL.md
    scripts/deploy.sh
  review/
    SKILL.md
    references/checklist.md
```

## SKILL.md

The file starts with a frontmatter block between `---` lines, followed by the
instructions:

```markdown
---
name: deploy
description: Deploy the service to staging and check that it is healthy.
metadata:
  short-description: Deploy to staging
---
1. Run `scripts/deploy.sh staging`.
2. Wait for `make health ENV=staging` to pass.
3. Report the deployed version.
```

| Key | Meaning |
|---|---|
| `name` | the skill's name, at most 64 characters; default: the directory name |
| `description` | when to use the skill; shown to the model in the list of skills (required) |
| `metadata.short-description` | a shorter description, optional |

Other keys are ignored. Line breaks and repeated spaces in the name and the
descriptions are replaced by single spaces.

## The list of skills

When the session starts, meltzer adds a `## Skills` section to the system
message: one line per skill with its name, description and the path of its
`SKILL.md`, followed by instructions on how to use skills. Project skills come
first, then user skills, each in name order. Descriptions longer than 1,024
characters are shortened.

```toml
[skills]
include_instructions = false   # leave the section out of the system message
```

## Using a skill

Name a skill in a prompt with `$name`, or with a link to its file,
`[$name](/path/to/SKILL.md)`:

```
$deploy the current main branch
```

meltzer reads the skill's `SKILL.md` when the prompt is submitted and adds it
to the conversation after the prompt, so the model has the full instructions
for that turn. A `$name` mention applies when exactly one skill has that name;
when a project skill and a user skill share a name, use the link form. Names
of common environment variables (`$PATH`, `$HOME`, ...) are not read as
mentions.

Without a mention, the model reads a skill's `SKILL.md` itself when the task
matches its description. A skill's scripts run through the `shell` and
`exec_command` tools, with the same sandbox, rules and approvals as any other
command.

## Turning skills off

`[[skills.config]]` entries in `~/.meltzer/config.toml` turn skills off and on,
by name or by the path of the `SKILL.md`:

```toml
[[skills.config]]
name = "deploy"
enabled = false

[[skills.config]]
path = "~/.meltzer/skills/review/SKILL.md"
enabled = false
```

Entries apply in order, so a later entry for the same skill wins. A skill that
is turned off is not in the list of skills and is not added by a mention. These
entries are read only from the user config and the command line.

## Configuration

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `skills.include_instructions` | `true` | add the list of skills to the system message |
| `skills.config` | `[]` | entries with `name` or `path`, and `enabled` (user config only) |
