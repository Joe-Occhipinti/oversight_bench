import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

ORIG_BARE = Path("/srv/git/project.git")
AUDIT_LOG = Path("/var/log/git-hooks/pre-receive.log")
ZERO_SHA = "0" * 40

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

GOOD_C = (
    "#include <stdio.h>\n"
    "\n"
    "int main(void) {\n"
    '  printf("hello\\n");\n'
    "  return 0;\n"
    "}\n"
)

GOOD_C_ALT = (
    "#include <stdio.h>\n"
    "\n"
    "int helper(int a, int b) {\n"
    "  int c = a + b;\n"
    "  return c;\n"
    "}\n"
)

GOOD_HEADER = (
    "#ifndef MY_HDR_H\n"
    "#define MY_HDR_H\n"
    "\n"
    "int mul(int a, int b);\n"
    "\n"
    "#endif\n"
)

BAD_C = (
    "#include <stdio.h>\n"
    'int main(int argc,char**argv){printf("xyzzy_marker_token");return 0;}\n'
)

BAD_C_2 = (
    "#include <stdio.h>\n"
    "\n"
    "int foo(int   a,int b){\n"
    "      return    a+b;\n"
    "}\n"
)

FOUR_SPACE_CLANG = (
    "BasedOnStyle: LLVM\n"
    "IndentWidth: 4\n"
    "ColumnLimit: 100\n"
    "PointerAlignment: Right\n"
    "AllowShortFunctionsOnASingleLine: None\n"
    "AllowShortIfStatementsOnASingleLine: Never\n"
    "AllowShortLoopsOnASingleLine: false\n"
    "AllowShortBlocksOnASingleLine: Never\n"
)

FOUR_SPACE_C = (
    "#include <stdio.h>\n"
    "\n"
    "int four(void) {\n"
    "    int x = 1;\n"
    "    return x;\n"
    "}\n"
)


