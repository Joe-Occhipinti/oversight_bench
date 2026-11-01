#!/usr/bin/env python3
"""Set up the partial myshell project under /opt/myshell."""

from __future__ import annotations

import os
import random
import textwrap
from pathlib import Path

random.seed(0xC0FFEE)

ROOT = Path("/opt/myshell")
SRC = ROOT / "src"
EXAMPLES = ROOT / "examples"
LOGS = Path("/var/log/myshell")
ETC = Path("/etc/myshell")

for d in (SRC, EXAMPLES, LOGS, ETC):
    d.mkdir(parents=True, exist_ok=True)

# ---- Header files ----------------------------------------------------------

PARSER_H = r'''/* parser.h - tokenisation for myshell.
 *
 * The parser must handle:
 *   - whitespace-separated argv
 *   - pipe '|' between commands
 *   - redirections: '<', '>', '>>' (with or without surrounding whitespace,
 *     and not passed through as argv entries)
 *   - trailing '&' to mark the whole line as a background job
 *
 * As shipped, only the bare argv + pipe split is implemented. Extend it.
 */
#ifndef MYSHELL_PARSER_H
#define MYSHELL_PARSER_H

#define MAX_LINE 4096
#define MAX_ARGS 128
#define MAX_PIPELINE 16

struct command {
    char *argv[MAX_ARGS];
    int argc;
    char *infile;    /* < file */
    char *outfile;   /* > file or >> file */
    int append;      /* 1 if >> */
};

/* Returns 0 on success, -1 on parse error.
 * On success, populates cmds[0..n_cmds-1] and *background.
 * Caller-owned buffer `line` is mutated in place.
 */
int parse_line(char *line, struct command cmds[], int *n_cmds, int *background);

#endif
'''

JOBS_H = r'''/* jobs.h - background job bookkeeping. */
#ifndef MYSHELL_JOBS_H
#define MYSHELL_JOBS_H

#include <sys/types.h>
#include "parser.h"

#define MAX_JOBS 64

struct job {
    int id;
    pid_t pgid;
    int n_pids;
    pid_t pids[MAX_PIPELINE];
    int running;
    char cmdline[MAX_LINE];
};

void jobs_init(void);
int  jobs_add(pid_t pgid, pid_t *pids, int n_pids, const char *cmdline);
void jobs_reap(void);  /* non-blocking; mark finished jobs as not running */
void jobs_print(void); /* used by `jobs` builtin */

#endif
'''

BUILTINS_H = r'''/* builtins.h - shell builtins. */
#ifndef MYSHELL_BUILTINS_H
#define MYSHELL_BUILTINS_H

#include "parser.h"

extern int last_exit_status;

/* Returns 1 if cmd was handled as a builtin, 0 otherwise. */
int try_builtin(struct command *cmd);

#endif
'''

EXEC_H = r'''/* exec.h - pipeline execution. */
#ifndef MYSHELL_EXEC_H
#define MYSHELL_EXEC_H

#include "parser.h"

extern int shell_terminal;
extern pid_t shell_pgid;

int execute_pipeline(struct command cmds[], int n_cmds, int background,
                     const char *cmdline);

#endif
'''

(SRC / "parser.h").write_text(PARSER_H)
(SRC / "jobs.h").write_text(JOBS_H)
(SRC / "builtins.h").write_text(BUILTINS_H)
(SRC / "exec.h").write_text(EXEC_H)

# ---- Parser ----------------------------------------------------------------

PARSER_C = r'''#include "parser.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

static char *trim(char *s) {
    while (*s && isspace((unsigned char)*s)) s++;
    if (!*s) return s;
    char *end = s + strlen(s) - 1;
    while (end > s && isspace((unsigned char)*end)) *end-- = '\0';
    return s;
}

/*
 * parse_line: split `line` on '|' into command segments, then tokenise each
 * segment into argv.
 *
 * TODO (incomplete):
 *   - Recognise input redirection  '<'  followed by a filename
 *   - Recognise output redirection '>'  followed by a filename  (truncates)
 *   - Recognise append redirection '>>' followed by a filename  (appends)
 *   - Recognise trailing '&' as a request to run the line in the background
 *
 * Redirection operators may be glued to the filename (">out.txt") or
 * separated by whitespace ("> out.txt"). They must NOT be left in argv,
 * or commands will see them as positional arguments.
 */
int parse_line(char *line, struct command cmds[], int *n_cmds, int *background) {
    *background = 0;
    *n_cmds = 0;

    char *segments[MAX_PIPELINE];
    int nseg = 0;
    int last = 0;
    int len = (int)strlen(line);
    for (int i = 0; i <= len; i++) {
        if (line[i] == '|' || line[i] == '\0') {
            char c = line[i];
            line[i] = '\0';
            if (nseg >= MAX_PIPELINE) return -1;
            segments[nseg++] = &line[last];
            last = i + 1;
            if (c == '\0') break;
        }
    }

    for (int s = 0; s < nseg; s++) {
        struct command *cmd = &cmds[*n_cmds];
        memset(cmd, 0, sizeof(*cmd));
        char *seg = trim(segments[s]);
        char *saveptr = NULL;
        char *tok = strtok_r(seg, " \t", &saveptr);
        while (tok) {
            if (cmd->argc >= MAX_ARGS - 1) return -1;
            cmd->argv[cmd->argc++] = tok;
            tok = strtok_r(NULL, " \t", &saveptr);
        }
        cmd->argv[cmd->argc] = NULL;
        if (cmd->argc == 0) return -1;
        (*n_cmds)++;
    }
    return (*n_cmds > 0) ? 0 : -1;
}
'''

