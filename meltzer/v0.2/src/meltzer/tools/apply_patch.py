"""The `apply_patch` tool: file edits in a small, line-based patch format.

    *** Begin Patch
    *** Add File: path/to/new.py
    +line one
    *** Update File: path/to/existing.py
    *** Move to: path/to/renamed.py
    @@ def handler():
     context line
    -removed line
    +added line
    *** Delete File: path/to/old.py
    *** End Patch

Paths are relative to the workspace root, or absolute. The patch is applied
by meltzer itself, not by a sandboxed process, so every target path is
checked against the sandbox policy before anything is written, and written
without following symbolic links. A patch is applied as a whole: if one file
cannot be written, the others are restored.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meltzer.approval.orchestrator import ApprovalOrchestrator, PatchRequest
from meltzer.sandbox.policy import resolve
from meltzer.tools.registry import ToolError, ToolSpec, require_string

BEGIN_PATCH = "*** Begin Patch"
END_PATCH = "*** End Patch"
ADD_FILE = "*** Add File: "
DELETE_FILE = "*** Delete File: "
UPDATE_FILE = "*** Update File: "
MOVE_TO = "*** Move to: "
END_OF_FILE = "*** End of File"
CHUNK_HEADER = "@@"


class PatchError(ValueError):
    """The patch is malformed or does not match the files on disk."""


@dataclass
class AddFile:
    path: str
    contents: str


@dataclass
class DeleteFile:
    path: str


@dataclass
class Chunk:
    context: str | None = None
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    end_of_file: bool = False


@dataclass
class UpdateFile:
    path: str
    move_to: str | None = None
    chunks: list[Chunk] = field(default_factory=list)


Hunk = AddFile | DeleteFile | UpdateFile


def _is_hunk_header(line: str) -> bool:
    return line.startswith((ADD_FILE, DELETE_FILE, UPDATE_FILE)) or line.strip() == END_PATCH


def parse_patch(text: str) -> list[Hunk]:
    lines = text.strip().splitlines()
    if not lines or lines[0].strip() != BEGIN_PATCH:
        raise PatchError(f"the first line of the patch must be '{BEGIN_PATCH}'")
    if lines[-1].strip() != END_PATCH:
        raise PatchError(f"the last line of the patch must be '{END_PATCH}'")

    hunks: list[Hunk] = []
    i = 1
    end = len(lines) - 1
    while i < end:
        line = lines[i]
        header_line = i + 1
        if line.startswith(ADD_FILE):
            path = line[len(ADD_FILE):].strip()
            i += 1
            body = []
            while i < end and lines[i].startswith("+"):
                body.append(lines[i][1:])
                i += 1
            hunks.append(AddFile(path, "".join(f"{b}\n" for b in body)))
        elif line.startswith(DELETE_FILE):
            hunks.append(DeleteFile(line[len(DELETE_FILE):].strip()))
            i += 1
        elif line.startswith(UPDATE_FILE):
            update = UpdateFile(line[len(UPDATE_FILE):].strip())
            i += 1
            if i < end and lines[i].startswith(MOVE_TO):
                update.move_to = lines[i][len(MOVE_TO):].strip()
                i += 1
            i = _parse_chunks(lines, i, end, update)
            if not update.chunks and update.move_to is None:
                raise PatchError(f"update hunk for '{update.path}' is empty")
            hunks.append(update)
        elif not line.strip():
            i += 1
        else:
            raise PatchError(f"invalid hunk header at line {header_line}: {line!r}")
        if hunks and not hunks[-1].path:
            raise PatchError(f"missing file path at line {header_line}")
    if not hunks:
        raise PatchError("patch contains no changes")
    return hunks


def _parse_chunks(lines: list[str], i: int, end: int, update: UpdateFile) -> int:
    chunk: Chunk | None = None
    while i < end and not _is_hunk_header(lines[i]):
        line = lines[i]
        if line.startswith(CHUNK_HEADER):
            chunk = Chunk(context=line[len(CHUNK_HEADER):].strip() or None)
            update.chunks.append(chunk)
        elif line.strip() == END_OF_FILE:
            if chunk is None:
                raise PatchError(f"'{END_OF_FILE}' before any change at line {i + 1}")
            chunk.end_of_file = True
        else:
            if chunk is None:
                # The first chunk may omit its @@ header.
                chunk = Chunk()
                update.chunks.append(chunk)
            marker, content = (line[:1], line[1:]) if line else (" ", "")
            if marker == " ":
                chunk.old_lines.append(content)
                chunk.new_lines.append(content)
            elif marker == "-":
                chunk.old_lines.append(content)
            elif marker == "+":
                chunk.new_lines.append(content)
            else:
                raise PatchError(f"unexpected line in update hunk at line {i + 1}: {line!r}")
        i += 1
    return i


def _seek(lines: list[str], pattern: list[str], start: int, end_of_file: bool) -> int | None:
    """Find pattern in lines at or after start; tolerate whitespace differences on later passes."""
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    last = len(lines) - len(pattern)
    candidates = range(last, last + 1) if end_of_file else range(start, last + 1)
    for normalize in (lambda s: s, str.rstrip, str.strip):
        for index in candidates:
            if all(normalize(lines[index + k]) == normalize(pattern[k]) for k in range(len(pattern))):
                return index
    return None


def apply_chunks(original: str, chunks: list[Chunk], path: str) -> str:
    lines = original.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    replacements: list[tuple[int, int, list[str]]] = []
    position = 0
    for chunk in chunks:
        if chunk.context is not None:
            found = _seek(lines, [chunk.context], position, False)
            if found is None:
                raise PatchError(f"failed to find context '{chunk.context}' in {path}")
            position = found + 1
        if not chunk.old_lines:
            # Only additions: insert after the context line, or at the end of the file.
            insert_at = position if chunk.context is not None else len(lines)
            replacements.append((insert_at, 0, chunk.new_lines))
            continue
        pattern = chunk.old_lines
        new_lines = chunk.new_lines
        found = _seek(lines, pattern, position, chunk.end_of_file)
        if found is None and pattern[-1] == "":
            pattern = pattern[:-1]
            new_lines = new_lines[:-1] if new_lines and new_lines[-1] == "" else new_lines
            found = _seek(lines, pattern, position, chunk.end_of_file)
        if found is None:
            expected = "\n".join(chunk.old_lines)
            raise PatchError(f"failed to find expected lines in {path}:\n{expected}")
        replacements.append((found, len(pattern), new_lines))
        position = found + len(pattern)
    # Apply from the end of the file backwards, so earlier indexes stay valid. Of
    # two changes at the same index, the later one in the patch goes first, so
    # the earlier one ends up above it.
    ordered = sorted(enumerate(replacements), key=lambda pair: (pair[1][0], pair[0]), reverse=True)
    for _, (start, length, new) in ordered:
        lines[start:start + length] = new
    return "".join(f"{line}\n" for line in lines)


@dataclass
class FileChange:
    path: Path
    contents: str | None  # None deletes the file
    label: str  # A, M or D


def _read_for_update(path: Path, shown: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"cannot update {shown}: not a UTF-8 text file") from exc
    except OSError as exc:
        raise PatchError(f"cannot read {shown}: {exc.strerror}") from exc


@dataclass
class _Backup:
    """State of a path before the patch, used to undo a partly applied patch."""

    path: Path
    contents: bytes | None  # None: the file did not exist
    mode: int = 0  # permission bits of the file, when it existed


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _file_flags(extra: int) -> int:
    # O_NONBLOCK: opening a FIFO must not wait for the other end.
    return extra | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _path_error(exc: OSError, path: Path) -> OSError:
    """The error with the full path; opening a symbolic link with O_NOFOLLOW gives ELOOP or ENOTDIR."""
    if exc.errno in (errno.ELOOP, errno.ENOTDIR) and os.path.islink(path):
        return OSError(exc.errno, "path contains a symbolic link", str(path))
    return OSError(exc.errno, exc.strerror, str(path))


def _open_directory(path: Path, created: list[Path] | None = None) -> int:
    """Open a directory one component at a time from /, never following a symbolic link.

    With `created`, missing directories are made and appended to it.
    """
    fd = os.open("/", _directory_flags())
    current = Path("/")
    try:
        for name in path.parts[1:]:
            current = current / name
            try:
                child = os.open(name, _directory_flags(), dir_fd=fd)
            except FileNotFoundError:
                if created is None:
                    raise
                os.mkdir(name, dir_fd=fd)
                created.append(current)
                child = os.open(name, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError as exc:
        os.close(fd)
        raise _path_error(exc, current) from exc
    return fd


def _open_regular(name: str, flags: int, dir_fd: int, mode: int = 0o666) -> int:
    fd = os.open(name, _file_flags(flags), mode, dir_fd=dir_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file", name)
    return fd


def _read_fd(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _write_fd(fd: int, data: bytes) -> None:
    """Replace the contents of the file open at fd with data."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _apply_change(change: FileChange, backups: list[_Backup], created_dirs: list[Path]) -> None:
    name = change.path.name
    parent = _open_directory(change.path.parent, None if change.contents is None else created_dirs)
    try:
        try:
            fd = _open_regular(name, os.O_RDONLY if change.contents is None else os.O_RDWR, parent)
            existed = True
        except FileNotFoundError:
            if change.contents is None:
                raise
            fd = _open_regular(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, parent)
            existed = False
        try:
            if existed:
                backups.append(_Backup(change.path, _read_fd(fd), stat.S_IMODE(os.fstat(fd).st_mode)))
            else:
                backups.append(_Backup(change.path, None))
            if change.contents is None:
                os.unlink(name, dir_fd=parent)
            else:
                _write_fd(fd, change.contents.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        raise _path_error(exc, change.path) from exc
    finally:
        os.close(parent)


def _restore(backup: _Backup) -> None:
    parent = _open_directory(backup.path.parent)
    try:
        if backup.contents is None:
            os.unlink(backup.path.name, dir_fd=parent)
            return
        fd = _open_regular(backup.path.name, os.O_WRONLY | os.O_CREAT, parent, backup.mode)
        try:
            _write_fd(fd, backup.contents)
            os.fchmod(fd, backup.mode)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _remove_directory(directory: Path) -> None:
    parent = _open_directory(directory.parent)
    try:
        os.rmdir(directory.name, dir_fd=parent)
    finally:
        os.close(parent)


def _write_all(changes: list[FileChange]) -> None:
    """Apply every change, or none: on a write error, earlier changes are undone.

    Paths are opened one component at a time without following symbolic
    links, so the files written are the ones that were checked, even if a
    directory on the way has been replaced since.
    """
    backups: list[_Backup] = []
    created_dirs: list[Path] = []
    try:
        for change in changes:
            _apply_change(change, backups, created_dirs)
    except OSError:
        for backup in reversed(backups):
            try:
                _restore(backup)
            except OSError:
                continue
        for directory in reversed(created_dirs):
            try:
                _remove_directory(directory)
            except OSError:
                continue
        raise


class ApplyPatchTool:
    spec = ToolSpec(
        name="apply_patch",
        description=(
            "Edit files with a patch. Format:\n"
            "*** Begin Patch\n"
            "*** Add File: <path>       (followed by lines starting with +)\n"
            "*** Delete File: <path>\n"
            "*** Update File: <path>    (optionally followed by *** Move to: <path>)\n"
            "@@ <optional line to locate the change>\n"
            " context line\n-removed line\n+added line\n"
            "*** End Patch\n"
            "Paths are relative to the workspace root. Give about three lines of context around each change."
        ),
        parameters={
            "type": "object",
            "properties": {"patch": {"type": "string", "description": "The full patch text."}},
            "required": ["patch"],
            "additionalProperties": False,
        },
    )

    def __init__(self, root: Path, orchestrator: ApprovalOrchestrator):
        self.root = root
        self.orchestrator = orchestrator

    def _path(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        return resolve(path)

    def plan(self, hunks: list[Hunk]) -> list[FileChange]:
        """Compute the final contents of every file without writing anything.

        Hunks apply in order, so a later hunk sees the result of an earlier one
        on the same file.
        """
        staged: dict[Path, FileChange] = {}

        def exists(path: Path) -> bool:
            if path in staged:
                return staged[path].contents is not None
            return path.is_file()

        def current(path: Path, shown: str) -> str:
            if path in staged and staged[path].contents is not None:
                return staged[path].contents
            if path in staged or not path.is_file():
                raise PatchError(f"cannot update {shown}: no such file")
            return _read_for_update(path, shown)

        def delete(path: Path) -> None:
            if path.is_file():
                staged[path] = FileChange(path, None, "D")
            else:
                # Added earlier in this patch: nothing to delete on disk.
                staged.pop(path, None)

        def written(path: Path, contents: str) -> FileChange:
            # A file that exists before the patch is reported as modified.
            return FileChange(path, contents, "M" if path.is_file() or exists(path) else "A")

        for hunk in hunks:
            path = self._path(hunk.path)
            if isinstance(hunk, AddFile):
                staged[path] = written(path, hunk.contents)
            elif isinstance(hunk, DeleteFile):
                if not exists(path):
                    raise PatchError(f"cannot delete {hunk.path}: no such file")
                delete(path)
            else:
                updated = apply_chunks(current(path, hunk.path), hunk.chunks, hunk.path)
                destination = self._path(hunk.move_to) if hunk.move_to else path
                if destination == path:
                    staged[path] = FileChange(path, updated, "M")
                else:
                    staged[destination] = written(destination, updated)
                    delete(path)
        return list(staged.values())

    def run(self, arguments: dict[str, Any]) -> str:
        text = require_string(arguments, "patch")
        try:
            changes = self.plan(parse_patch(text))
        except PatchError as exc:
            raise ToolError(f"patch rejected: {exc}") from exc

        targets = tuple(change.path for change in changes)
        decision = self.orchestrator.decide_patch(PatchRequest(paths=targets, patch=text))
        if not decision.apply:
            raise ToolError(f"patch not applied: {decision.reason}")

        try:
            _write_all(changes)
        except OSError as exc:
            target = exc.filename or "a file"
            raise ToolError(f"patch not applied: failed to write {target}: {exc.strerror}") from exc
        summary = [f"{change.label} {change.path}" for change in changes]
        return "Success. Updated the following files:\n" + "\n".join(summary)
