from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from meltzer.client import ChatClient, ModelError, Usage, parse_response
from meltzer.config.loader import load_config
from meltzer.sandbox.policy import SandboxMode, SandboxPolicy
from meltzer.tools import image_gen
from meltzer.tools.image_gen import (
    MAX_REFERENCED_IMAGES,
    ImageGenTool,
    image_gen_tool,
    image_output_dir,
    image_type,
    save_image,
)
from meltzer.tools.registry import ToolError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8


def image_body(data: bytes = PNG) -> dict:
    return {"created": 1, "data": [{"b64_json": base64.b64encode(data).decode()}]}


def make_client(handler) -> ChatClient:
    return ChatClient("https://models.example/v1", "secret", "gpt-test", transport=httpx.MockTransport(handler))


# Client


def test_images_generate_sends_prompt_and_returns_image_bytes():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=image_body())

    assert make_client(handler).images_generate("a red cube", model="gpt-image-2") == PNG
    (request,) = requests
    assert str(request.url) == "https://models.example/v1/images/generations"
    assert request.headers["authorization"] == "Bearer secret"
    assert json.loads(request.content) == {
        "model": "gpt-image-2",
        "prompt": "a red cube",
        "size": "auto",
        "quality": "auto",
        "background": "auto",
    }


def test_images_edit_sends_multipart_with_every_image():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=image_body(GIF))

    images = [("a.png", PNG, "image/png"), ("b.jpg", JPEG, "image/jpeg")]
    assert make_client(handler).images_edit("make it blue", images, model="gpt-image-2") == GIF
    (request,) = requests
    assert str(request.url) == "https://models.example/v1/images/edits"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert body.count(b'name="image[]"') == 2
    assert b'filename="a.png"' in body and b"Content-Type: image/jpeg" in body
    assert b'name="prompt"\r\n\r\nmake it blue' in body
    assert b'name="model"\r\n\r\ngpt-image-2' in body


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"data": [{"url": "https://x"}]}),
        httpx.Response(200, json={"data": [{"b64_json": "not base64!"}]}),
        httpx.Response(200, json={}),
        httpx.Response(400, text="bad prompt"),
    ],
)
def test_image_response_without_image_data_or_an_http_error_is_an_error(response):
    with pytest.raises(ModelError):
        make_client(lambda request: response).images_generate("x", model="m")


def test_chat_response_usage():
    body = {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230},
    }
    assert parse_response(body).usage == Usage(prompt_tokens=1200, completion_tokens=30)
    assert parse_response({"choices": [{"message": {"content": "hi"}}]}).usage is None
    assert parse_response({"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": "x"}}).usage is None


# Tool


class FakeImages:
    def __init__(self, result: bytes = PNG, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple] = []

    def images_generate(self, prompt, *, model):
        self.calls.append(("generate", prompt, model))
        if self.error:
            raise self.error
        return self.result

    def images_edit(self, prompt, images, *, model):
        self.calls.append(("edit", prompt, images, model))
        return self.result


def make_tool(tmp_path: Path, client=None, **policy) -> tuple[ImageGenTool, Path]:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    sandbox = SandboxPolicy.for_session(policy.pop("mode", SandboxMode.WORKSPACE_WRITE), root, **policy)
    output = image_output_dir(tmp_path / "home", "session-1")
    tool = ImageGenTool(root, SimpleNamespace(sandbox=sandbox), client or FakeImages(), "gpt-image-2", output)
    return tool, output


def test_generate_saves_numbered_files(tmp_path):
    client = FakeImages()
    tool, output = make_tool(tmp_path, client)
    first = tool.run({"prompt": "a red cube"})
    second = tool.run({"prompt": "a blue cube"})
    assert str(output / "1.png") in first
    assert str(output / "2.png") in second
    assert (output / "1.png").read_bytes() == PNG
    assert client.calls == [("generate", "a red cube", "gpt-image-2"), ("generate", "a blue cube", "gpt-image-2")]


def test_save_never_overwrites_and_the_output_dir_comes_from_ids_only(tmp_path):
    directory = tmp_path / "images"
    directory.mkdir()
    (directory / "1.png").write_bytes(b"old")
    assert save_image(directory, PNG) == directory / "2.png"
    assert (directory / "1.png").read_bytes() == b"old"
    assert image_output_dir(tmp_path, "abc-123") == tmp_path / "generated_images" / "abc-123"
    assert image_output_dir(tmp_path, "../x/y") == tmp_path / "generated_images" / "___x_y"