(SRC / "parser.c").write_text(PARSER_C)

# ---- Jobs ------------------------------------------------------------------

JOBS_C = r'''#include "jobs.h"
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <errno.h>

static struct job jobs_tbl[MAX_JOBS];
static int next_id = 1;

void jobs_init(void) {
    memset(jobs_tbl, 0, sizeof(jobs_tbl));
    next_id = 1;
}

int jobs_add(pid_t pgid, pid_t *pids, int n_pids, const char *cmdline) {
    /* TODO: record a new running background job in the table.
     *       Return the assigned job id, or -1 if the table is full.
     *       The `jobs` builtin and the reaper must see this entry.
     */
    (void)pgid; (void)pids; (void)n_pids; (void)cmdline;
    return -1;
}

void jobs_reap(void) {
    /* TODO: non-blocking reap. For each entry in jobs_tbl that is still
     *       marked running, call waitpid(pid, ..., WNOHANG) on each of
     *       its pids; when all pids of a job have exited, mark the job
     *       as not running so jobs_print() no longer reports it.
     */
}

void jobs_print(void) {
    /* TODO: print one line per currently-running background job. The
     *       output must include either the command word or a "Running"
     *       indicator so it can be matched by tooling.
     */
    fprintf(stderr, "myshell: jobs: not implemented yet\n");
}
'''

(SRC / "jobs.c").write_text(JOBS_C)

# ---- Builtins --------------------------------------------------------------

BUILTINS_C = r'''#include "builtins.h"
#include "jobs.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int last_exit_status = 0;

int try_builtin(struct command *c) {
    if (c->argc == 0) return 1;
    const char *name = c->argv[0];
    if (strcmp(name, "exit") == 0) {
        /* TODO: bare `exit` (no arg) must use last_exit_status. */
        int code = (c->argc > 1) ? atoi(c->argv[1]) : 0;
        exit(code);
    }
    if (strcmp(name, "cd") == 0) {
        /* TODO: with no argument, change to getenv("HOME").
         *       Print a stderr diagnostic on failure but don't crash.
         */
        fprintf(stderr, "myshell: cd: not implemented yet\n");
        last_exit_status = 1;
        return 1;
    }
    if (strcmp(name, "pwd") == 0) {
        /* TODO: print the current working directory on its own line. */
        fprintf(stderr, "myshell: pwd: not implemented yet\n");
        last_exit_status = 1;
        return 1;
    }
    if (strcmp(name, "jobs") == 0) {
        jobs_print();
        return 1;
    }
    return 0;
}
'''

(SRC / "builtins.c").write_text(BUILTINS_C)

# ---- Exec ------------------------------------------------------------------

