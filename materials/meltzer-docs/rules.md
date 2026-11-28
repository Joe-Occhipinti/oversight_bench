# Rules

Prefix rules decide how matching commands of the `shell` and `exec_command`
tools run. An allow rule matches a command that runs the allowed program and
lets it run outside the sandbox without review; a prompt rule sends the command
to the reviewer; a forbidden rule rejects it.

## Where rules are read

- `~/.meltzer/rules/*.rules`
- in a [trusted project](trust.md), the project's `.meltzer/rules/*.rules`

Files are read in name order when a session starts. Subagents use the rules of
their parent session. A rule that cannot be read or is invalid is skipped with a
warning; the other rules of the file still load.

## Format

A rules file has one `prefix_rule(...)` call per rule, with keyword arguments:

```python
prefix_rule(pattern=["cargo", "check"], decision="allow")
prefix_rule(pattern=["git", ["status", "diff", "log"]])
prefix_rule(
    pattern=["git", "push"],
    decision="prompt",
    justification="Pushes need a review.",
    match=["git push origin main"],
    not_match=["git pull"],
)
prefix_rule(pattern=["rm", "-rf"], decision="forbidden", justification="Use trash instead.")
```

| Argument | Meaning |
|---|---|
| `pattern` | the first words of a command; a nested list gives alternatives for one position (required) |
| `decision` | `allow` (default), `prompt` or `forbidden` |
| `justification` | why the rule exists; shown to the model when the rule rejects a command, and with the result of a prompt review |
| `match` | example commands the rule must match |
| `not_match` | example commands the rule must not match |

An allow rule whose pattern is too broad is rejected with a warning: a shell or
an interpreter followed only by options (`bash -c`, `python3 -m`), a program
that runs another command (`env`, `sudo`, `xargs`, `timeout`, `npx`, ...),
`git` alone, or a script runner such as `npm run` or `uv run` without a script
name. Prompt and forbidden rules may be broad.

## Decisions

- `allow`: the command runs outside the sandbox, without review.
- `prompt`: the command goes to the reviewer set by `approvals_reviewer` (see
  [approvals](approvals.md)). If it is approved, it runs in the sandbox, or
  outside it when the model asked for escalation. With
  `approval_policy = "never"` it is rejected.
- `forbidden`: the command is rejected, and the justification is shown to the
  model.

A command that no rule matches runs as described in [approvals](approvals.md):
in the sandbox, or through review when the model asks for escalation.

## Matching

A rule matches a command whose first words equal its pattern: `cargo check
--all` matches `["cargo", "check"]`; `cargo checkout` does not. Words are read
as bash reads them, with quotes and backslashes removed and nothing expanded:
`git commit -m "fix: x"` is the four words `git`, `commit`, `-m`, `fix: x`.

Commands joined with `&&`, `||`, `;` or `|` are matched one by one. When they
match rules with different decisions, the strictest applies: `forbidden`, then
`prompt`, then `allow`. With the rules above, `git status && git push` needs
review and `git status; rm -rf build` is rejected.

## Examples

`match` and `not_match` work as tests of a rule. They are checked when the
rules file is loaded: if a `match` example does not match the rule, or a
`not_match` example does, the rule is rejected with a warning. An example is a
string, split into words as above (`"git push origin main"`), or a list of
words (`["git", "push", "origin", "main"]`). A string example must be a single
command.

## "Always allow"

When the user reviewer approves a command with "always allow", an allow rule
for the command's words is appended to `~/.meltzer/rules/default.rules` and
applies from then on. It is offered for a single command whose words would not
make a too-broad allow rule.

## Checking a command

`meltzer execpolicy check` shows what rules files decide for a command:

```sh
meltzer execpolicy check --rules ~/.meltzer/rules/default.rules -- git push origin main
meltzer execpolicy check -r team.rules -r default.rules --pretty -- "cargo check && git status"
```

`--rules` (`-r`) may be repeated. A single argument after `--` is read as a
command line, as the model writes it; several arguments are the words of one
command. The output is JSON:

```json
{
  "matchedRules": [
    {
      "prefixRuleMatch": {
        "matchedPrefix": ["git", "push"],
        "decision": "prompt",
        "justification": "Pushes need a review."
      }
    }
  ],
  "decision": "prompt"
}
```

`matchedRules` lists every rule that matches the command; `decision` is the
decision meltzer applies, and is left out when no rule matches
(`{"matchedRules":[]}`). Rejected rules are reported on stderr. The exit code is
`0`, or `2` if a rules file cannot be read.
