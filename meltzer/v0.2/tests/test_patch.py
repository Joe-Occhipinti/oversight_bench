from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meltzer.approval.orchestrator import PatchDecision, PatchRequest
from meltzer.tools.apply_patch import (
    AddFile,
    ApplyPatchTool,
    DeleteFile,
    FileChange,
    PatchError,
    UpdateFile,
    _write_all,
    apply_chunks,
    parse_patch,
)
from meltzer.tools.registry import ToolError

# Files are written through directory file descriptors (dir_fd), which need POSIX.
needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX file APIs")


def patch(*body: str) -> str:
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"]) + "\n"


def planner(root: Path) -> ApplyPatchTool:
    # plan() only reads files; the approval orchestrator is used when the patch is applied.
    return ApplyPatchTool(root.resolve(), orchestrator=None)


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


def test_parse_move_only():
    (hunk,) = parse_patch(patch("*** Update File: x.py", "*** Move to: y.py"))
    assert hunk.chunks == [] and hunk.move_to == "y.py"


def test_parse_tolerates_blank_lines_between_hunks():
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


def test_apply_replaces_lines_after_context():
    original = "def a():\n    return 1\n\ndef b():\n    return 1\n"
    updated = apply_chunks(original, chunks("@@ def b():", "-    return 1", "+    return 2"), "f")
    assert updated == "def a():\n    return 1\n\ndef b():\n    return 2\n"


def test_apply_several_chunks_in_order():
    original = "1\n2\n3\n4\n5\n"
    updated = apply_chunks(original, chunks("@@", "-2", "+two", "@@", "-4", "+four"), "f")
    assert updated == "1\ntwo\n3\nfour\n5\n"


def test_apply_ignores_trailing_whitespace_differences():
    assert apply_chunks("a  \nb\n", chunks(" a", "-b", "+c"), "f") == "a\nc\n"


def test_apply_end_of_file_matches_last_occurrence():
    original = "x\nend\nx\nend\n"
    assert apply_chunks(original, chunks(" x", "-end", "+END", "*** End of File"), "f") == "x\nend\nx\nEND\n"


def test_apply_insertion_goes_after_the_context_line():
    original = "import os\n\ndef main():\n    pass\n"
    updated = apply_chunks(original, chunks("@@ import os", "+import sys"), "f")
    assert updated == "import os\nimport sys\n\ndef main():\n    pass\n"


def test_apply_insertion_before_a_later_change():
    original = "a\nb\nc\n"
    updated = apply_chunks(original, chunks("@@ a", "+inserted", "@@", "-b", "+B"), "f")
    assert updated == "a\ninserted\nB\nc\n"


def test_apply_insertion_without_context_appends():
    assert apply_chunks("a\n", chunks("+b"), "f") == "a\nb\n"


def test_missing_path_error_names_the_header_line():
    with pytest.raises(PatchError, match="missing file path at line 3"):
        parse_patch("*** Begin Patch\n*** Add File: a.txt\n*** Add File: \n+x\n*** End Patch\n")


def test_apply_adds_trailing_newline():
    assert apply_chunks("a\nb", chunks(" a", "-b", "+c"), "f") == "a\nc\n"


def test_apply_missing_context_fails():
    with pytest.raises(PatchError, match="failed to find context"):
        apply_chunks("a\n", chunks("@@ nowhere", "-a", "+b"), "f")


def test_apply_missing_lines_fails():
    with pytest.raises(PatchError, match="failed to find expected lines"):
        apply_chunks("a\n", chunks("-zzz", "+b"), "f")


def test_plan_move_to_same_path_is_a_modification(tmp_path):
    (tmp_path / "same.txt").write_text("a\n")
    changes = planner(tmp_path).plan(parse_patch(patch("*** Update File: same.txt", "*** Move to: same.txt", "-a", "+b")))
    assert [(c.path.name, c.contents, c.label) for c in changes] == [("same.txt", "b\n", "M")]


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


def test_plan_add_then_delete_leaves_nothing_to_do(tmp_path):
    changes = planner(tmp_path).plan(parse_patch(patch("*** Add File: tmp.txt", "+x", "*** Delete File: tmp.txt")))
    assert changes == []


def test_plan_errors(tmp_path):
    tool = planner(tmp_path)
    with pytest.raises(PatchError, match="no such file"):
        tool.plan(parse_patch(patch("*** Delete File: missing.txt")))
    with pytest.raises(PatchError, match="no such file"):
        tool.plan(parse_patch(patch("*** Update File: missing.txt", "-a", "+b")))
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9\n")
    with pytest.raises(PatchError, match="not a UTF-8 text file"):
        tool.plan(parse_patch(patch("*** Update File: latin1.txt", "-x", "+y")))


@needs_posix
def test_write_all_applies_every_change(tmp_path):
    (tmp_path / "gone.txt").write_text("x\n")
    _write_all(
        [
            FileChange(tmp_path / "deep" / "new.txt", "new\n", "A"),
            FileChange(tmp_path / "gone.txt", None, "D"),
        ]
    )
    assert (tmp_path / "deep" / "new.txt").read_text() == "new\n"
    assert not (tmp_path / "gone.txt").exists()


@needs_posix
def test_write_all_restores_files_after_a_failure(tmp_path):
    (tmp_path / "keep.txt").write_text("old\n")
    (tmp_path / "gone.txt").write_text("still here\n")
    (tmp_path / "gone.txt").chmod(0o750)
    (tmp_path / "blocker").write_text("a file, not a directory\n")
    with pytest.raises(OSError):
        _write_all(
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
