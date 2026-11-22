from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from meltzer.approval.orchestrator import PatchDecision, PatchRequest
from meltzer.sandbox.policy import SandboxMode, SandboxPolicy
from meltzer.tools.apply_patch import (
    AddFile,
    ApplyPatchTool,
    DeleteFile,
    FileChange,
    PatchError,
    UpdateFile,
    write_all,
    apply_chunks,
    parse_patch,
)
from meltzer.tools.registry import ToolError

from conftest import needs_posix



def patch(*body: str) -> str:
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"]) + "\n"


def workspace_policy(root: Path, deny_read: tuple[Path, ...] = ()) -> SandboxPolicy:
    return SandboxPolicy.for_session(SandboxMode.WORKSPACE_WRITE, root, deny_read=deny_read)


def planner(root: Path, deny_read: tuple[Path, ...] = ()) -> ApplyPatchTool:
    # plan() only reads files and checks the sandbox policy; the approval orchestrator decides when the patch is applied.
    orchestrator = SimpleNamespace(sandbox=workspace_policy(root, deny_read))
    return ApplyPatchTool(root.resolve(), orchestrator)


def test_parse_add_delete_update():
    hunks = parse_patch(
        patch(
            "*** Add File: a.txt",
            "+one",
            "*** Delete File: b.txt",
            "*** Update File: c.txt",
            "@@ class C:",
            " keep",
            "-old",
            "+new",
        )
    )
    assert hunks[0] == AddFile("a.txt", "one\n")
    assert hunks[1] == DeleteFile("b.txt")
    assert isinstance(hunks[2], UpdateFile)
    assert hunks[2].chunks[0].context == "class C:"
    assert hunks[2].chunks[0].old_lines == ["keep", "old"]
    assert hunks[2].chunks[0].new_lines == ["keep", "new"]


def test_parse_first_chunk_without_header_and_move():
    (hunk,) = parse_patch(patch("*** Update File: x.py", "*** Move to: y.py", "-a", "+b"))
    assert hunk.move_to == "y.py"
    assert hunk.chunks[0].context is None
    assert hunk.chunks[0].old_lines == ["a"]
    (hunk,) = parse_patch(patch("*** Update File: x.py", "*** Move to: y.py"))
    assert hunk.chunks == [] and hunk.move_to == "y.py"
    hunks = parse_patch(patch("*** Delete File: a", "", "*** Delete File: b"))
    assert [h.path for h in hunks] == ["a", "b"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "*** Add File: x\n+a\n*** End Patch",
        "*** Begin Patch\n*** Add File: x\n+a",
        "*** Begin Patch\n*** Rename File: x\n*** End Patch",
        "*** Begin Patch\n*** End Patch",
        "*** Begin Patch\n*** Update File: x\n*** End Patch",
        "*** Begin Patch\n*** Add File: \n+a\n*** End Patch",
        "*** Begin Patch\n*** Update File: x\n*** End of File\n*** End Patch",
        "*** Begin Patch\n*** Update File: x\n?what\n*** End Patch",
    ],
)
def test_parse_rejects_malformed(text):
    with pytest.raises(PatchError):
        parse_patch(text)


def chunks(*body: str):
    return parse_patch(patch("*** Update File: f", *body))[0].chunks


def test_missing_path_error_names_the_header_line():
    with pytest.raises(PatchError, match="missing file path at line 3"):
        parse_patch("*** Begin Patch\n*** Add File: a.txt\n*** Add File: \n+x\n*** End Patch\n")