def test_referenced_images_are_sent_for_edit(tmp_path):
    client = FakeImages()
    tool, _ = make_tool(tmp_path, client)
    (tool.root / "a.png").write_bytes(PNG)
    (tool.root / "b.webp").write_bytes(WEBP)
    tool.run({"prompt": "combine", "referenced_image_paths": ["a.png", str(tool.root / "b.webp")]})
    (call,) = client.calls
    assert call[0] == "edit"
    assert call[2] == [("a.png", PNG, "image/png"), ("b.webp", WEBP, "image/webp")]


def test_sandbox_tmp_paths_read_from_session_temp_dir(tmp_path):
    temp = tmp_path / "session-tmp"
    temp.mkdir()
    (temp / "draft.gif").write_bytes(GIF)
    client = FakeImages()
    tool, _ = make_tool(tmp_path, client, temp_dir=temp)
    tool.run({"prompt": "edit", "referenced_image_paths": ["/tmp/draft.gif"]})
    assert client.calls[0][2] == [("draft.gif", GIF, "image/gif")]


def test_referenced_images_must_be_in_the_workspace_or_the_session_temp_dir_and_readable(tmp_path):
    (tmp_path / "work" / "private").mkdir(parents=True)
    (tmp_path / "work" / "private" / "a.png").write_bytes(PNG)
    (tmp_path / "elsewhere.png").write_bytes(PNG)
    client = FakeImages()
    tool, _ = make_tool(tmp_path, client, deny_read=(tmp_path / "work" / "private",))
    for path in ("private/a.png", str(tmp_path / "elsewhere.png"), "../elsewhere.png"):
        with pytest.raises(ToolError, match="cannot read"):
            tool.run({"prompt": "x", "referenced_image_paths": [path]})
    assert client.calls == []


def test_referenced_images_have_a_total_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(image_gen, "MAX_IMAGE_BYTES", 2 * 1024 * 1024)
    tool, _ = make_tool(tmp_path)
    for name in ("a.png", "b.png"):
        (tool.root / name).write_bytes(PNG + bytes(1_500_000))
    tool.run({"prompt": "x", "referenced_image_paths": ["a.png"]})
    with pytest.raises(ToolError, match="larger than 2 MiB in total"):
        tool.run({"prompt": "x", "referenced_image_paths": ["a.png", "b.png"]})


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"prompt": "x", "referenced_image_paths": ["notes.png"]}, "not a PNG, JPEG, WebP or GIF image"),
        ({"prompt": "x", "referenced_image_paths": ["missing.png"]}, "cannot read"),
        ({"prompt": "x", "referenced_image_paths": ["."]}, "cannot read"),
        ({"prompt": "x", "referenced_image_paths": ["a.png"] * (MAX_REFERENCED_IMAGES + 1)}, "at most"),
        ({"prompt": "x", "referenced_image_paths": "a.png"}, "list of strings"),
        ({}, "'prompt'"),
    ],
)
def test_invalid_requests_are_tool_errors(tmp_path, arguments, message):
    tool, _ = make_tool(tmp_path)
    (tool.root / "notes.png").write_bytes(b"plain text")
    with pytest.raises(ToolError, match=message):
        tool.run(arguments)


def test_provider_error_is_a_tool_error(tmp_path):
    tool, output = make_tool(tmp_path, FakeImages(error=ModelError("model provider returned HTTP 500")))
    with pytest.raises(ToolError, match="image generation failed: model provider returned HTTP 500"):
        tool.run({"prompt": "x"})
    assert not output.exists()


@pytest.mark.parametrize(
    ("data", "kind"), [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp"), (b"BM", None)]
)
def test_image_type(data, kind):
    assert image_type(data) == kind


def test_registered_only_with_a_model(tmp_path):
    home = tmp_path / "home"
    args = (tmp_path, SimpleNamespace(sandbox=None), FakeImages(), tmp_path / "out")
    assert image_gen_tool(load_config(tmp_path, home=home), *args) is None
    config = load_config(tmp_path, home=home, overrides=[("image_generation.model", "gpt-image-2")])
    assert image_gen_tool(config, *args).model == "gpt-image-2"