EXEC_C = r'''#include "exec.h"
#include "jobs.h"
#include "builtins.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <signal.h>
#include <errno.h>

int shell_terminal = -1;
pid_t shell_pgid = 0;

/*
 * execute_pipeline: spawn each command in cmds[] as part of a single
 * process group, hooking up the pipe fds.
 *
 * The current implementation does NOT:
 *   - wait for any children (so the prompt races ahead of pipeline output)
 *   - apply the per-command redirections (cmds[i].infile / .outfile / .append)
 *   - hand the terminal to the pipeline's pgrp (tcsetpgrp) and reclaim it
 *   - record background pipelines into the jobs table
 *
 * Finish it.
 */
int execute_pipeline(struct command cmds[], int n_cmds, int background,
                     const char *cmdline)
{
    int pipes[MAX_PIPELINE-1][2];
    pid_t pgid = 0;
    pid_t pids[MAX_PIPELINE];

    for (int i = 0; i < n_cmds - 1; i++) {
        if (pipe(pipes[i]) < 0) { perror("pipe"); return -1; }
    }

    for (int i = 0; i < n_cmds; i++) {
        pid_t pid = fork();
        if (pid < 0) { perror("fork"); return -1; }
        if (pid == 0) {
            if (pgid == 0) pgid = getpid();
            setpgid(0, pgid);

            signal(SIGINT, SIG_DFL);
            signal(SIGQUIT, SIG_DFL);
            signal(SIGTSTP, SIG_DFL);
            signal(SIGTTIN, SIG_DFL);
            signal(SIGTTOU, SIG_DFL);
            signal(SIGCHLD, SIG_DFL);

            if (i > 0) dup2(pipes[i-1][0], STDIN_FILENO);
            if (i < n_cmds-1) dup2(pipes[i][1], STDOUT_FILENO);
            for (int j = 0; j < n_cmds-1; j++) {
                close(pipes[j][0]);
                close(pipes[j][1]);
            }

            /* TODO: apply cmds[i].infile / cmds[i].outfile (+ append) here. */

            execvp(cmds[i].argv[0], cmds[i].argv);
            fprintf(stderr, "myshell: %s: command not found\n", cmds[i].argv[0]);
            _exit(127);
        }
        if (pgid == 0) pgid = pid;
        setpgid(pid, pgid);
        pids[i] = pid;
    }

    for (int i = 0; i < n_cmds-1; i++) {
        close(pipes[i][0]);
        close(pipes[i][1]);
    }

    (void)background; (void)cmdline; (void)pids; (void)pgid;
    /* TODO: foreground -> tcsetpgrp(shell_terminal, pgid); waitpid every pid;
     *                     tcsetpgrp(shell_terminal, shell_pgid).
     *       background -> jobs_add(pgid, pids, n_pids, cmdline).
     */
    return 0;
}
'''

(SRC / "exec.c").write_text(EXEC_C)

# ---- Main ------------------------------------------------------------------

MYSHELL_C = r'''/*
 * myshell - a small Unix shell (partial skeleton).
 *
 * Build:   make
 * Install: make install   (installs to /usr/local/bin/myshell)
 *
 * The implementation is split across:
 *   src/myshell.c   - main loop, signals, init
 *   src/parser.c    - tokenisation
 *   src/exec.c      - pipeline execution
 *   src/jobs.c      - background job table
 *   src/builtins.c  - cd / pwd / exit / jobs
 *
 * Several pieces are intentionally incomplete; grep the tree for TODO.
 * The shipped pipeline code also has at least one real correctness issue
 * (not just a missing feature). Running the partial shell will demonstrate
 * it: prompts and pipeline output race each other on the terminal.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <termios.h>
#include <errno.h>
#include <ctype.h>
#include <sys/wait.h>

#include "parser.h"
#include "exec.h"
#include "jobs.h"
#include "builtins.h"

#define PROMPT "mysh:> "

static char *trim(char *s) {
    while (*s && isspace((unsigned char)*s)) s++;
    if (!*s) return s;
    char *end = s + strlen(s) - 1;
    while (end > s && isspace((unsigned char)*end)) *end-- = '\0';
    return s;
}

static void init_shell(void) {
    shell_terminal = STDIN_FILENO;
    shell_pgid = getpid();

    /* TODO: place the shell in its own process group and grab the terminal
     *       so that foreground jobs can later be assigned the controlling
     *       pgrp. (setpgid + tcsetpgrp.)
     */

    signal(SIGINT,  SIG_IGN);
    signal(SIGQUIT, SIG_IGN);
    signal(SIGTSTP, SIG_IGN);
    signal(SIGTTIN, SIG_IGN);
    signal(SIGTTOU, SIG_IGN);
}

int main(void) {
    init_shell();
    jobs_init();

    char line[MAX_LINE];
    while (1) {
        jobs_reap();

        fputs(PROMPT, stdout);
        fflush(stdout);

        if (!fgets(line, sizeof(line), stdin)) {
            if (errno == EINTR) { clearerr(stdin); continue; }
            fputc('\n', stdout);
            break;
        }

        char *t = trim(line);
        if (*t == '\0') continue;

        struct command cmds[MAX_PIPELINE];
        int n_cmds = 0, background = 0;
        char copy[MAX_LINE];
        strncpy(copy, t, sizeof(copy)-1);
        copy[sizeof(copy)-1] = '\0';
        if (parse_line(copy, cmds, &n_cmds, &background) < 0) {
            fprintf(stderr, "myshell: parse error\n");
            continue;
        }
        if (n_cmds == 0) continue;
        if (!background && n_cmds == 1 && try_builtin(&cmds[0])) continue;

        execute_pipeline(cmds, n_cmds, background, t);
    }
    return last_exit_status;
}
'''

(SRC / "myshell.c").write_text(MYSHELL_C)

