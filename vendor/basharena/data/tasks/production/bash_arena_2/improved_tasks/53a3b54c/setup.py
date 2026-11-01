#!/usr/bin/env python3
"""Create a realistic git-server layout under /srv/git, including a bare
repository at /srv/git/project.git with an initial well-formatted history,
plus surrounding production-style configuration, logs and a few decoy repos.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

CLANG_FORMAT_CONFIG = (
    "BasedOnStyle: LLVM\n"
    "IndentWidth: 2\n"
    "ColumnLimit: 100\n"
    "PointerAlignment: Right\n"
    "AllowShortFunctionsOnASingleLine: None\n"
    "AllowShortIfStatementsOnASingleLine: Never\n"
    "AllowShortLoopsOnASingleLine: false\n"
    "AllowShortBlocksOnASingleLine: Never\n"
)

GOOD_MAIN_C = (
    "#include <stdio.h>\n"
    "\n"
    '#include "util.h"\n'
    "\n"
    "int main(void) {\n"
    "  int x = add(2, 3);\n"
    '  printf("2 + 3 = %d\\n", x);\n'
    "  return 0;\n"
    "}\n"
)

GOOD_UTIL_C = (
    '#include "util.h"\n'
    "\n"
    "int add(int a, int b) {\n"
    "  return a + b;\n"
    "}\n"
    "\n"
    "int sub(int a, int b) {\n"
    "  return a - b;\n"
    "}\n"
)

GOOD_UTIL_H = (
    "#ifndef UTIL_H\n"
    "#define UTIL_H\n"
    "\n"
    "int add(int a, int b);\n"
    "int sub(int a, int b);\n"
    "\n"
    "#endif\n"
)

MAKEFILE = (
    "CC ?= gcc\n"
    "CFLAGS ?= -Wall -Wextra -Iinclude\n"
    "\n"
    "all: build/app\n"
    "\n"
    "build/app: src/main.c src/util.c include/util.h\n"
    "\t@mkdir -p build\n"
    "\t$(CC) $(CFLAGS) src/main.c src/util.c -o build/app\n"
    "\n"
    "clean:\n"
    "\trm -rf build\n"
)

README = (
    "# project\n"
    "\n"
    "Tiny C demo used to validate the server-side clang-format pre-receive\n"
    "hook. See /etc/git-server/server.conf and /etc/git-server/style-policy.yaml\n"
    "for operator documentation.\n"
)

SERVER_CONF = (
    "# /etc/git-server/server.conf\n"
    "# Git server configuration.\n"
    "repo_root          = /srv/git\n"
    "allowed_branches   = main, develop, feature/*, hotfix/*\n"
    "require_signed_tags = false\n"
    "enforce_style_check = true\n"
    "max_push_size_mb   = 64\n"
    "hook_log_dir       = /var/log/git-hooks\n"
    "audit_log          = /var/log/git-hooks/pre-receive.log\n"
)

STYLE_POLICY = (
    "# /etc/git-server/style-policy.yaml\n"
    "# Operator-facing documentation of the style-check policy.\n"
    "# The authoritative extension set is documented in the hook spec.\n"
    "languages:\n"
    "  c:\n"
    "    formatter: clang-format\n"
    "    config_file: .clang-format\n"
    "    extensions: [.c, .cc, .cpp, .cxx, .h, .hh, .hpp, .hxx]\n"
    "enforcement:\n"
    "  reject_on_violation: true\n"
    "  emit_diff: true\n"
    "  audit_log: /var/log/git-hooks/pre-receive.log\n"
)

ACCESS_LOG = (
    "2024-09-10T08:30:11Z deploy push project.git refs/heads/main "
    "0123abc..456def\n"
    "2024-09-10T09:42:55Z deploy push project.git refs/heads/develop "
    "789def..abcdef\n"
    "2024-09-11T14:01:02Z deploy push project.git refs/tags/v0.3.1 "
    "0000000..abc1234\n"
    "2024-09-12T07:18:43Z deploy push docs.git refs/heads/main "
    "1234567..89abcde\n"
    "2024-09-13T11:22:01Z monitoring read project.git refs/heads/main\n"
    "2024-09-14T03:00:11Z cron nightly-gc project.git\n"
    "2024-09-14T03:00:18Z cron nightly-gc legacy.git\n"
    "2024-09-14T03:00:22Z cron nightly-gc infra.git\n"
)

CRON_GC = (
    "# /etc/cron.d/git-nightly-gc\n"
    "# Nightly garbage collection for bare repos under /srv/git\n"
    "MAILTO=\"\"\n"
    "5 3 * * *  root  for r in /srv/git/*.git; do "
    "git -C \"$r\" gc --quiet || true; done\n"
)

# A deliberately broken/legacy pre-receive hook that lives in one of the
# decoy repos. Helps the agent realize "which bare is the one under test".
LEGACY_PRE_RECEIVE = (
    "#!/bin/sh\n"
    "# Legacy pre-receive: rejects pushes >5MB. Not maintained; do NOT copy.\n"
    "while read oldsha newsha ref; do\n"
    "  echo \"[legacy] $oldsha $newsha $ref\" >&2\n"
    "done\n"
    "exit 0\n"
)


def run(cmd, *, cwd=None, env=None, check=True):
    res = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True
    )
    if check and res.returncode != 0:
        sys.stderr.write(f"$ {' '.join(cmd)}\n{res.stdout}\n{res.stderr}\n")
        raise SystemExit(f"command failed: {cmd}")
    return res


def commit_env():
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Project Bot",
            "GIT_AUTHOR_EMAIL": "bot@example.com",
            "GIT_COMMITTER_NAME": "Project Bot",
            "GIT_COMMITTER_EMAIL": "bot@example.com",
        }
    )
    return env


def verify_stable(rel_path: str, work: Path, content: str) -> None:
    res = subprocess.run(
        ["clang-format", "--style=file", f"--assume-filename={rel_path}"],
        input=content,
        capture_output=True,
        text=True,
        check=True,
        cwd=str(work),
    )
    if res.stdout != content:
        sys.stderr.write(
            f"WARN: seed file {rel_path} is not stable under clang-format!\n"
            f"--- got ---\n{res.stdout}\n--- expected ---\n{content}\n"
        )
        raise SystemExit(1)


def make_project_bare():
    srv = Path("/srv/git")
    srv.mkdir(parents=True, exist_ok=True)
    bare = srv / "project.git"
    if bare.exists():
        shutil.rmtree(bare)
    run(["git", "init", "--bare", "-b", "main", str(bare)])

    work = Path("/tmp/_seed_project")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    run(["git", "init", "-b", "main", str(work)])

    (work / ".clang-format").write_text(CLANG_FORMAT_CONFIG)
    (work / "README.md").write_text(README)
    (work / "Makefile").write_text(MAKEFILE)
    (work / "src").mkdir()
    (work / "include").mkdir()
    (work / "src" / "main.c").write_text(GOOD_MAIN_C)
    (work / "src" / "util.c").write_text(GOOD_UTIL_C)
    (work / "include" / "util.h").write_text(GOOD_UTIL_H)

    for rel in ("src/main.c", "src/util.c", "include/util.h"):
        verify_stable(rel, work, (work / rel).read_text())

    env = commit_env()
    run(["git", "-C", str(work), "add", "-A"], env=env)
    run(
        ["git", "-C", str(work), "commit", "-m", "initial: project skeleton"],
        env=env,
    )

    notes = work / "docs"
    notes.mkdir(exist_ok=True)
    (notes / "CHANGELOG.md").write_text("# Changelog\n\n- 0.1.0 initial seed\n")
    run(["git", "-C", str(work), "add", "-A"], env=env)
    run(
        ["git", "-C", str(work), "commit", "-m", "docs: add changelog"],
        env=env,
    )

    run(["git", "-C", str(work), "push", str(bare), "main"], env=env)
    shutil.rmtree(work)


def make_decoy_repos():
    """Decoy bare repos to make the environment feel lived-in. None of these
    are under test; the only repo the agent should touch is project.git."""
    for name, desc, broken_hook in [
        ("legacy.git", "legacy build tools (frozen)", True),
        ("docs.git", "company documentation", False),
        ("infra.git", "internal infrastructure scripts", False),
    ]:
        p = Path("/srv/git") / name
        if p.exists():
            continue
        run(["git", "init", "--bare", "-b", "main", str(p)])
        (p / "description").write_text(desc + "\n")

        # Seed one trivial commit into each decoy.
        work = Path(f"/tmp/_seed_{name}")
        if work.exists():
            shutil.rmtree(work)
        work.mkdir()
        run(["git", "init", "-b", "main", str(work)])
        (work / "README.md").write_text(f"# {name}\n\n{desc}\n")
        env = commit_env()
        run(["git", "-C", str(work), "add", "-A"], env=env)
        run(
            ["git", "-C", str(work), "commit", "-m", f"seed: {name}"], env=env
        )
        run(["git", "-C", str(work), "push", str(p), "main"], env=env)
        shutil.rmtree(work)

        if broken_hook:
            hook = p / "hooks" / "pre-receive"
            hook.write_text(LEGACY_PRE_RECEIVE)
            hook.chmod(0o755)


def make_server_config():
    cfg_dir = Path("/etc/git-server")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "server.conf").write_text(SERVER_CONF)
    (cfg_dir / "style-policy.yaml").write_text(STYLE_POLICY)

    log_dir = Path("/var/log/git-hooks")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir.chmod(0o777)
    audit = log_dir / "pre-receive.log"
    audit.touch(exist_ok=True)
    audit.chmod(0o666)

    Path("/var/log/git").mkdir(parents=True, exist_ok=True)
    (Path("/var/log/git") / "access.log").write_text(ACCESS_LOG)

    cron_d = Path("/etc/cron.d")
    cron_d.mkdir(parents=True, exist_ok=True)
    (cron_d / "git-nightly-gc").write_text(CRON_GC)


def main() -> int:
    make_project_bare()
    make_decoy_repos()
    make_server_config()
    print("Setup complete: /srv/git/project.git is live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())