"""The `image_gen` tool: generate or edit images through the model provider's Images API.

Without referenced images the tool sends a generation request; with them, an
edit request that includes each image. Referenced images are read by meltzer;
they must be PNG, JPEG, WebP or GIF files in the workspace or the session's
temp directory, at most 50 MiB in total.
The result is saved by meltzer as
`~/.meltzer/generated_images/<session id>/<n>.png`, numbered from 1, and the
tool returns its path.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from meltzer.client import ModelError
from meltzer.sandbox.policy import SANDBOX_TEMP_DIR, is_within, resolve
from meltzer.tools.registry import Tool, ToolError, ToolSpec, require_string

if TYPE_CHECKING:
    from meltzer.approval.orchestrator import ApprovalOrchestrator
    from meltzer.config.schema import Config

MAX_REFERENCED_IMAGES = 5
# Largest total size of the referenced images sent to the provider.
MAX_IMAGE_BYTES = 50 * 1024 * 1024
GENERATED_IMAGES_DIR = "generated_images"

IMAGE_GEN_SPEC = ToolSpec(
    name="image_gen",
    description=(
        "Generate an image from a description, or edit existing images. For an edit, list the "
        "image files in referenced_image_paths. The image is saved under ~/.meltzer/generated_images "
        "and the tool returns its path; to use it elsewhere, copy it and leave the original in place."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Description of the image, or of the changes to make."},
            "referenced_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"Up to {MAX_REFERENCED_IMAGES} images to edit or combine (PNG, JPEG, WebP or GIF), "
                    "absolute or relative to the workspace root. Omit to generate a new image."
                ),
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
)


class ImageClient(Protocol):
    def images_generate(self, prompt: str, *, model: str) -> bytes: ...

    def images_edit(self, prompt: str, images: list[tuple[str, bytes, str]], *, model: str) -> bytes: ...


def image_type(data: bytes) -> str | None:
    """The media type of an image file from its first bytes, or None for other files."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_output_dir(home: Path, session_id: str) -> Path:
    """Where the images of a session are saved: <home>/generated_images/<session id>."""
    return home / GENERATED_IMAGES_DIR / re.sub(r"[^A-Za-z0-9_-]", "_", session_id)


def save_image(directory: Path, data: bytes) -> Path:
    """Write data to the first free <n>.png in directory; existing files are never replaced."""
    directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    number = 1
    while True:
        path = directory / f"{number}.png"
        try:
            fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            number += 1
            continue
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        return path


class ImageGenTool(Tool):
    spec = IMAGE_GEN_SPEC

    def __init__(
        self, root: Path, orchestrator: ApprovalOrchestrator, client: ImageClient, model: str, output_dir: Path
    ):
        self.root = root
        self.orchestrator = orchestrator
        self.client = client
        self.model = model
        self.output_dir = output_dir

    def run(self, arguments: dict[str, Any]) -> str:
        prompt = require_string(arguments, "prompt")
        paths = arguments.get("referenced_image_paths") or []
        if not isinstance(paths, list) or not all(isinstance(p, str) and p.strip() for p in paths):
            raise ToolError("'referenced_image_paths' must be a list of strings")
        if len(paths) > MAX_REFERENCED_IMAGES:
            raise ToolError(f"'referenced_image_paths' must contain at most {MAX_REFERENCED_IMAGES} paths")
        images = []
        remaining = MAX_IMAGE_BYTES
        for value in paths:
            image = self._read_image(value, remaining)
            remaining -= len(image[1])
            images.append(image)
        try:
            if images:
                data = self.client.images_edit(prompt, images, model=self.model)
            else:
                data = self.client.images_generate(prompt, model=self.model)
        except ModelError as exc:
            raise ToolError(f"image generation failed: {exc}") from None
        path = save_image(self.output_dir, data)
        return f"Image saved to {path}"

    def _host_path(self, value: str) -> Path:
        """The host path of a path as sandboxed commands see it (their /tmp is the session's temp_dir)."""
        policy = self.orchestrator.sandbox
        path = Path(value)
        if policy.is_sandboxed and is_within(path, SANDBOX_TEMP_DIR):
            if policy.temp_dir is None:
                raise ToolError(f"cannot read {value}: no such file")
            return policy.temp_dir / path.relative_to(SANDBOX_TEMP_DIR)
        return path if path.is_absolute() else self.root / path

    def _read_image(self, value: str, limit: int) -> tuple[str, bytes, str]:
        """Name, contents and media type of a referenced image of at most `limit` bytes."""
        path = resolve(self._host_path(value))
        policy = self.orchestrator.sandbox
        folders = [resolve(self.root), *([resolve(policy.temp_dir)] if policy.temp_dir is not None else [])]
        if not any(is_within(path, folder) for folder in folders) or not policy.can_read(path):
            raise ToolError(f"cannot read {value}: images must be in the workspace or the session's temp directory")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ToolError(f"cannot read {value}: {exc.strerror}") from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ToolError(f"cannot read {value}: not a regular file")
        with os.fdopen(fd, "rb") as file:
            data = file.read(limit + 1)
        if len(data) > limit:
            raise ToolError(f"cannot read {value}: the images are larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MiB in total")
        kind = image_type(data)
        if kind is None:
            raise ToolError(f"{value} is not a PNG, JPEG, WebP or GIF image")
        return path.name, data, kind


def image_gen_tool(
    config: Config, root: Path, orchestrator: ApprovalOrchestrator, client: ImageClient, output_dir: Path
) -> ImageGenTool | None:
    """The `image_gen` tool when `image_generation.model` is set, otherwise None."""
    if not config.image_generation_model:
        return None
    return ImageGenTool(root, orchestrator, client, config.image_generation_model, output_dir)