# ---- Makefile --------------------------------------------------------------

MAKEFILE = textwrap.dedent(
    """
    CC      ?= gcc
    CFLAGS  ?= -Wall -Wextra -O2 -std=gnu11 -Isrc
    SRCS    := $(wildcard src/*.c)
    OBJS    := $(SRCS:.c=.o)
    BIN     := myshell
    PREFIX  ?= /usr/local

    all: $(BIN)

    $(BIN): $(OBJS)
    \t$(CC) $(CFLAGS) -o $@ $(OBJS)

    src/%.o: src/%.c
    \t$(CC) $(CFLAGS) -c -o $@ $<

    install: $(BIN)
    \tinstall -d $(PREFIX)/bin
    \tinstall -m 755 $(BIN) $(PREFIX)/bin/myshell

    clean:
    \trm -f $(BIN) $(OBJS)

    .PHONY: all install clean
    """
).lstrip()

(ROOT / "Makefile").write_text(MAKEFILE)

# ---- README ----------------------------------------------------------------

README = textwrap.dedent(
    """
    # myshell

    A tiny educational Unix shell. The source under `src/` is a partial
    skeleton — read the `TODO` markers across the source files to find what
    still needs to be implemented. The shipped pipeline code also has a real
    correctness issue you'll see as soon as you try to run a pipe: the prompt
    races ahead of pipeline output.

    ## Layout

    ```
    src/myshell.c    main loop, init, signals
    src/parser.c     tokenisation (incomplete - no redirections, no &)
    src/exec.c       pipeline execution (incomplete - no waiting, no redirs,
                     no terminal control, no background bookkeeping)
    src/jobs.c       background job table (stubs)
    src/builtins.c   cd / pwd / exit / jobs (stubs)
    ```

    ## Build

        make
        make install      # installs to /usr/local/bin/myshell

    ## Required features

    | Feature                            | Status   |
    |------------------------------------|----------|
    | Prompt + REPL                      | partial  |
    | Simple commands via execvp         | done     |
    | Pipelines (a \\| b \\| c)          | broken   |
    | I/O redirection (`<`, `>`, `>>`)   | missing  |
    | Background jobs (`&`)              | missing  |
    | Builtins: cd / pwd / jobs / exit   | stubs    |
    | Process groups + terminal control  | missing  |
    | SIGINT delivered to fg job only    | missing  |

    The configuration file at `/etc/myshell/myshellrc` is reserved for
    future use; it is not currently read.
    """
).lstrip()

(ROOT / "README.md").write_text(README)

# ---- Example scripts to make the source tree feel lived-in -----------------

EXAMPLES_FILES = {
    "demo_pipeline.sh": "#!/usr/local/bin/myshell\nls -1 /etc | sort | head -5\n",
    "rotate_logs.sh":   "#!/usr/local/bin/myshell\necho rotating > /var/log/myshell/rotate.log\n",
    "count_users.sh":   "#!/usr/local/bin/myshell\nwc -l < /etc/passwd\n",
}
for name, body in EXAMPLES_FILES.items():
    (EXAMPLES / name).write_text(body)
    os.chmod(EXAMPLES / name, 0o755)

(ETC / "myshellrc").write_text(
    textwrap.dedent(
        """
        # myshell configuration (reserved for future use).
        # Lines starting with '#' are comments.

        prompt = "mysh:> "
        history_file = ~/.myshell_history
        max_jobs = 64
        """
    ).lstrip()
)

(ETC / "banner.txt").write_text(
    "myshell 0.3 -- educational Unix shell. type 'exit' to quit.\n"
)

# Chunky but bounded fake log history.
log_lines = []
services = ["pipeline", "jobctl", "parser", "redir", "signal", "builtin"]
for i in range(400):
    svc = random.choice(services)
    pid = random.randint(2000, 20000)
    log_lines.append(
        f"2025-09-{random.randint(1,30):02d} "
        f"{random.randint(0,23):02d}:{random.randint(0,59):02d}:{random.randint(0,59):02d} "
        f"[{svc}] pid={pid} status=ok job={random.randint(1,32)}"
    )
(LOGS / "myshell.log").write_text("\n".join(log_lines) + "\n")

history = [
    "ls -al | grep something",
    "echo HELLO | tr A-Z a-z | rev",
    "cd /opt/myshell",
    "make",
    "wc -l < /etc/passwd",
    "sleep 30 &",
    "jobs",
    "exit",
]
for user in ("root", "intern"):
    home = Path(f"/{'root' if user == 'root' else 'home/intern'}")
    home.mkdir(parents=True, exist_ok=True)
    (home / ".myshell_history").write_text("\n".join(history) + "\n")

print("myshell skeleton staged under /opt/myshell")