@pytest.mark.parametrize(
    "original, body, expected",
    [
        (
            "def a():\n    return 1\n\ndef b():\n    return 1\n",
            ["@@ def b():", "-    return 1", "+    return 2"],
            "def a():\n    return 1\n\ndef b():\n    return 2\n",
        ),
        ("1\n2\n3\n4\n5\n", ["@@", "-2", "+two", "@@", "-4", "+four"], "1\ntwo\n3\nfour\n5\n"),
        ("a  \nb\n", [" a", "-b", "+c"], "a\nc\n"),
        ("x\nend\nx\nend\n", [" x", "-end", "+END", "*** End of File"], "x\nend\nx\nEND\n"),
        ("import os\n\ndef main():\n    pass\n", ["@@ import os", "+import sys"], "import os\nimport sys\n\ndef main():\n    pass\n"),
        ("a\nb\nc\n", ["@@ a", "+inserted", "@@", "-b", "+B"], "a\ninserted\nB\nc\n"),
        ("a\n", ["+b"], "a\nb\n"),
        ("a\nb", [" a", "-b", "+c"], "a\nc\n"),
    ],
    ids=["after-context", "several-chunks", "trailing-whitespace", "end-of-file", "insert-after-context",
         "insert-before-change", "insert-appends", "trailing-newline"],
)
def test_apply_chunks(original, body, expected):
    assert apply_chunks(original, chunks(*body), "f") == expected


@pytest.mark.parametrize(
    "body, message",
    [(["@@ nowhere", "-a", "+b"], "failed to find context"), (["-zzz", "+b"], "failed to find expected lines")],
)
def test_apply_fails_when_the_text_is_not_found(body, message):
    with pytest.raises(PatchError, match=message):
        apply_chunks("a\n", chunks(*body), "f")


def test_plan_move_to_same_path_is_a_modification(tmp_path):
    (tmp_path / "same.txt").write_text("a\n")
    changes = planner(tmp_path).plan(parse_patch(patch("*** Update File: same.txt", "*** Move to: same.txt", "-a", "+b")))
    assert [(c.path.name, c.contents, c.label) for c in changes] == [("same.txt", "b\n", "M")]
    tmp = tmp_path / "later"
    tmp.mkdir()
    assert planner(tmp).plan(parse_patch(patch("*** Add File: tmp.txt", "+x", "*** Delete File: tmp.txt"))) == []


def test_plan_move_to_other_path(tmp_path):
    (tmp_path / "old.txt").write_text("a\n")
    changes = planner(tmp_path).plan(parse_patch(patch("*** Update File: old.txt", "*** Move to: sub/new.txt")))
    assert [(c.path.relative_to(tmp_path.resolve()).as_posix(), c.label) for c in changes] == [
        ("sub/new.txt", "A"),
        ("old.txt", "D"),
    ]


def test_plan_move_onto_existing_file_is_a_modification(tmp_path):
    (tmp_path / "old.txt").write_text("a\n")
    (tmp_path / "taken.txt").write_text("b\n")
    changes = planner(tmp_path).plan(parse_patch(patch("*** Update File: old.txt", "*** Move to: taken.txt")))
    assert [(c.path.name, c.label) for c in changes] == [("taken.txt", "M"), ("old.txt", "D")]


def test_plan_later_hunk_sees_earlier_hunk(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\n")
    changes = planner(tmp_path).plan(
        parse_patch(patch("*** Update File: f.txt", "-a", "+A", "*** Update File: f.txt", "-b", "+B"))
    )
    assert len(changes) == 1 and changes[0].contents == "A\nB\n"


def test_plan_errors(tmp_path):
    tool = planner(tmp_path)
    with pytest.raises(PatchError, match="no such file"):
        tool.plan(parse_patch(patch("*** Delete File: missing.txt")))
    with pytest.raises(PatchError, match="no such file"):
        tool.plan(parse_patch(patch("*** Update File: missing.txt", "-a", "+b")))
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9\n")
    with pytest.raises(PatchError, match="not a UTF-8 text file"):
        tool.plan(parse_patch(patch("*** Update File: latin1.txt", "-x", "+y")))


@pytest.mark.parametrize(
    "body",
    [
        ["*** Update File: secrets/key.txt", "-a", "+b"],
        ["*** Update File: secrets/key.txt", "*** Move to: moved.txt"],
        ["*** Update File: ok.txt", "*** Move to: secrets/moved.txt"],
        ["*** Delete File: secrets/key.txt"],
        ["*** Add File: secrets/new.txt", "+x"],
    ],
)
def test_plan_rejects_paths_the_policy_cannot_read(tmp_path, body):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "key.txt").write_text("a\n")
    (tmp_path / "ok.txt").write_text("a\n")
    tool = planner(tmp_path, deny_read=(tmp_path / "secrets",))
    text = patch("*** Update File: ok.txt", "-a", "+b", *body)
    with pytest.raises(PatchError, match="cannot read secrets/"):
        tool.plan(parse_patch(text))