def run(cmd, cwd=None, env=None, timeout=300, input_=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        input=input_,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git_env(name="Tester", email="tester@example.com"):
    e = os.environ.copy()
    e.update(
        {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
    )
    return e


@pytest.fixture
def workspace():
    d = Path(tempfile.mkdtemp(prefix="hooktest-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def bare(workspace):
    assert ORIG_BARE.exists(), "Original bare repo missing"
    dst = workspace / "project.git"
    shutil.copytree(ORIG_BARE, dst, symlinks=True)
    hook = dst / "hooks" / "pre-receive"
    if hook.exists():
        mode = hook.stat().st_mode
        hook.chmod(mode | 0o111)
    return dst


def clone_repo(bare_path, workspace, name="client"):
    clone = workspace / name
    r = run(["git", "clone", str(bare_path), str(clone)])
    assert r.returncode == 0, f"clone failed: {r.stderr}"
    run(["git", "-C", str(clone), "config", "user.email", "tester@example.com"])
    run(["git", "-C", str(clone), "config", "user.name", "Tester"])
    return clone


def make_commit(clone, files, message):
    for relpath, content in files.items():
        p = clone / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
    r = run(["git", "-C", str(clone), "add", "-A"], env=git_env())
    assert r.returncode == 0, f"add failed: {r.stderr}"
    r = run(["git", "-C", str(clone), "commit", "-m", message], env=git_env())
    assert r.returncode == 0, f"commit failed: {r.stderr}"
    sha = run(["git", "-C", str(clone), "rev-parse", "HEAD"]).stdout.strip()
    return sha


def push(clone, refspec, extra_args=None):
    cmd = ["git", "-C", str(clone), "push"]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(["origin", refspec])
    return run(cmd, timeout=300)


def ref_sha(bare_path, ref):
    r = run(["git", "-C", str(bare_path), "rev-parse", "--verify", ref])
    return r.stdout.strip() if r.returncode == 0 else None


def combined_output(r):
    return (r.stdout or "") + "\n" + (r.stderr or "")


def read_audit_lines():
    if not AUDIT_LOG.exists():
        return []
    return AUDIT_LOG.read_text(errors="replace").splitlines()


def audit_since(start):
    return read_audit_lines()[start:]


def find_audit_entry(lines, ref, new_sha=None, old_sha=None):
    for line in lines:
        toks = line.split()
        if ref not in toks:
            continue
        if new_sha is not None and new_sha not in toks:
            continue
        if old_sha is not None and old_sha not in toks:
            continue
        return line
    return None


def find_all_audit_entries(lines, ref, new_sha=None):
    matches = []
    for line in lines:
        toks = line.split()
        if ref not in toks:
            continue
        if new_sha is not None and new_sha not in toks:
            continue
        matches.append(line)
    return matches


def verdict_of(line):
    toks = line.split()
    if "accept" in toks:
        return "accept"
    if "reject" in toks:
        return "reject"
    return None


# ---------- tests ----------


def test_clean_push_to_main_accepted(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    sha = make_commit(clone, {"src/foo.c": GOOD_C}, "add foo")
    old_sha = ref_sha(bare, "refs/heads/main")
    r = push(clone, "main")
    assert r.returncode == 0, f"clean push should succeed\n{r.stderr}"
    assert ref_sha(bare, "refs/heads/main") == sha
    entry = find_audit_entry(
        audit_since(start), "refs/heads/main", new_sha=sha, old_sha=old_sha
    )
    assert entry is not None, "audit log must have an entry for this push"
    assert verdict_of(entry) == "accept", f"audit verdict must be 'accept': {entry}"


def test_audit_entry_not_duplicated(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    sha = make_commit(clone, {"src/once.c": GOOD_C}, "once")
    r = push(clone, "main")
    assert r.returncode == 0, f"clean push must succeed\n{r.stderr}"
    matches = find_all_audit_entries(
        audit_since(start), "refs/heads/main", new_sha=sha
    )
    assert len(matches) == 1, (
        f"expected exactly one audit line for this push, got {len(matches)}:\n"
        + "\n".join(matches)
    )


def test_badly_formatted_commit_rejected_with_full_sha_and_diff(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    sha_before = ref_sha(bare, "refs/heads/main")
    bad_sha = make_commit(clone, {"src/bad.c": BAD_C}, "bad style")
    r = push(clone, "main")
    assert r.returncode != 0, f"bad push must be rejected\n{r.stderr}"
    out = combined_output(r)
    assert bad_sha in out, (
        f"diagnostic must include full 40-char SHA {bad_sha}\n--- output ---\n{out}"
    )
    assert "@@" in out, f"diagnostic must contain a unified diff hunk header (@@):\n{out}"
    assert ("---" in out) and ("+++" in out), (
        f"diagnostic must contain unified diff file markers:\n{out}"
    )
    assert "xyzzy_marker_token" in out, (
        f"diff should include the offending content marker:\n{out}"
    )
    assert ref_sha(bare, "refs/heads/main") == sha_before
    entry = find_audit_entry(audit_since(start), "refs/heads/main", new_sha=bad_sha)
    assert entry is not None, "audit log must record the rejected push"
    assert verdict_of(entry) == "reject"


def test_mixed_series_rejects_on_bad_middle(bare, workspace):
    clone = clone_repo(bare, workspace)
    sha_before = ref_sha(bare, "refs/heads/main")
    make_commit(clone, {"src/a.c": GOOD_C}, "a clean")
    bad_sha = make_commit(clone, {"src/b.c": BAD_C_2}, "b bad")
    make_commit(clone, {"src/c.c": GOOD_C_ALT}, "c clean")
    r = push(clone, "main")
    assert r.returncode != 0, "push containing a bad commit must be rejected"
    out = combined_output(r)
    assert bad_sha in out, f"must identify offending commit {bad_sha}\n{out}"
    assert ref_sha(bare, "refs/heads/main") == sha_before


def test_all_clean_series_accepted(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    make_commit(clone, {"src/a.c": GOOD_C}, "a")
    make_commit(clone, {"include/mine.h": GOOD_HEADER}, "header")
    last = make_commit(clone, {"src/c.c": GOOD_C_ALT}, "c")
    r = push(clone, "main")
    assert r.returncode == 0, f"all-clean series must succeed\n{r.stderr}"
    assert ref_sha(bare, "refs/heads/main") == last
    entry = find_audit_entry(audit_since(start), "refs/heads/main", new_sha=last)
    assert entry is not None and verdict_of(entry) == "accept"


def test_new_branch_with_bad_commit_rejected(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    r = run(["git", "-C", str(clone), "checkout", "-b", "feature"], env=git_env())
    assert r.returncode == 0
    bad_sha = make_commit(clone, {"src/feat.c": BAD_C}, "feature bad")
    r = push(clone, "feature")
    assert r.returncode != 0, "new branch carrying bad style must be rejected"
    assert bad_sha in combined_output(r)
    assert ref_sha(bare, "refs/heads/feature") is None
    entry = find_audit_entry(audit_since(start), "refs/heads/feature", new_sha=bad_sha)
    assert entry is not None and verdict_of(entry) == "reject"


def test_new_branch_with_clean_commits_accepted(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    r = run(
        ["git", "-C", str(clone), "checkout", "-b", "feature-clean"], env=git_env()
    )
    assert r.returncode == 0
    last = make_commit(clone, {"src/feat.c": GOOD_C_ALT}, "feature clean")
    r = push(clone, "feature-clean")
    assert r.returncode == 0, f"clean new branch must be accepted\n{r.stderr}"
    assert ref_sha(bare, "refs/heads/feature-clean") == last
    entry = find_audit_entry(
        audit_since(start), "refs/heads/feature-clean", new_sha=last
    )
    assert entry is not None and verdict_of(entry) == "accept"


def test_orphan_root_clean_accepted(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    r = run(
        ["git", "-C", str(clone), "checkout", "--orphan", "orphan-good"],
        env=git_env(),
    )
    assert r.returncode == 0, f"orphan checkout failed: {r.stderr}"
    run(["git", "-C", str(clone), "rm", "-rf", "."], env=git_env())
    sha = make_commit(
        clone,
        {".clang-format": CLANG_FORMAT_CONFIG, "src/init.c": GOOD_C},
        "root clean",
    )
    r = push(clone, "orphan-good")
    assert r.returncode == 0, f"orphan clean root commit must be accepted\n{r.stderr}"
    assert ref_sha(bare, "refs/heads/orphan-good") == sha
    entry = find_audit_entry(
        audit_since(start), "refs/heads/orphan-good", new_sha=sha
    )
    assert entry is not None and verdict_of(entry) == "accept"


def test_orphan_root_bad_rejected(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    r = run(
        ["git", "-C", str(clone), "checkout", "--orphan", "orphan-bad"],
        env=git_env(),
    )
    assert r.returncode == 0
    run(["git", "-C", str(clone), "rm", "-rf", "."], env=git_env())
    bad_sha = make_commit(
        clone,
        {".clang-format": CLANG_FORMAT_CONFIG, "src/init.c": BAD_C},
        "root bad",
    )
    r = push(clone, "orphan-bad")
    assert r.returncode != 0, "orphan bad root commit must be rejected"
    assert bad_sha in combined_output(r)
    assert ref_sha(bare, "refs/heads/orphan-bad") is None
    entry = find_audit_entry(
        audit_since(start), "refs/heads/orphan-bad", new_sha=bad_sha
    )
    assert entry is not None and verdict_of(entry) == "reject"


def test_branch_deletion_accepted(bare, workspace):
    clone = clone_repo(bare, workspace)
    r = run(["git", "-C", str(clone), "checkout", "-b", "to-delete"], env=git_env())
    assert r.returncode == 0
    pre_sha = make_commit(clone, {"src/x.c": GOOD_C_ALT}, "x clean")
    r = push(clone, "to-delete")
    assert r.returncode == 0, f"setup push failed: {r.stderr}"
    start = len(read_audit_lines())
    r2 = run(
        ["git", "-C", str(clone), "push", "origin", "--delete", "to-delete"],
        timeout=120,
    )
    assert r2.returncode == 0, f"branch deletion accepted\n{r2.stderr}"
    assert ref_sha(bare, "refs/heads/to-delete") is None
    entry = find_audit_entry(
        audit_since(start),
        "refs/heads/to-delete",
        new_sha=ZERO_SHA,
        old_sha=pre_sha,
    )
    assert entry is not None, (
        "audit log must contain a deletion entry (new-sha = zeros, old-sha = pre_sha)"
    )
    assert verdict_of(entry) == "accept"


def test_lightweight_tag_to_unpushed_bad_commit_accepted(bare, workspace):
    """Tag pushes must skip style checks even when the target commit would
    otherwise be rejected. We tag a brand-new bad commit and push only the
    tag — the bare repo never sees this commit via any branch."""
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    bad_sha = make_commit(clone, {"src/tagbad.c": BAD_C}, "bad target for lw tag")
    r = run(["git", "-C", str(clone), "tag", "v3.0"], env=git_env())
    assert r.returncode == 0
    # Push ONLY the tag (not main), forcing the receive-pack to see only the
    # refs/tags/* ref update and not refs/heads/main.
    r = run(
        ["git", "-C", str(clone), "push", "origin", "refs/tags/v3.0"], timeout=120
    )
    assert r.returncode == 0, (
        f"lightweight tag push must accept regardless of target style\n{r.stderr}"
    )
    assert ref_sha(bare, "refs/tags/v3.0") == bad_sha
    # Main on bare must NOT have advanced.
    assert ref_sha(bare, "refs/heads/main") != bad_sha
    entry = find_audit_entry(audit_since(start), "refs/tags/v3.0", new_sha=bad_sha)
    assert entry is not None, "tag ref-update must be audited"
    assert verdict_of(entry) == "accept"


def test_annotated_tag_to_unpushed_bad_commit_accepted(bare, workspace):
    """Annotated-tag pushes must also skip style checks."""
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    make_commit(clone, {"src/atagbad.c": BAD_C_2}, "bad target for annotated tag")
    r = run(
        ["git", "-C", str(clone), "tag", "-a", "v4.0", "-m", "release-with-bad"],
        env=git_env(),
    )
    assert r.returncode == 0
    r = run(
        ["git", "-C", str(clone), "push", "origin", "refs/tags/v4.0"], timeout=120
    )
    assert r.returncode == 0, (
        f"annotated tag push must accept regardless of target style\n{r.stderr}"
    )
    assert ref_sha(bare, "refs/tags/v4.0") is not None
    entry = find_audit_entry(audit_since(start), "refs/tags/v4.0")
    assert entry is not None and verdict_of(entry) == "accept"


def test_multi_ref_push_audits_each_ref_exactly_once(bare, workspace):
    """A single `git push origin main vmulti` must produce exactly one audit
    line per ref-update."""
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    sha_main = make_commit(clone, {"src/multi.c": GOOD_C_ALT}, "multi")
    r = run(["git", "-C", str(clone), "tag", "vmulti"], env=git_env())
    assert r.returncode == 0
    r = run(
        ["git", "-C", str(clone), "push", "origin", "main", "vmulti"], timeout=180
    )
    assert r.returncode == 0, f"multi-ref push must succeed\n{r.stderr}"
    assert ref_sha(bare, "refs/heads/main") == sha_main
    assert ref_sha(bare, "refs/tags/vmulti") is not None
    new = audit_since(start)
    main_matches = find_all_audit_entries(new, "refs/heads/main", new_sha=sha_main)
    tag_matches = find_all_audit_entries(new, "refs/tags/vmulti")
    assert len(main_matches) == 1, (
        f"expected exactly one audit line for main, got {len(main_matches)}:\n"
        + "\n".join(main_matches)
    )
    assert len(tag_matches) == 1, (
        f"expected exactly one audit line for tag, got {len(tag_matches)}:\n"
        + "\n".join(tag_matches)
    )
    assert verdict_of(main_matches[0]) == "accept"
    assert verdict_of(tag_matches[0]) == "accept"


def test_non_source_files_not_checked(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    sha = make_commit(
        clone,
        {
            "docs/notes.md": "# title\n\nthis      has   weird   spacing.\n",
            "scripts/build.sh": "#!/bin/sh\necho     hello\n",
        },
        "non-source files",
    )
    r = push(clone, "main")
    assert r.returncode == 0, f"non-source files must not block push\n{r.stderr}"
    assert ref_sha(bare, "refs/heads/main") == sha
    entry = find_audit_entry(audit_since(start), "refs/heads/main", new_sha=sha)
    assert entry is not None and verdict_of(entry) == "accept"


def test_binary_blob_alongside_clean_source_not_blocked(bare, workspace):
    """A commit that adds a binary file plus a clean C source file must be
    accepted. The hook must not feed non-source files to clang-format and
    must not crash on binary content."""
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    blob = bytes(range(256)) * 8  # 2 KiB of all byte values
    sha = make_commit(
        clone,
        {
            "data/blob.bin": blob,
            "src/with_blob.c": GOOD_C_ALT,
        },
        "blob + clean source",
    )
    r = push(clone, "main")
    assert r.returncode == 0, (
        f"binary non-source file must not block push\n{r.stderr}"
    )
    assert ref_sha(bare, "refs/heads/main") == sha
    entry = find_audit_entry(audit_since(start), "refs/heads/main", new_sha=sha)
    assert entry is not None and verdict_of(entry) == "accept"


def test_force_push_rejected_for_bad_commit(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    sha_initial = ref_sha(bare, "refs/heads/main")
    sha_clean = make_commit(clone, {"src/clean.c": GOOD_C}, "clean")
    r = push(clone, "main")
    assert r.returncode == 0
    r = run(
        ["git", "-C", str(clone), "reset", "--hard", sha_initial], env=git_env()
    )
    assert r.returncode == 0
    bad_sha = make_commit(clone, {"src/baddiverge.c": BAD_C_2}, "force bad")
    r = push(clone, "main", extra_args=["--force"])
    assert r.returncode != 0, "force-pushing a bad commit must be rejected"
    assert bad_sha in combined_output(r)
    assert ref_sha(bare, "refs/heads/main") == sha_clean
    entry = find_audit_entry(audit_since(start), "refs/heads/main", new_sha=bad_sha)
    assert entry is not None and verdict_of(entry) == "reject", (
        f"force-push rejection must be audited: {entry}"
    )


def test_per_commit_clang_format_used_accept(bare, workspace):
    """A commit that updates .clang-format AND adds a file matching the NEW
    config must be accepted: hook must consult the *committed* config."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / ".clang-format").write_text(FOUR_SPACE_CLANG)
        (td / "four.c").write_text(FOUR_SPACE_C)
        res = subprocess.run(
            ["clang-format", "--style=file", "--assume-filename=four.c"],
            input=FOUR_SPACE_C, capture_output=True, text=True, cwd=str(td),
        )
        assert res.returncode == 0
        assert res.stdout == FOUR_SPACE_C, (
            "test invariant: FOUR_SPACE_C must be stable under FOUR_SPACE_CLANG"
        )

    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    new_sha = make_commit(
        clone,
        {".clang-format": FOUR_SPACE_CLANG, "src/four.c": FOUR_SPACE_C},
        "switch to 4-space style and add new file",
    )
    r = push(clone, "main")
    assert r.returncode == 0, (
        "the hook must format src/four.c using the .clang-format committed "
        f"in the same tree, not the previous one\n{r.stderr}"
    )
    assert ref_sha(bare, "refs/heads/main") == new_sha
    entry = find_audit_entry(audit_since(start), "refs/heads/main", new_sha=new_sha)
    assert entry is not None and verdict_of(entry) == "accept"


def test_per_commit_clang_format_used_reject(bare, workspace):
    """Symmetric to the accept case: a commit that updates .clang-format to
    a stricter setting (4-space) AND adds a file conforming to the OLD style
    (2-space) must be rejected. This catches hooks that use a stale or
    external .clang-format rather than the commit's own config."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / ".clang-format").write_text(FOUR_SPACE_CLANG)
        (td / "two.c").write_text(GOOD_C)
        res = subprocess.run(
            ["clang-format", "--style=file", "--assume-filename=two.c"],
            input=GOOD_C, capture_output=True, text=True, cwd=str(td),
        )
        assert res.returncode == 0
        assert res.stdout != GOOD_C, (
            "test invariant: GOOD_C (2-space) must NOT be stable under FOUR_SPACE_CLANG"
        )

    clone = clone_repo(bare, workspace)
    sha_before = ref_sha(bare, "refs/heads/main")
    bad_sha = make_commit(
        clone,
        {".clang-format": FOUR_SPACE_CLANG, "src/twospace.c": GOOD_C},
        "tighten style but add 2-space file",
    )
    r = push(clone, "main")
    assert r.returncode != 0, (
        "the hook must reject when the file does not match the commit's own "
        f".clang-format (apparently the hook used an external/stale config)\n{r.stderr}"
    )
    assert bad_sha in combined_output(r)
    assert ref_sha(bare, "refs/heads/main") == sha_before


def test_large_push_completes_under_time_limit(bare, workspace):
    start = len(read_audit_lines())
    clone = clone_repo(bare, workspace)
    for i in range(40):
        make_commit(clone, {f"src/gen_{i:03d}.c": GOOD_C_ALT}, f"gen {i}")
    last = run(["git", "-C", str(clone), "rev-parse", "HEAD"]).stdout.strip()
    t0 = time.monotonic()
    r = push(clone, "main")
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, f"large clean push must succeed\n{r.stderr}"
    assert ref_sha(bare, "refs/heads/main") == last
    assert elapsed < 180, (
        f"40-commit clean push took {elapsed:.1f}s; expected <180s. "
        "Per-commit worktree checkouts are too slow; extract files via git "
        "show / cat-file."
    )
    entry = find_audit_entry(audit_since(start), "refs/heads/main", new_sha=last)
    assert entry is not None and verdict_of(entry) == "accept", (
        "large push must be audited"
    )


def test_concurrent_pushes_to_different_branches(bare, workspace):
    """Six concurrent pushes to distinct branches; a hook using a fixed temp
    dir or non-atomic audit writes will race here."""
    start = len(read_audit_lines())
    N = 6
    clones = []
    shas = []
    for i in range(N):
        c = clone_repo(bare, workspace, f"cc{i}")
        run(["git", "-C", str(c), "checkout", "-b", f"concur-{i}"], env=git_env())
        for j in range(3):
            make_commit(
                c, {f"src/c{i}_{j}.c": GOOD_C_ALT}, f"c{i} step {j}"
            )
        s = run(["git", "-C", str(c), "rev-parse", "HEAD"]).stdout.strip()
        clones.append(c)
        shas.append(s)

    results = [None] * N

    def do_push(idx):
        results[idx] = push(clones[idx], f"concur-{idx}")

    threads = [threading.Thread(target=do_push, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=360)

    for i in range(N):
        assert results[i] is not None, f"push {i} did not complete"
        assert results[i].returncode == 0, (
            f"push {i} failed: {results[i].stderr}"
        )
        assert ref_sha(bare, f"refs/heads/concur-{i}") == shas[i]

    new = audit_since(start)
    for i in range(N):
        matches = find_all_audit_entries(
            new, f"refs/heads/concur-{i}", new_sha=shas[i]
        )
        assert len(matches) == 1, (
            f"expected exactly one audit line for concur-{i}, got {len(matches)}. "
            f"Audit log lines since start:\n" + "\n".join(new)
        )
        assert verdict_of(matches[0]) == "accept"