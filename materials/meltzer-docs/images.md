# Image generation

The `image_gen` tool lets the model generate images from a description, or
edit images in the workspace. meltzer sends the request to the Images API of
the model provider (`base_url`, with the same API key as chat requests) and
saves the result as a PNG file.

## Turning it on

Image generation is off by default. Set the image model to turn it on:

```toml
[image_generation]
model = "gpt-image-2"
```

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `image_generation.model` | unset | Images API model; when set, the `image_gen` tool is registered; read only from the user config and the command line |

Subagents get the tool too.

## The tool

```json
{"prompt": "a flowchart of the build steps, flat style", "referenced_image_paths": ["docs/draft.png"]}
```

| Argument | Meaning |
|---|---|
| `prompt` | description of the image, or of the changes to make (required) |
| `referenced_image_paths` | up to 5 images to edit or combine, absolute or relative to the workspace root; omit to generate a new image |

Without `referenced_image_paths`, meltzer sends `POST <base_url>/images/generations`.
With it, meltzer sends `POST <base_url>/images/edits` as a multipart form with
each image as an `image[]` part. Size, quality and background are `auto`.

Referenced images are read by meltzer, and only from the workspace and the
session's private `/tmp` (`/tmp/...` paths refer to it): a path elsewhere, or
one that sandboxed commands cannot read, is refused. Each file must be a PNG,
JPEG, WebP or GIF image (checked from its first bytes); the images of one call
total at most 50 MiB.

## Where images are saved

meltzer writes each result to

```
~/.meltzer/generated_images/<session id>/<n>.png
```

numbered from 1 within the session, and returns the path to the model. An
existing file is never replaced. To use an image elsewhere in the workspace,
the model copies it.