@needs_posix
def test_write_all_restores_files_after_a_failure(tmp_path):
    (tmp_path / "keep.txt").write_text("old\n")
    (tmp_path / "gone.txt").write_text("still here\n")
    (tmp_path / "gone.txt").chmod(0o750)
    (tmp_path / "blocker").write_text("a file, not a directory\n")
    with pytest.raises(OSError):
        write_all(
            [
                FileChange(tmp_path / "keep.txt", "new\n", "M"),
                FileChange(tmp_path / "gone.txt", None, "D"),
                FileChange(tmp_path / "fresh" / "a.txt", "a\n", "A"),
                FileChange(tmp_path / "blocker" / "b.txt", "b\n", "A"),
            ]
        )
    assert (tmp_path / "keep.txt").read_text() == "old\n"
    assert (tmp_path / "gone.txt").read_text() == "still here\n"
    assert (tmp_path / "gone.txt").stat().st_mode & 0o777 == 0o750
    assert not (tmp_path / "fresh").exists()


class SwappingOrchestrator:
    """Approves the patch, then runs `swap`: the files change between planning and writing."""

    def __init__(self, swap):
        self.swap = swap
        self.sandbox = SandboxPolicy.for_session(SandboxMode.FULL_ACCESS, Path.cwd())
        self.requests: list[PatchRequest] = []

    def decide_patch(self, request: PatchRequest) -> PatchDecision:
        self.requests.append(request)
        self.swap()
        return PatchDecision(apply=True, reason="all paths are writable")


@needs_posix
def test_directory_swapped_for_a_symlink_is_not_followed(tmp_path):
    root = (tmp_path / "work").resolve()
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap() -> None:
        (root / "sub").rename(root / "sub-before")
        (root / "sub").symlink_to(outside, target_is_directory=True)

    text = patch("*** Add File: sub/new.txt", "+x")
    tool = ApplyPatchTool(root, SwappingOrchestrator(swap))
    with pytest.raises(ToolError, match="symbolic link"):
        tool.run({"patch": text})
    assert not (outside / "new.txt").exists()
    assert tool.orchestrator.requests[0].patch == text


@needs_posix
def test_file_swapped_for_a_symlink_is_not_followed(tmp_path):
    root = (tmp_path / "work").resolve()
    root.mkdir()
    (root / "a.txt").write_text("a\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")

    def swap() -> None:
        (root / "a.txt").unlink()
        (root / "a.txt").symlink_to(outside)

    tool = ApplyPatchTool(root, SwappingOrchestrator(swap))
    with pytest.raises(ToolError, match="patch not applied"):
        tool.run({"patch": patch("*** Update File: a.txt", "-a", "+b")})
    assert outside.read_text() == "keep\n"


@needs_posix
def test_run_writes_and_reports_labels(tmp_path):
    root = tmp_path.resolve()
    (root / "old.txt").write_text("a\n")
    (root / "taken.txt").write_text("b\n")
    os.chmod(root / "taken.txt", 0o640)
    tool = ApplyPatchTool(root, SwappingOrchestrator(lambda: None))
    out = tool.run({"patch": patch("*** Update File: old.txt", "*** Move to: taken.txt", "*** Add File: n/new.txt", "+n")})
    assert out.splitlines()[1:] == [f"M {root / 'taken.txt'}", f"D {root / 'old.txt'}", f"A {root / 'n' / 'new.txt'}"]
    assert (root / "taken.txt").read_text() == "a\n"
    assert (root / "taken.txt").stat().st_mode & 0o777 == 0o640
    assert (root / "n" / "new.txt").read_text() == "n\n"
    assert not (root / "old.txt").exists()
